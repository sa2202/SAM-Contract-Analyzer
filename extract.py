"""
Calls the Gemini API to extract structured contract data from parsed
document text, using the schema + prompts defined in schema.py / prompts.py.

Uses Gemini's OpenAI-compatible endpoint via the `openai` client, so no
Google-specific SDK is required.

Setup required before this will run:
    1. Get a free API key at https://aistudio.google.com/apikey (no credit
       card required)
    2. Copy .env.example to .env and paste your key in as GEMINI_API_KEY
    3. pip install -r requirements.txt

This module does NOT read files or handle uploads itself -- it expects
already-parsed text (e.g. from parser.ParsedDocument.full_text).
"""

from __future__ import annotations

import json
import re
import os

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import ValidationError

from prompts import build_messages, build_reduce_messages, MAX_DOCUMENT_CHARS
from schema import CLAUSE_TYPES, ContractExtraction
from chunking import chunk_document, DocumentChunk
from merge import merge_extractions, MergeResult
from rate_limiter import TokenRateLimiter, estimate_tokens
from risk_rules import apply_risk_rules

load_dotenv()  # reads .env into environment variables if present

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_SIGNUP_HINT = "https://aistudio.google.com/apikey"

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# How large a single chunk is allowed to be before chunking kicks in.
#
# This MUST stay below prompts.MAX_DOCUMENT_CHARS. It previously sat at
# 180,000 while the prompt builder truncated at 14,000, which meant the
# chunker would emit one oversized chunk that was then silently cut down
# before the model saw it -- the map-reduce path existed but never actually
# protected anything. Keeping the two in a known relationship (assert
# below) makes that class of mismatch impossible to reintroduce quietly.
DEFAULT_CHUNK_MAX_CHARS = 45000

# Conservative token-per-minute ceiling used by the rate limiter. Free-tier
# numbers reported for Gemini vary noticeably across sources (roughly
# 250K-1M TPM depending on model/source, and Google has changed these more
# than once in 2026) -- 200,000 is set well below even the most
# conservative figure found, as headroom against both inaccurate reporting
# and future reductions. Check
# https://ai.google.dev/gemini-api/docs/rate-limits for current numbers.
DEFAULT_TPM_BUDGET = 200000

# Caps the model's OUTPUT length.
#
# SIZE THIS AGAINST THE SCHEMA, NOT INTUITION. Too low a cap doesn't produce a
# nice "response too long" error -- the JSON is cut off mid-string and fails to
# parse, surfacing as "Unterminated string at line N", which points at the
# symptom and says nothing about the cause.
#
# This was 4000, which was correct when the output was ~22 fields plus flags
# and a summary. Adding key_clauses changed the arithmetic completely: up to
# 20 clauses, each carrying ~120 words of VERBATIM contract text plus a
# heading and a summary line, is on its own around 4,200 tokens -- more than
# the entire old budget, before a single field or line item is written.
#
# Rough budget for a dense contract:
#     key clauses    ~4,200      fields + evidence  ~1,000
#     line items       ~500      summary + structure  ~950
#     ---------------------------------------------------
#     total          ~6,700 tokens
#
# 16,000 gives roughly 2.4x headroom, which matters because Gemini 3.x models
# also spend output tokens on internal reasoning before emitting the answer.
# Gemini 3.6 Flash permits 65,536 output tokens, so this is not near any limit.
# If you extend CLAUSE_TYPES or raise the per-clause word budget in prompts.py,
# revisit this number.
MAX_OUTPUT_TOKENS = 16000

# Fail loudly at import time if the chunk budget ever drifts back above the
# prompt ceiling. A silent mismatch here costs whole sections of every long
# contract, and it is invisible in the output -- the app reports the missing
# clauses as "not found in this document", which looks like a clean result.
assert DEFAULT_CHUNK_MAX_CHARS < MAX_DOCUMENT_CHARS, (
    f"Chunk size ({DEFAULT_CHUNK_MAX_CHARS}) must stay below the prompt "
    f"truncation ceiling ({MAX_DOCUMENT_CHARS}), or chunks get silently cut."
)


