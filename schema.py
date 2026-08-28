"""
Structured extraction schema for SAM vendor contracts.

This defines exactly what fields the LLM must return. Every field is
Optional because contracts vary wildly -- a field genuinely not present in
the source document should come back as null, never guessed.

Each extracted fact also carries an `evidence` object (page number + a short
verbatim snippet) so the field can be traced back to the source text. This
is what lets the tool avoid silently hallucinating a renewal date or a
dollar figure.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    page: Optional[int] = Field(None, description="Page number the fact was found on")
    quote: Optional[str] = Field(
        None,
        description="Short verbatim snippet (<= 25 words) from the source text supporting this field",
    )


class ExtractedField(BaseModel):
    """Wraps any extracted value together with its source evidence."""
    value: Optional[str] = None
    evidence: Optional[Evidence] = None


class RiskFlag(BaseModel):
    """
    One flagged clause.

    `source` records HOW the flag was produced, and it matters a lot for
    trust: "rule" flags come from risk_rules.py applying fixed, documented
    thresholds to already-extracted fields, so they are deterministic and
    reproducible -- the same contract always yields the same rule flags.
    "ai" flags come from the model's own judgement over the contract text,
    which catches unusual clauses the rulebook doesn't cover but is not
    guaranteed to repeat identically run-to-run. The UI and the PDF label
    the two differently so a reviewer knows which is which.
    """
    clause: str = Field(..., description="Short name of the clause, e.g. 'Auto-renewal'")
    severity: str = Field(..., description="One of: low, medium, high")
    explanation: str = Field(..., description="One sentence on why this is flagged")
    evidence: Optional[Evidence] = None
    source: str = Field("ai", description="'rule' (deterministic) or 'ai' (model judgement)")
    rule_id: Optional[str] = Field(None, description="Identifier of the rule that fired, for rule-sourced flags")


# The fixed catalogue of clauses the tool hunts for. Fixed on purpose: a
# free-form "pull out anything important" produces a different set of clause
# names for every contract, which makes them impossible to line up side by
# side. A closed list means clause X in contract A sits in the same row as
# clause X in contract B, which is the entire point of a clause finder.
CLAUSE_TYPES = [
    "License grant",
    "License restrictions",
    "Deployment / virtualisation rights",
    "Audit rights",
    "True-up / over-deployment",
    "Term & renewal",
    "Termination",
    "Pricing & payment",
    "Price increases",
    "Support & maintenance",
    "Service levels (SLA)",
    "Assignment / change of control",
    "Transfer & resale",
    "Limitation of liability",
    "Indemnity",
    "Confidentiality",
    "Data protection",
    "Exit & transition",
    "Governing law",
    "Warranty",
]


class KeyClause(BaseModel):
    """
    One important clause, captured in enough of its own words to be read
    without opening the source document.

    This is deliberately different from `Evidence`, which caps quotes at 25
    words because it exists to prove a single extracted value. Someone
    reviewing contracts for clauses needs the clause itself -- the actual
    obligation, its carve-outs and its conditions -- so `text` allows a
    fuller passage. It stays verbatim: the paraphrase goes in `summary`, and
    the two are kept apart so nobody mistakes a summary for the contract's
    own language.
    """
    clause_type: str = Field(..., description="One of the entries in CLAUSE_TYPES")
    heading: Optional[str] = Field(None, description="The clause's own section number/heading, e.g. '11.3 Audit'")
    text: Optional[str] = Field(None, description="Verbatim clause text, up to ~120 words")
    summary: Optional[str] = Field(None, description="One plain-English sentence on what it means in practice")
    page: Optional[int] = None


class ProductLineItem(BaseModel):
    """
    One line item from a contract's order form / license schedule / SKU
    table -- the actual products/licenses being purchased, as opposed to
    the top-level contract terms captured elsewhere in this schema.

    Uses a single evidence reference per row (rather than per field, like
    the top-level fields do) since a line item's fields typically all come
    from the same table row -- one page/quote pointing at that row is
    enough to verify the whole line, and asking for 9 separate evidence
    quotes per product would balloon token usage for little extra value.
    """
    publisher_part_number: Optional[str] = None
    product_name: Optional[str] = None
    license_type: Optional[str] = Field(None, description="'subscription' | 'perpetual' | 'unclear'")
    license_metric: Optional[str] = Field(None, description="e.g. per-user, per-core, concurrent")
    purchased_rights: Optional[str] = Field(None, description="quantity/entitlement, e.g. '500 users', '100 cores'")
    unit_cost: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    country_of_agreement: Optional[str] = None
    evidence: Optional[Evidence] = None


class ContractExtraction(BaseModel):
    # --- Identification ---
    vendor_name: Optional[ExtractedField] = None
    customer_name: Optional[ExtractedField] = None
    contract_title: Optional[ExtractedField] = None

    # --- Term & renewal ---
    effective_date: Optional[ExtractedField] = None
    term_end_date: Optional[ExtractedField] = None
    term_length: Optional[ExtractedField] = None
    auto_renewal: Optional[ExtractedField] = None            # "yes" / "no" / "unclear"
    renewal_notice_period_days: Optional[ExtractedField] = None

    # --- Commercial terms ---
    contract_value: Optional[ExtractedField] = None
    pricing_model: Optional[ExtractedField] = None            # per-seat, per-core, flat fee, etc.
    price_escalation_cap: Optional[ExtractedField] = None
    payment_terms: Optional[ExtractedField] = None

    # --- License / entitlement ---
    license_metric: Optional[ExtractedField] = None           # per-user, per-device, concurrent, etc.
    true_up_rights: Optional[ExtractedField] = None

    # --- Compliance & risk ---
    audit_rights_present: Optional[ExtractedField] = None     # "yes" / "no" / "unclear"
    audit_notice_period_days: Optional[ExtractedField] = None
    audit_frequency: Optional[ExtractedField] = None

    # --- Termination ---
    termination_for_convenience: Optional[ExtractedField] = None
    termination_for_cause: Optional[ExtractedField] = None
    early_termination_fee: Optional[ExtractedField] = None

    # --- SLA & exit ---
    sla_summary: Optional[ExtractedField] = None
    data_exit_transition_period: Optional[ExtractedField] = None

    # --- Key clauses (the clause finder) ---
    key_clauses: list[KeyClause] = Field(
        default_factory=list,
        description="Important clauses captured verbatim, one entry per clause found",
    )

    # --- Product / line-item table ---
    products: list[ProductLineItem] = Field(
        default_factory=list,
        description="Line items from an order form, license schedule, or SKU table, if present",
    )

    # --- Narrative + risk ---
    # NOT required. A required field here means one omission from the model
    # discards an otherwise complete extraction -- 22 good fields, the clauses
    # and the whole licence schedule thrown away because a summary sentence
    # was missing. The summary is the least load-bearing thing in the object;
    # it should never be the reason an extraction fails.
    plain_english_summary: str = Field(
        default="",
        description="3-5 sentence plain-English summary of the contract",
    )
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    fields_not_found: list[str] = Field(
        default_factory=list,
        description="Names of schema fields that could not be found in the document at all",
    )
