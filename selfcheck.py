"""
Self-check: verify the environment and exercise every fix on YOUR machine.

Run before trusting a batch, and after any change to the schema or prompts:

    python selfcheck.py

What this is for: the fixes were developed in a sandbox with no network, so
pydantic, streamlit and pymupdf weren't installable there. Everything was
verified by static analysis and by unit-testing extracted functions against
stub schemas -- but the live path, real model output flowing through real
pydantic validation, could only be checked where those packages exist. That
is here.

Nothing here calls the API or costs anything, unless you pass --api, which
makes exactly one cheap call to confirm your model ID and key work.
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import sys
import traceback

PASS, FAIL, WARN = "  PASS", "  FAIL", "  WARN"
results = {"pass": 0, "fail": 0, "warn": 0}


def check(name, fn, warn_only=False):
    """Run one check, print its result, never let it stop the run."""
    try:
        detail = fn()
        print(f"{PASS}  {name}" + (f" — {detail}" if detail else ""))
        results["pass"] += 1
    except Exception as e:
        tag = WARN if warn_only else FAIL
        results["warn" if warn_only else "fail"] += 1
        print(f"{tag}  {name} — {type(e).__name__}: {e}")
        if not warn_only and "-v" in sys.argv:
            traceback.print_exc()


def section(title):
    print(f"\n{title}\n" + "-" * len(title))


# ---------------------------------------------------------------------------
# 1. Dependencies
# ---------------------------------------------------------------------------

def check_dependencies():
    section("1. Dependencies")

    for module, label in [
        ("pydantic", "pydantic"), ("streamlit", "streamlit"), ("pymupdf", "PyMuPDF"),
        ("docx", "python-docx"), ("openpyxl", "openpyxl"), ("reportlab", "reportlab"),
        ("PIL", "pillow"), ("pytesseract", "pytesseract"), ("openai", "openai"),
        ("pandas", "pandas"), ("dotenv", "python-dotenv"),
    ]:
        def probe(m=module, l=label):
            mod = importlib.import_module(m)
            return f"{l} {getattr(mod, '__version__', 'installed')}"
        check(f"import {label}", probe)

    def pydantic_v2():
        import pydantic
        major = int(pydantic.__version__.split(".")[0])
        if major < 2:
            raise RuntimeError(
                f"pydantic {pydantic.__version__} found; this code targets v2. "
                "Run: pip install -U pydantic"
            )
        return f"v{pydantic.__version__}"
    check("pydantic is v2", pydantic_v2)

    def tesseract():
        path = shutil.which("tesseract")
        if not path:
            raise RuntimeError(
                "tesseract binary not on PATH — scans and photos will fail. "
                "Install with: brew install tesseract"
            )
        return path
    check("tesseract binary (needed for scans/photos)", tesseract, warn_only=True)


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------

def check_config():
    section("2. Configuration")

    def api_key():
        import os
        from dotenv import load_dotenv
        load_dotenv()
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY not set. Create a .env file containing:\n"
                "         GEMINI_API_KEY=your_key_here\n"
                "         GEMINI_MODEL=gemini-3.6-flash"
            )
        # Never print a key, even partially -- a screenshot of this output
        # shouldn't leak anything.
        return f"set ({len(key)} chars)"
    check("GEMINI_API_KEY present", api_key)

    def model_id():
        import os
        model = os.getenv("GEMINI_MODEL", "(using code default)")
        if "2.0" in model or "1.5" in model:
            raise RuntimeError(
                f"GEMINI_MODEL={model} looks retired. Google drops model IDs "
                "periodically; try gemini-3.6-flash."
            )
        return model
    check("GEMINI_MODEL looks current", model_id, warn_only=True)

    def invariants():
        from extract import DEFAULT_CHUNK_MAX_CHARS, MAX_OUTPUT_TOKENS
        from prompts import MAX_DOCUMENT_CHARS
        if DEFAULT_CHUNK_MAX_CHARS >= MAX_DOCUMENT_CHARS:
            raise RuntimeError(
                f"chunk size {DEFAULT_CHUNK_MAX_CHARS} >= prompt ceiling "
                f"{MAX_DOCUMENT_CHARS} — chunks would be silently truncated"
            )
        return (f"chunk {DEFAULT_CHUNK_MAX_CHARS:,} < ceiling "
                f"{MAX_DOCUMENT_CHARS:,}; output {MAX_OUTPUT_TOKENS:,} tokens")
    check("size invariants hold", invariants)


# ---------------------------------------------------------------------------
# 3. The path that couldn't be tested in the sandbox:
#    messy model output -> sanitizer -> REAL pydantic validation
# ---------------------------------------------------------------------------

def check_validation():
    section("3. Model output → sanitizer → pydantic (the untested path)")

    # Imports live INSIDE each check, not at section level. A missing
    # dependency must report as a failed check, not abort the whole script --
    # a diagnostic tool that dies on the first problem tells you about one
    # issue when you wanted the list.
    def messy_payload():
        from extract import _sanitize_extraction_payload
        from schema import ContractExtraction

        """
        Everything a model realistically gets wrong, in one object. Before the
        fixes, ANY ONE of these discarded the entire contract.
        """
        payload = {
            "vendor_name": {"value": "Oracle Corporation",
                            "evidence": {"page": "p. 11", "quote": "quote"}},
            "auto_renewal": True,                       # bool, not "yes"
            "renewal_notice_period_days": 30,           # int where str declared
            "contract_value": 1200000.0,                # float
            "audit_notice_period_days": {"value": "ten (10) days",
                                         "evidence": {"page": "page 7", "quote": "q"}},
            "products": [{
                "publisher_part_number": "GX-4471",
                "product_name": "DataCore Enterprise",
                "purchased_rights": 128,                # the ELP-critical one
                "unit_cost": 4200.5,
                "evidence": {"page": "11", "quote": "row"},
            }],
            "risk_flags": [
                {"clause": "Audit", "severity": "CRITICAL", "explanation": "x"},
                "",                                     # stray empty string
                None,
            ],
            "key_clauses": [
                {"clause_type": "audit rights", "text": "T", "page": "11"},
                {"clause_type": "Not A Real Type", "text": "X"},
                {"clause_type": "Term & renewal"},      # no text or summary
            ],
            "fields_not_found": ["sla_summary", 42],
            # plain_english_summary deliberately absent
        }
        cleaned = _sanitize_extraction_payload(payload)
        model = ContractExtraction.model_validate(cleaned)

        assert model.renewal_notice_period_days.value == "30", "int→str failed"
        assert model.auto_renewal.value == "yes", "bool→yes failed"
        assert model.products[0].purchased_rights == "128", "quantity coercion failed"
        assert model.vendor_name.evidence.page == 11, "'p. 11' not parsed"
        assert model.risk_flags[0].severity == "medium", "bad severity not normalised"
        assert len(model.risk_flags) == 1, "malformed flags not dropped"
        assert [c.clause_type for c in model.key_clauses] == ["Audit rights"], \
            "clause filtering wrong"
        assert model.plain_english_summary == "", "missing summary not defaulted"
        return "all 8 coercions validated against real pydantic"
    check("messy payload validates", messy_payload)

    def minimal_payload():
        """An almost-empty response should still produce a usable object."""
        from extract import _sanitize_extraction_payload
        from schema import ContractExtraction
        model = ContractExtraction.model_validate(_sanitize_extraction_payload({}))
        assert model.products == [] and model.key_clauses == []
        return "empty response handled"
    check("near-empty payload validates", minimal_payload)

    def json_repair():
        import json
        from extract import _repair_json
        for raw in ['{"a":1,}', '```json\n{"a":1}\n```', 'Here:\n{"a":1}']:
            assert json.loads(_repair_json(raw)) == {"a": 1}
        # Truncated output must STILL fail -- fabricating a closing brace would
        # turn "cut off" into a silently incomplete extraction.
        try:
            json.loads(_repair_json('{"a": "Oracle Corp'))
            raise AssertionError("truncated JSON was silently 'repaired'")
        except json.JSONDecodeError:
            pass
        # Verbatim text must survive untouched.
        q = json.dumps({"t": "the Programs [as defined, ] and {as amended, } apply"})
        assert json.loads(_repair_json(q))["t"] == json.loads(q)["t"], \
            "repair corrupted quoted contract text"
        return "repairs work, truncation still fails, quotes preserved"
    check("JSON repair", json_repair)


# ---------------------------------------------------------------------------
# 4. Crash and hang fixes
# ---------------------------------------------------------------------------

def check_robustness():
    section("4. Crash / hang fixes")

    def chunker():
        from chunking import chunk_document
        from parser import ParsedDocument, PageResult

        # A page bigger than the whole budget used to hang forever.
        doc = ParsedDocument(filename="t.pdf", file_type="pdf", pages=[
            PageResult(1, "intro. " * 50, "native"),
            PageResult(2, "A" * 120000, "native"),
            PageResult(3, "outro. " * 50, "native"),
        ])
        chunks = chunk_document(doc, max_chars=45000)
        assert all(len(c.text) <= 46000 for c in chunks), "chunk over budget"

        # No paragraph breaks: used to leak one oversized chunk.
        doc2 = ParsedDocument(filename="t.txt", file_type="txt",
                              pages=[PageResult(1, "B" * 200000, "native")])
        chunks2 = chunk_document(doc2, max_chars=45000)
        assert all(len(c.text) <= 46000 for c in chunks2), "single-page leak"
        return f"oversized pages split ({len(chunks)} and {len(chunks2)} chunks)"
    check("chunker: no hang, no oversized chunk", chunker)

    def limiter():
        from rate_limiter import TokenRateLimiter
        TokenRateLimiter(tpm_budget=8000).reserve(27000)  # used to IndexError
        return "over-budget request handled"
    check("rate limiter: no IndexError", limiter)

    def rules():
        from risk_rules import parse_days, parse_money, evaluate_rules
        assert parse_days("thirty (30) days prior written notice") == 30
        assert parse_days("forty-five (45) days") == 45
        assert parse_money("500k") == 500000
        assert parse_money("EUR 2.5 million") == 2500000
        return "parsers correct, rules deterministic"
    check("risk rules", rules)


# ---------------------------------------------------------------------------
# 5. Output generation against hostile content
# ---------------------------------------------------------------------------

def check_outputs():
    section("5. PDF / Excel against hostile contract text")

    def _hostile_extraction():
        """Build one extraction containing everything that used to break output."""
        from schema import ContractExtraction
        from extract import _sanitize_extraction_payload
        from risk_rules import apply_risk_rules

        # Angle brackets break reportlab's XML parser; control characters
        # break openpyxl. Both appear in real contracts and OCR respectively.
        hostile = _sanitize_extraction_payload({
            "vendor_name": {"value": "AT&T <Global> Services \x07Ltd", "evidence": None},
            "auto_renewal": {"value": "yes", "evidence": None},
            "audit_rights_present": {"value": "yes", "evidence": None},
            "audit_notice_period_days": {"value": "ten (10) days", "evidence": None},
            "contract_value": {"value": "$1,200,000", "evidence": None},
            "plain_english_summary": "S&S applies if headcount <b then review.",
            "products": [{"publisher_part_number": "GX-1\x00",
                          "product_name": "Widget & <Pro>",
                          "purchased_rights": "128",
                          "evidence": {"page": 11, "quote": "row\x0bhere"}}],
            "key_clauses": [{"clause_type": "Audit rights", "heading": "11.3 <Audit>",
                             "text": "excess <REDACTED> billed at list, } per clause 7 & 8",
                             "summary": "Audit on 10 days notice.", "page": 11}],
        })
        return apply_risk_rules(ContractExtraction.model_validate(hostile))

    def pdf():
        from report_generator import generate_pdf_report
        data = generate_pdf_report(_hostile_extraction(), "AT&T <contract> & co.pdf")
        assert len(data) > 1000
        return f"{len(data):,} bytes"
    check("PDF survives angle brackets and ampersands", pdf)

    def xlsx():
        from entitlement import generate_batch_workbook

        class R:
            filename = "AT&T <2026> [signed]/v3.pdf"
            extraction = None
            error = None
            pages = 12
            ocr_pages = 3
            conflicts: dict = {}
            ok = True
            vendor = "AT&T Global Services Ltd"
            line_item_count = 1

        class B:
            results = [R]
            successful = [R]
            failed: list = []
            total_line_items = 1

        R.extraction = _hostile_extraction()
        data = generate_batch_workbook(B)
        assert len(data) > 1000
        return f"{len(data):,} bytes, illegal sheet chars handled"
    check("Excel survives OCR control characters", xlsx)


# ---------------------------------------------------------------------------
# 6. Optional: one real API call
# ---------------------------------------------------------------------------

def check_api():
    section("6. Live API (one small call)")

    def call():
        from extract import DEFAULT_MODEL, _get_client, _sampling_kwargs
        client = _get_client()
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user",
                       "content": 'Reply with exactly this JSON: {"ok": true}'}],
            max_tokens=50,
            response_format={"type": "json_object"},
            **_sampling_kwargs(DEFAULT_MODEL),
        )
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("empty response from the model")
        return f"{DEFAULT_MODEL} responded"
    check("API key and model both work", call)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Verify the analyzer on this machine.")
    parser.add_argument("--api", action="store_true",
                        help="also make one small live API call")
    parser.add_argument("-v", action="store_true", help="show tracebacks on failure")
    args = parser.parse_args()

    print("SAM Contract Analyzer — self-check")
    print(f"Python {sys.version.split()[0]}")

    check_dependencies()
    check_config()
    check_validation()
    check_robustness()
    check_outputs()
    if args.api:
        check_api()
    else:
        print("\n(skipping the live API check — add --api to include it)")

    print("\n" + "=" * 58)
    print(f"  {results['pass']} passed, {results['fail']} failed, "
          f"{results['warn']} warnings")
    if results["fail"]:
        print("\n  Fix the failures above before running real contracts.")
        print("  Re-run with -v to see full tracebacks.")
    elif results["warn"]:
        print("\n  Usable. Warnings mean some features are unavailable —")
        print("  no tesseract means scans and photos won't work.")
    else:
        print("\n  Everything checks out. Run: streamlit run app.py")
    print("=" * 58)

    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