class ExtractionError(Exception):
    """Raised when the LLM call fails or its output doesn't match the schema."""


def _get_client() -> OpenAI:
    api_key = os.getenv(GEMINI_API_KEY_ENV)
    if not api_key:
        raise ExtractionError(
            f"{GEMINI_API_KEY_ENV} is not set. Copy .env.example to .env and add "
            f"your key from {GEMINI_SIGNUP_HINT}."
        )
    return OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)


# ---------------------------------------------------------------------------
# Response sanitization
# ---------------------------------------------------------------------------
# Models occasionally deviate slightly from the requested JSON shape --
# e.g. an empty string ('') showing up inside a list of objects, or a bare
# string where {"value": ..., "evidence": ...} was expected. Rather than
# letting one malformed entry fail the entire extraction, we clean up
# known-safe deviations before schema validation. Anything that's still
# broken after this still surfaces as a normal ExtractionError.

_EXTRACTED_FIELD_NAMES = [
    "vendor_name", "customer_name", "contract_title",
    "effective_date", "term_end_date", "term_length", "auto_renewal",
    "renewal_notice_period_days", "contract_value", "pricing_model",
    "price_escalation_cap", "payment_terms", "license_metric",
    "true_up_rights", "audit_rights_present", "audit_notice_period_days",
    "audit_frequency", "termination_for_convenience", "termination_for_cause",
    "early_termination_fee", "sla_summary", "data_exit_transition_period",
]


# ```json ... ``` fences. Asked for raw JSON, models still wrap it sometimes.
_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _strip_trailing_commas(text: str) -> str:
    """
    Remove commas that sit immediately before a closing brace or bracket.

    Scans character by character, tracking whether we are inside a string
    literal, because a regex cannot tell the difference between structural
    punctuation and the same characters inside a quoted value. That
    distinction is critical here: this tool's whole value is VERBATIM clause
    text, and contract language can legitimately contain sequences like
    "[as defined, ]". A regex-based strip silently deletes that comma from
    the quote -- corrupting the evidence a reviewer is meant to rely on,
    with nothing to indicate it happened. A repair pass that quietly edits
    contract text is worse than no repair pass.
    """
    out = []
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            continue

        if ch == ",":
            # Look ahead past whitespace. If the next structural character
            # closes the current container, this comma is trailing.
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "}]":
                continue  # drop it
        out.append(ch)

    return "".join(out)


def _repair_json(raw: str) -> str:
    """
    Fix the malformations models actually produce, without touching content.

    A model asked for JSON returns *nearly* JSON with some regularity: a
    trailing comma before a closing brace, a markdown fence around the whole
    thing, a line of prose before the opening brace. None of these are
    ambiguous and all are mechanical to fix, so repairing locally is strictly
    better than burning a retry -- a retry costs an API call, takes seconds,
    and the model may well repeat the same mistake.

    Deliberately conservative. It only removes syntax that cannot carry
    meaning; it never rewrites values, never guesses at missing fields, and
    never closes an unbalanced brace. A truncated response stays broken here
    on purpose -- inventing a closing brace would turn "the response was cut
    off" into a silently incomplete extraction, which is far worse than a
    clean error, because nothing downstream would flag it.
    """
    text = _CODE_FENCE.sub("", raw).strip()

    # Drop anything before the first { and after the last } -- covers a
    # preamble line ("Here is the JSON:") or a trailing note. Only when both
    # are present and correctly ordered, so a truncated response is untouched.
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last != -1 and last > first:
        text = text[first:last + 1]

    return _strip_trailing_commas(text)


