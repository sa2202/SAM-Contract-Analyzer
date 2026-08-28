"""
The "reduce" side of map-reduce extraction.

Most fields are merged structurally (no LLM needed): a fact like "audit
notice period" either showed up in one chunk or it didn't, so we just take
the first non-null value found across chunks, in document order. Where two
chunks disagree (rare, but possible if a renewal amendment restates a term),
we keep the first occurrence but record the disagreement so the UI/PDF can
surface it rather than silently picking one.

`plain_english_summary` is the one field that genuinely benefits from an
LLM pass over the *merged* facts (cheap -- it's operating on compact
structured JSON, not the raw document text again).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from schema import CLAUSE_TYPES, ContractExtraction, ExtractedField, KeyClause, ProductLineItem, RiskFlag

# Fields merged structurally, in the order they should be checked/reported.
_FIELD_NAMES = [
    "vendor_name", "customer_name", "contract_title",
    "effective_date", "term_end_date", "term_length", "auto_renewal",
    "renewal_notice_period_days", "contract_value", "pricing_model",
    "price_escalation_cap", "payment_terms", "license_metric",
    "true_up_rights", "audit_rights_present", "audit_notice_period_days",
    "audit_frequency", "termination_for_convenience", "termination_for_cause",
    "early_termination_fee", "sla_summary", "data_exit_transition_period",
]

MAX_MERGED_RISK_FLAGS = 8
MAX_MERGED_PRODUCTS = 200  # generous -- a real license schedule can run long


def _dedupe_products(all_products: list[ProductLineItem]) -> list[ProductLineItem]:
    """
    Overlap pages mean the same line item can legitimately show up in two
    consecutive chunks -- dedupe on (part number, product name) so it's
    counted once, while keeping distinct rows (different SKUs, or the same
    product across two different order forms) intact.
    """
    seen: set[tuple[str, str]] = set()
    deduped: list[ProductLineItem] = []

    for product in all_products:
        key = (
            (product.publisher_part_number or "").strip().lower(),
            (product.product_name or "").strip().lower(),
        )
        if key == ("", ""):
            continue  # skip entries with no identifying info at all
        if key in seen:
            continue
        seen.add(key)
        deduped.append(product)

    return deduped[:MAX_MERGED_PRODUCTS]


@dataclass
class MergeResult:
    merged: ContractExtraction
    conflicts: dict[str, list[str]] = dc_field(default_factory=dict)  # field -> conflicting values seen
    chunk_count: int = 1


def _dedupe_risk_flags(all_flags: list[RiskFlag]) -> list[RiskFlag]:
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    seen_clauses: set[str] = set()
    deduped: list[RiskFlag] = []

    for flag in sorted(all_flags, key=lambda f: severity_rank.get(f.severity.lower(), 3)):
        key = flag.clause.strip().lower()
        if key in seen_clauses:
            continue
        seen_clauses.add(key)
        deduped.append(flag)

    return deduped[:MAX_MERGED_RISK_FLAGS]


def _dedupe_key_clauses(all_clauses: list[KeyClause]) -> list[KeyClause]:
    """
    One clause per type, kept in catalogue order.

    Overlapping chunks mean the same clause can be captured twice; a long
    agreement can also cover a topic in two places. Where there's a choice,
    keep the entry with the fullest verbatim text -- for a reviewer reading
    clauses, more of the actual language is what makes the entry useful, and
    a truncated capture from a chunk boundary is the one to discard.
    """
    best: dict[str, KeyClause] = {}
    for clause in all_clauses:
        key = clause.clause_type
        existing = best.get(key)
        if existing is None or len(clause.text or "") > len(existing.text or ""):
            best[key] = clause

    order = {name: i for i, name in enumerate(CLAUSE_TYPES)}
    return sorted(best.values(), key=lambda c: order.get(c.clause_type, 999))


def merge_extractions(chunk_results: list[ContractExtraction]) -> MergeResult:
    if len(chunk_results) == 1:
        return MergeResult(merged=chunk_results[0], conflicts={}, chunk_count=1)

    merged_data: dict = {}
    conflicts: dict[str, list[str]] = {}

    for name in _FIELD_NAMES:
        chosen: ExtractedField | None = None
        seen_values: list[str] = []

        for result in chunk_results:
            candidate: ExtractedField | None = getattr(result, name, None)
            if candidate is not None and candidate.value:
                seen_values.append(candidate.value)
                if chosen is None:
                    chosen = candidate

        # flag genuine disagreements (different non-null values), not just
        # the same fact confirmed redundantly in an overlap page
        distinct_values = {v.strip().lower() for v in seen_values}
        if len(distinct_values) > 1:
            conflicts[name] = seen_values

        merged_data[name] = chosen

    all_flags = [flag for result in chunk_results for flag in result.risk_flags]
    merged_data["risk_flags"] = _dedupe_risk_flags(all_flags)

    all_products = [product for result in chunk_results for product in result.products]
    merged_data["products"] = _dedupe_products(all_products)

    all_clauses = [clause for result in chunk_results for clause in result.key_clauses]
    merged_data["key_clauses"] = _dedupe_key_clauses(all_clauses)

    # a field only counts as "not found" if it was null in every single chunk
    not_found_everywhere = set(_FIELD_NAMES)
    for result in chunk_results:
        for name in _FIELD_NAMES:
            candidate: ExtractedField | None = getattr(result, name, None)
            if candidate is not None and candidate.value:
                not_found_everywhere.discard(name)
    merged_data["fields_not_found"] = sorted(not_found_everywhere)

    # placeholder -- the caller (extract.py) replaces this with the output
    # of the reduce/synthesis LLM call, which has access to the full merged
    # facts. Kept non-empty here so the object is valid on its own.
    merged_data["plain_english_summary"] = " ".join(
        r.plain_english_summary for r in chunk_results
    )[:800]

    merged = ContractExtraction.model_validate(merged_data)
    return MergeResult(merged=merged, conflicts=conflicts, chunk_count=len(chunk_results))