def _as_text(value):
    """
    Coerce a scalar to str, leaving None and containers alone.

    Pydantic v2 does NOT accept an int where a str is declared -- that lax
    coercion existed in v1 and was deliberately removed. It matters here
    because the fields most likely to come back as bare numbers are exactly
    the ones an ELP is built from: a quantity of 128, a unit cost of 4200, a
    notice period of 30. One numeric quantity would fail validation and
    discard the entire contract, which is a catastrophic response to a
    trivial type difference.

    Booleans are handled explicitly because bool is a subclass of int in
    Python, and "True" is not a useful value for auto_renewal -- the schema
    expects "yes"/"no"/"unclear", so they are mapped accordingly.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        # Render 128.0 as "128" rather than "128.0" -- these are quantities
        # and part numbers, and a spurious decimal reads as a real one.
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return value  # dict/list left for the caller to handle


def _as_page(value):
    """
    Coerce a page reference to int, or None.

    Models return page numbers as "11", "p. 11" or "page 11" as readily as
    11. A non-numeric page is not worth failing an extraction over -- losing
    the page number costs one citation, losing the extraction costs the whole
    contract -- so anything unparseable becomes None.
    """
    if value is None or isinstance(value, int) and not isinstance(value, bool):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def _clean_evidence(raw):
    """Normalise an evidence object, or None if it isn't one."""
    if not isinstance(raw, dict):
        return None
    return {
        "page": _as_page(raw.get("page")),
        "quote": _as_text(raw.get("quote")),
    }


def _sanitize_extraction_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        return data

    # risk_flags: drop malformed entries rather than failing the response
    risk_flags = data.get("risk_flags")
    if isinstance(risk_flags, list):
        cleaned = []
        for item in risk_flags:
            if not isinstance(item, dict):
                continue  # drop malformed entries like ''
            item["severity"] = (_as_text(item.get("severity")) or "medium").lower()
            item["explanation"] = _as_text(item.get("explanation")) or ""
            item["clause"] = _as_text(item.get("clause")) or "Unspecified clause"
            item["evidence"] = _clean_evidence(item.get("evidence"))
            # Severity drives colour and sort order everywhere downstream, so
            # an unrecognised value would sort last and render unstyled.
            if item["severity"] not in ("low", "medium", "high"):
                item["severity"] = "medium"
            cleaned.append(item)
        data["risk_flags"] = cleaned

    # products: every field stringified, because quantities and prices are
    # exactly the values a model is most likely to emit as bare numbers
    products = data.get("products")
    if isinstance(products, list):
        cleaned_products = []
        for item in products:
            if not isinstance(item, dict):
                continue
            for key in ("publisher_part_number", "product_name", "license_type",
                        "license_metric", "purchased_rights", "unit_cost",
                        "start_date", "end_date", "country_of_agreement"):
                if key in item:
                    item[key] = _as_text(item[key])
            item["evidence"] = _clean_evidence(item.get("evidence"))
            cleaned_products.append(item)
        data["products"] = cleaned_products

    # key_clauses: drop malformed entries, and drop any whose clause_type is
    # off-catalogue. An invented type would sit in the clause grid as a
    # one-off row lining up with nothing in any other contract, defeating the
    # point of comparing the same clause side by side.
    clauses = data.get("key_clauses")
    if isinstance(clauses, list):
        valid_types = {t.lower(): t for t in CLAUSE_TYPES}
        cleaned_clauses = []
        for item in clauses:
            if not isinstance(item, dict):
                continue
            raw_type = str(item.get("clause_type") or "").strip()
            canonical = valid_types.get(raw_type.lower())
            if canonical is None:
                continue
            item["clause_type"] = canonical  # normalise casing/spacing
            item["heading"] = _as_text(item.get("heading"))
            item["text"] = _as_text(item.get("text"))
            item["summary"] = _as_text(item.get("summary"))
            item["page"] = _as_page(item.get("page"))
            if not (item.get("text") or item.get("summary")):
                continue  # an entry with neither is an empty row
            cleaned_clauses.append(item)
        data["key_clauses"] = cleaned_clauses

    # fields_not_found must be a list of strings
    fnf = data.get("fields_not_found")
    if isinstance(fnf, list):
        data["fields_not_found"] = [x for x in (_as_text(v) for v in fnf) if isinstance(x, str)]
    else:
        data["fields_not_found"] = []

    # Declared as a plain str, so None or a number would fail validation on a
    # field carrying no structural weight whatsoever.
    data["plain_english_summary"] = _as_text(data.get("plain_english_summary")) or ""

    # A field expected as {"value":..., "evidence":...} comes back as a bare
    # string often enough to be worth handling, and occasionally as a number.
    for name in _EXTRACTED_FIELD_NAMES:
        val = data.get(name)
        if val is None:
            continue
        if isinstance(val, dict):
            data[name] = {
                "value": _as_text(val.get("value")),
                "evidence": _clean_evidence(val.get("evidence")),
            }
        else:
            data[name] = {"value": _as_text(val), "evidence": None}

    return data


def _sampling_kwargs(model: str) -> dict:
    """
    Sampling parameters to send for a given model.

    `temperature=0` was doing real work here: extraction should be as close to
    deterministic as the API allows, because the same contract analysed twice
    ought to produce the same numbers. Gemini 3.x DEPRECATED temperature,
    top_p and top_k -- on that family the value is accepted and then silently
    ignored, so sending it neither helps nor errors; it just creates a false
    impression that output is pinned when it isn't.

    Sending nothing on 3.x is therefore honest rather than merely tidy. Older
    model families still honour it, so it is still sent to those. Note that
    determinism of the RISK SCORES does not depend on this at all -- severity
    comes from risk_rules.py, which is deterministic by construction. What
    varies without temperature is the wording of summaries and, occasionally,
    which of two equally-valid quotes gets picked as evidence.
    """
    family_3x = model.startswith(("gemini-3", "gemini-4"))
    return {} if family_3x else {"temperature": 0}


def extract_contract(
    document_text: str,
    document_filename: str = "uploaded contract",
    model: str = DEFAULT_MODEL,
    max_retries: int = 1,
) -> ContractExtraction:
    """
    Sends parsed contract text to Gemini and returns a validated
    ContractExtraction object.

    Raises ExtractionError if the API call fails, or if the model's output
    doesn't validate against the schema after retries and sanitization.
    """
    client = _get_client()
    messages = build_messages(document_text, document_filename)

    last_error: Exception | None = None
    raw_content = ""

    for attempt in range(1, max_retries + 2):  # e.g. max_retries=1 -> try twice total
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=MAX_OUTPUT_TOKENS,  # see comment above -- avoids mid-string truncation
                response_format={"type": "json_object"},  # forces valid JSON output
                **_sampling_kwargs(model),
            )
            raw_content = response.choices[0].message.content or ""

            # Check for truncation BEFORE trying to parse. When the model runs
            # out of output budget it stops mid-token, and json.loads then
            # reports "Unterminated string at line N" -- an error that
            # describes where parsing gave up, not why. finish_reason tells us
            # the real cause, so we can say so plainly and name the fix.
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            if finish_reason == "length":
                raise ExtractionError(
                    f"The model's response was cut off after {MAX_OUTPUT_TOKENS} "
                    f"output tokens, so the JSON is incomplete. This contract needs "
                    f"a larger budget: raise MAX_OUTPUT_TOKENS in extract.py, or "
                    f"reduce the number of clause types in CLAUSE_TYPES (schema.py) "
                    f"or the per-clause word limit in prompts.py."
                )

            # strict=False tolerates literal control characters (e.g. raw
            # newlines) inside JSON string values, which some models emit
            # unescaped despite being asked for valid JSON.
            try:
                parsed_json = json.loads(raw_content, strict=False)
            except json.JSONDecodeError:
                # Try a local repair before spending a retry on the API. See
                # _repair_json -- it only removes syntax that can't carry
                # meaning, so if this succeeds the content is unchanged.
                parsed_json = json.loads(_repair_json(raw_content), strict=False)
            parsed_json = _sanitize_extraction_payload(parsed_json)
            return ContractExtraction.model_validate(parsed_json)

        except json.JSONDecodeError as e:
            # "Unterminated string" almost always means the response was cut
            # short rather than malformed, so say that rather than leaving the
            # reader to infer it from a character offset.
            hint = ""
            if "Unterminated" in str(e) or "Expecting" in str(e):
                hint = (
                    f" This usually means the response was cut off before it "
                    f"finished -- try raising MAX_OUTPUT_TOKENS in extract.py "
                    f"(currently {MAX_OUTPUT_TOKENS})."
                )
            last_error = ExtractionError(f"Model did not return valid JSON: {e}.{hint}")
        except ValidationError as e:
            last_error = ExtractionError(f"Model output didn't match the expected schema: {e}")
        except ExtractionError:
            # Raised deliberately above for output truncation. Re-raise rather
            # than retrying: the token budget is identical on the next attempt,
            # so a retry burns an API call to fail the same way -- and worse,
            # the retry path appends the truncated response to the message
            # history, making the next request larger than the one that just
            # ran out of room. Must sit BEFORE the generic handler below, or
            # it gets rewrapped as a nondescript "API call failed".
            raise
        except Exception as e:  # covers API/network errors from the SDK
            error_str = str(e)
            if "model_not_found" in error_str or "does not exist" in error_str or "404" in error_str:
                last_error = ExtractionError(
                    f"The model '{model}' is not available -- Google retires model "
                    f"IDs periodically. Set GEMINI_MODEL=gemini-3.6-flash in your "
                    f".env file, or check "
                    f"https://ai.google.dev/gemini-api/docs/models for the current "
                    f"list. (Original error: {e})"
                )
            elif "rate_limit_exceeded" in error_str or "tokens per minute" in error_str or "429" in error_str:
                last_error = ExtractionError(
                    f"Rate limit hit on Gemini. Try again in a minute, or try a "
                    f"shorter document. (Original error: {e})"
                )
            else:
                last_error = ExtractionError(f"Gemini API call failed: {e}")

        if attempt <= max_retries:
            # On retry, nudge the model to fix its own output rather than
            # resending the whole prompt from scratch.
            messages = messages + [
                {"role": "assistant", "content": raw_content},
                {"role": "user", "content": (
                    "That response was invalid. Return ONLY a single valid JSON "
                    "object matching the required schema -- no markdown, no "
                    "commentary, no trailing text, and every list item must be "
                    "a proper object, never an empty string."
                )},
            ]

    raise last_error  # all attempts exhausted


def _run_reduce_summary(merged_extraction: ContractExtraction, model: str, limiter: TokenRateLimiter) -> str:
    """
    Reduce step: one cheap LLM call over the already-merged structured facts
    (not the raw document again) to produce one coherent summary, instead of
    just concatenating each chunk's separate summary.
    """
    client = _get_client()

    facts = {
        name: (getattr(merged_extraction, name).value if getattr(merged_extraction, name, None) else None)
        for name in _EXTRACTED_FIELD_NAMES
    }
    risk_flags = [
        {"severity": f.severity, "clause": f.clause, "explanation": f.explanation}
        for f in merged_extraction.risk_flags
    ]

    messages = build_reduce_messages(facts, risk_flags)
    estimated = sum(estimate_tokens(m["content"]) for m in messages) + 300  # + output headroom
    limiter.reserve(estimated)

    response = client.chat.completions.create(
        model=model, messages=messages, max_tokens=300,
        **_sampling_kwargs(model),
    )
    return response.choices[0].message.content.strip()


def extract_contract_chunked(
    document_text_or_doc,
    document_filename: str = "uploaded contract",
    model: str = DEFAULT_MODEL,
    tpm_budget: int = DEFAULT_TPM_BUDGET,
    chunk_max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
    progress_callback=None,
    limiter: TokenRateLimiter | None = None,
) -> tuple[ContractExtraction, MergeResult]:
    """
    Map-reduce extraction for documents of any size.

    - MAP: the document is split into overlapping chunks (chunking.py).
      Given Gemini's generous free-tier budget, most real contracts fit in
      a single chunk -- chunking only activates for unusually long
      documents. Each chunk is sent through extract_contract(), spaced out
      by a rate limiter so consecutive chunk calls don't collectively bust
      the per-minute token budget.
    - REDUCE: per-chunk results are merged structurally (merge.py) -- most
      fields are simple "first non-null value wins" since a fact either
      appears once in the document or it doesn't. Risk flags are deduped.
      Only the final summary gets a dedicated LLM synthesis call, run over
      the compact merged facts rather than the raw text again.

    For a document that fits in a single chunk, this is exactly one
    extract_contract() call plus no reduce call -- no overhead added for
    the common case.

    `progress_callback(current_chunk: int, total_chunks: int)` is optional,
    for a UI to show "Extracting section 2 of 4...".

    `limiter` is optional; pass a shared TokenRateLimiter when processing
    multiple documents so they draw on one token budget rather than one each.

    Accepts either a parser.ParsedDocument (preferred -- enables page-aware
    chunking) or a plain string (falls back to paragraph-based chunking).
    """
    from parser import ParsedDocument, PageResult  # local import avoids a hard dependency for text-only callers

    if isinstance(document_text_or_doc, ParsedDocument):
        doc = document_text_or_doc
    else:
        # wrap a plain string as a single-page ParsedDocument so chunk_document
        # can treat it uniformly
        doc = ParsedDocument(filename=document_filename, file_type="text")
        doc.pages.append(PageResult(page_number=1, text=document_text_or_doc, source="native"))

    chunks: list[DocumentChunk] = chunk_document(doc, max_chars=chunk_max_chars)

    # A caller analysing several contracts in one run (see batch.py) passes a
    # shared limiter so the per-minute token budget is respected across the
    # whole batch. Building a fresh one per document would give each file its
    # own 60-second allowance and blow straight through the real limit.
    if limiter is None:
        limiter = TokenRateLimiter(tpm_budget=tpm_budget)

    chunk_results: list[ContractExtraction] = []
    for chunk in chunks:
        if progress_callback:
            progress_callback(chunk.index + 1, chunk.total_chunks)

        messages = build_messages(chunk.text, document_filename)
        estimated = sum(estimate_tokens(m["content"]) for m in messages) + MAX_OUTPUT_TOKENS
        limiter.reserve(estimated)

        result = extract_contract(chunk.text, document_filename, model=model)
        chunk_results.append(result)

    merge_result = merge_extractions(chunk_results)

    if merge_result.chunk_count > 1:
        # reduce step: one extra call for a coherent final summary
        try:
            merge_result.merged.plain_english_summary = _run_reduce_summary(
                merge_result.merged, model, limiter
            )
        except Exception:
            # non-fatal -- fall back to the concatenated summary merge.py
            # already produced rather than failing the whole extraction
            pass

    # Deterministic risk scoring runs LAST, over the fully merged facts --
    # not per-chunk. A rule like "auto-renews with a short notice window"
    # needs the renewal flag and the notice period together, and those can
    # legitimately come from two different chunks of a long agreement.
    merge_result.merged = apply_risk_rules(merge_result.merged)

    return merge_result.merged, merge_result


if __name__ == "__main__":
    # Manual smoke test -- only runs if you execute this file directly AND
    # have a real GEMINI_API_KEY set. Not run automatically anywhere else.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from parser import parse_document

    sample_pdf = Path(__file__).parent / "tests" / "fixtures" / "microsoft_native.pdf"
    if not sample_pdf.exists():
        print("Run tests/make_test_files.py first to generate sample fixtures.")
        sys.exit(1)

    print(f"Model: {DEFAULT_MODEL}  |  TPM budget: {DEFAULT_TPM_BUDGET}")
    doc = parse_document(sample_pdf)
    result, merge_info = extract_contract_chunked(doc, document_filename=sample_pdf.name)
    print(f"Chunks used: {merge_info.chunk_count}")
    print(result.model_dump_json(indent=2))
