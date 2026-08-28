"""
Deterministic risk scoring for SAM vendor contracts.

WHY THIS EXISTS
---------------
Extraction and judgement are two different jobs, and only one of them
should be left to a language model.

Pulling "audit notice period: 10 days" out of a contract is extraction --
it is grounded in a quote, and the model is good at it. Deciding that
10 days is a HIGH risk is judgement, and if a model makes that call it
will make it slightly differently every run: different severity, different
wording, sometimes not at all. That is fine for brainstorming and
unacceptable in a client deliverable, where "why did this contract score
high last week and medium today?" has no good answer.

So this module takes the already-extracted, evidence-backed fields and
applies fixed, documented thresholds to them. Same contract in, same flags
out, every single time -- and when a client disagrees with a threshold you
change one number here instead of re-tuning a prompt and hoping.

The model still contributes flags for genuinely unusual clauses no
rulebook anticipates (see prompts.py). Those are kept, but tagged
source="ai" so a reviewer can see at a glance which flags carry a
deterministic guarantee and which are suggestions worth a second look.

TUNING
------
Every threshold lives in THRESHOLDS below. Change a number there and the
whole rulebook shifts with it -- no other file needs editing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from schema import ContractExtraction, Evidence, RiskFlag

# ---------------------------------------------------------------------------
# Tunable thresholds -- the whole rulebook's judgement lives here
# ---------------------------------------------------------------------------

THRESHOLDS = {
    # Auto-renewal: the shorter the window to give notice, the easier it is
    # to miss it and get locked into another full term by accident.
    "renewal_notice_high_days": 30,      # < 30 days to act -> high
    "renewal_notice_medium_days": 90,    # < 90 days -> medium

    # Audit rights: short notice leaves no time to prepare a position or
    # reconcile deployment data before the publisher arrives.
    "audit_notice_high_days": 15,        # < 15 days -> high
    "audit_notice_medium_days": 30,      # < 30 days -> medium

    # Uncapped price escalation on a renewing subscription is where
    # multi-year budget overruns come from.
    "price_cap_high_pct": 10.0,          # cap above 10% is barely a cap
    "price_cap_medium_pct": 5.0,

    # Early termination fee, as a share of total contract value.
    "etf_high_share_of_value": 0.25,     # ETF >= 25% of contract value -> high
}


# ---------------------------------------------------------------------------
# Value parsing helpers
# ---------------------------------------------------------------------------
# Extracted values are strings, because that is what a contract actually
# says: "thirty (30) days", "90 days prior", "5% per annum", "USD 1,200,000".
# These helpers pull a number out where one exists and return None where it
# genuinely doesn't -- never a guess, since a wrong number here would
# produce a confidently wrong severity.

# Longest-first so "forty-five" is tried before "forty", and matched on word
# boundaries only -- a naive substring check finds "ten" inside "written",
# which silently turns "thirty (30) days prior written notice" into 10 days.
_WORD_NUMBERS = [
    ("one hundred eighty", 180), ("one hundred twenty", 120),
    ("forty-five", 45), ("forty five", 45),
    ("ninety", 90), ("sixty", 60), ("fifty", 50), ("forty", 40),
    ("thirty", 30), ("twenty", 20), ("fifteen", 15), ("ten", 10), ("zero", 0),
]


def parse_days(value: str | None) -> int | None:
    """
    Pull a day count out of a phrase like '30 days', 'thirty (30) days
    prior written notice', or a bare '60'.

    Contracts often spell the number and then repeat it in digits --
    'thirty (30) days' -- so digits are preferred when present, with a
    spelled-word fallback for the cases that don't repeat it.
    """
    if not value:
        return None
    text = value.strip().lower()

    # Allow closing punctuation between the number and the unit, because
    # contracts routinely spell it then repeat it: "thirty (30) days".
    digit_match = re.search(
        r"(\d{1,4})\s*[)\]]?\s*(?:calendar|business|working)?\s*day", text
    )
    if digit_match:
        return int(digit_match.group(1))

    # a bare number with no unit, e.g. the model returned just "60"
    bare = re.fullmatch(r"\s*(\d{1,4})\s*", text)
    if bare:
        return int(bare.group(1))

    # months expressed as a period, converted for comparability
    month_match = re.search(r"(\d{1,2})\s*month", text)
    if month_match:
        return int(month_match.group(1)) * 30

    for word, number in _WORD_NUMBERS:
        if re.search(rf"\b{re.escape(word)}\b", text):
            return number

    return None


def parse_percent(value: str | None) -> float | None:
    """Pull a percentage out of e.g. 'capped at 7% annually' or 'CPI + 3%'."""
    if not value:
        return None
    match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", value)
    return float(match.group(1)) if match else None


def parse_money(value: str | None) -> float | None:
    """
    Pull a currency amount out of e.g. '$1,200,000', 'USD 450000.00',
    'EUR 2.5 million'. Currency-agnostic: this is only ever used to compare
    two figures from the SAME contract (fee vs. total value), so the unit
    cancels out and no FX conversion is needed or attempted.
    """
    if not value:
        return None
    text = value.replace(",", "").lower()

    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    amount = float(match.group(1))

    # "2.5 million" and "500k" both need scaling. A \b before the suffix
    # fails on "500k" (digit and letter are both word chars, so there is no
    # boundary between them), hence matching the suffix after the number.
    if re.search(r"\d\s*m(?:illion|n)?\b", text) or re.search(r"\bmillions?\b", text):
        amount *= 1_000_000
    elif re.search(r"\d\s*k\b", text) or re.search(r"\bthousand\b", text):
        amount *= 1_000

    return amount


def is_affirmative(value: str | None) -> bool:
    """True only for a clear yes. 'unclear' is deliberately NOT a yes --
    an ambiguous extraction should not silently trigger a hard flag."""
    return bool(value) and value.strip().lower() in {"yes", "true", "present", "y"}


def is_negative(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in {"no", "false", "absent", "none", "n"}


# ---------------------------------------------------------------------------
# Rule plumbing
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    """Convenience view over an extraction: value + evidence per field."""
    extraction: ContractExtraction

    def val(self, name: str) -> str | None:
        field = getattr(self.extraction, name, None)
        return field.value if field and field.value else None

    def ev(self, name: str) -> Evidence | None:
        field = getattr(self.extraction, name, None)
        return field.evidence if field and field.evidence else None


def _flag(rule_id: str, clause: str, severity: str, explanation: str,
          evidence: Evidence | None) -> RiskFlag:
    return RiskFlag(
        clause=clause,
        severity=severity,
        explanation=explanation,
        evidence=evidence,
        source="rule",
        rule_id=rule_id,
    )


# ---------------------------------------------------------------------------
# The rulebook
# ---------------------------------------------------------------------------
# Each rule is a small function taking the context and returning a RiskFlag
# or None. Keeping them separate (rather than one long if-chain) means each
# is independently readable and independently testable, and adding a new
# risk to the catalogue is a self-contained change.


def _rule_auto_renewal(ctx: _Ctx) -> RiskFlag | None:
    if not is_affirmative(ctx.val("auto_renewal")):
        return None

    days = parse_days(ctx.val("renewal_notice_period_days"))
    evidence = ctx.ev("renewal_notice_period_days") or ctx.ev("auto_renewal")

    if days is None:
        return _flag(
            "AUTO_RENEWAL_NO_WINDOW", "Auto-renewal", "high",
            "Contract auto-renews but no clear notice window was found -- confirm the "
            "deadline manually, as missing it locks in another full term.",
            evidence,
        )
    if days < THRESHOLDS["renewal_notice_high_days"]:
        return _flag(
            "AUTO_RENEWAL_SHORT_WINDOW", "Auto-renewal", "high",
            f"Auto-renews with only {days} days' notice required -- a very short window "
            "to decide and serve notice.",
            evidence,
        )
    if days < THRESHOLDS["renewal_notice_medium_days"]:
        return _flag(
            "AUTO_RENEWAL_MEDIUM_WINDOW", "Auto-renewal", "medium",
            f"Auto-renews with {days} days' notice required -- diarise the deadline well in advance.",
            evidence,
        )
    return _flag(
        "AUTO_RENEWAL_LONG_WINDOW", "Auto-renewal", "low",
        f"Auto-renews, but with a workable {days}-day notice window.",
        evidence,
    )


def _rule_audit_rights(ctx: _Ctx) -> RiskFlag | None:
    if not is_affirmative(ctx.val("audit_rights_present")):
        return None

    days = parse_days(ctx.val("audit_notice_period_days"))
    evidence = ctx.ev("audit_notice_period_days") or ctx.ev("audit_rights_present")

    if days is None:
        return _flag(
            "AUDIT_NO_NOTICE_PERIOD", "Audit rights", "high",
            "Publisher holds audit rights with no stated notice period -- effectively "
            "audit on demand.",
            evidence,
        )
    if days < THRESHOLDS["audit_notice_high_days"]:
        return _flag(
            "AUDIT_SHORT_NOTICE", "Audit rights", "high",
            f"Audit can be triggered on {days} days' notice -- too little time to reconcile "
            "deployment data before it starts.",
            evidence,
        )
    if days < THRESHOLDS["audit_notice_medium_days"]:
        return _flag(
            "AUDIT_MEDIUM_NOTICE", "Audit rights", "medium",
            f"Audit notice period is {days} days -- keep entitlement records continuously current.",
            evidence,
        )
    return _flag(
        "AUDIT_STANDARD_NOTICE", "Audit rights", "low",
        f"Standard audit rights with {days} days' notice.",
        evidence,
    )


def _rule_price_escalation(ctx: _Ctx) -> RiskFlag | None:
    cap_value = ctx.val("price_escalation_cap")
    evidence = ctx.ev("price_escalation_cap")

    # No cap found at all is only meaningful on a contract that actually
    # renews -- a one-off perpetual purchase has nothing to escalate.
    if not cap_value:
        renews = is_affirmative(ctx.val("auto_renewal"))
        subscription_like = "subscription" in (ctx.val("pricing_model") or "").lower()
        if renews or subscription_like:
            return _flag(
                "PRICE_NO_CAP", "Price escalation", "high",
                "No cap on price increases was found on a renewing contract -- renewal pricing "
                "is effectively at the vendor's discretion.",
                ctx.ev("auto_renewal") or ctx.ev("pricing_model"),
            )
        return None

    pct = parse_percent(cap_value)
    if pct is None:
        return _flag(
            "PRICE_CAP_UNQUANTIFIED", "Price escalation", "medium",
            "A price escalation cap exists but is not expressed as a clear percentage -- "
            "confirm how the ceiling is actually calculated.",
            evidence,
        )
    if pct > THRESHOLDS["price_cap_high_pct"]:
        return _flag(
            "PRICE_CAP_HIGH", "Price escalation", "high",
            f"Price increases are capped at {pct:g}%, well above typical inflation-linked caps.",
            evidence,
        )
    if pct > THRESHOLDS["price_cap_medium_pct"]:
        return _flag(
            "PRICE_CAP_MEDIUM", "Price escalation", "medium",
            f"Price increases are capped at {pct:g}% -- model this into multi-year budget forecasts.",
            evidence,
        )
    return _flag(
        "PRICE_CAP_LOW", "Price escalation", "low",
        f"Price increases are capped at a contained {pct:g}%.",
        evidence,
    )


def _rule_termination_for_convenience(ctx: _Ctx) -> RiskFlag | None:
    value = ctx.val("termination_for_convenience")
    evidence = ctx.ev("termination_for_convenience")

    if value is None:
        return _flag(
            "TFC_NOT_FOUND", "Termination for convenience", "medium",
            "No termination-for-convenience right was found -- the customer may be committed "
            "for the full term regardless of need.",
            None,
        )
    if is_negative(value):
        return _flag(
            "TFC_ABSENT", "Termination for convenience", "medium",
            "The contract expressly provides no termination-for-convenience right -- exit before "
            "term end may not be possible without cause.",
            evidence,
        )
    return None


def _rule_early_termination_fee(ctx: _Ctx) -> RiskFlag | None:
    fee_value = ctx.val("early_termination_fee")
    if not fee_value:
        return None

    evidence = ctx.ev("early_termination_fee")
    fee = parse_money(fee_value)
    total = parse_money(ctx.val("contract_value"))

    if fee is not None and total:
        share = fee / total
        if share >= THRESHOLDS["etf_high_share_of_value"]:
            return _flag(
                "ETF_LARGE", "Early termination fee", "high",
                f"Early termination fee is roughly {share:.0%} of total contract value -- a "
                "material barrier to exiting early.",
                evidence,
            )
        return _flag(
            "ETF_PRESENT_QUANTIFIED", "Early termination fee", "medium",
            f"An early termination fee applies (about {share:.0%} of contract value).",
            evidence,
        )

    return _flag(
        "ETF_PRESENT", "Early termination fee", "medium",
        "An early termination fee applies -- factor it into any early-exit or consolidation plan.",
        evidence,
    )


def _rule_true_up(ctx: _Ctx) -> RiskFlag | None:
    value = ctx.val("true_up_rights")
    if not value:
        return None
    lowered = value.lower()
    # A true-up obligation is normal; the risk is when it is retroactive or
    # priced at list, which is what turns an over-deployment into a penalty.
    if any(term in lowered for term in ("retroactive", "back-dated", "backdated", "list price", "undiscounted")):
        return _flag(
            "TRUE_UP_PUNITIVE", "True-up terms", "high",
            "True-up appears to be charged retroactively or at undiscounted list price -- "
            "over-deployment would be expensive to correct.",
            ctx.ev("true_up_rights"),
        )
    return None


def _rule_missing_term_end(ctx: _Ctx) -> RiskFlag | None:
    """A contract with no findable end date can't be managed for renewal at all."""
    if ctx.val("term_end_date") or ctx.val("term_length"):
        return None
    return _flag(
        "TERM_END_UNKNOWN", "Contract term", "medium",
        "No term end date or term length could be located -- renewal cannot be tracked "
        "until this is confirmed from the source document.",
        None,
    )


RULES = [
    _rule_auto_renewal,
    _rule_audit_rights,
    _rule_price_escalation,
    _rule_termination_for_convenience,
    _rule_early_termination_fee,
    _rule_true_up,
    _rule_missing_term_end,
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def evaluate_rules(extraction: ContractExtraction) -> list[RiskFlag]:
    """Run the full rulebook. Deterministic: same input -> same output."""
    ctx = _Ctx(extraction)
    flags: list[RiskFlag] = []
    for rule in RULES:
        try:
            result = rule(ctx)
        except Exception:
            # One malformed field must never take down the whole scoring
            # pass -- a missing flag is recoverable, a crashed analysis is not.
            continue
        if result is not None:
            flags.append(result)
    return sorted(flags, key=lambda f: SEVERITY_RANK.get(f.severity.lower(), 3))


def apply_risk_rules(extraction: ContractExtraction, max_ai_flags: int = 4) -> ContractExtraction:
    """
    Replaces the extraction's risk_flags with: every deterministic rule flag,
    followed by the model's own flags for anything the rulebook doesn't cover.

    AI flags are kept only when they (a) carry supporting evidence and (b)
    don't duplicate a clause the rulebook already ruled on -- otherwise the
    same clause would appear twice with two different severities, which is
    exactly the inconsistency this module exists to remove.
    """
    rule_flags = evaluate_rules(extraction)
    covered_clauses = {f.clause.strip().lower() for f in rule_flags}

    ai_flags: list[RiskFlag] = []
    for flag in extraction.risk_flags:
        if flag.source == "rule":
            continue  # never re-admit a stale rule flag from a previous pass
        if flag.clause.strip().lower() in covered_clauses:
            continue  # rulebook owns this clause
        if not (flag.evidence and flag.evidence.quote):
            continue  # unsourced flag is a guess -- drop it
        flag.source = "ai"
        ai_flags.append(flag)

    ai_flags.sort(key=lambda f: SEVERITY_RANK.get(f.severity.lower(), 3))
    extraction.risk_flags = rule_flags + ai_flags[:max_ai_flags]
    return extraction


def risk_summary(flags: list[RiskFlag]) -> dict:
    """Counts by severity plus an overall posture, for the UI header and PDF."""
    counts = {"high": 0, "medium": 0, "low": 0}
    for flag in flags:
        sev = flag.severity.lower()
        if sev in counts:
            counts[sev] += 1

    if counts["high"] >= 3:
        posture = "Elevated"
    elif counts["high"] >= 1:
        posture = "Attention needed"
    elif counts["medium"] >= 1:
        posture = "Manageable"
    else:
        posture = "Low concern"

    return {
        "counts": counts,
        "posture": posture,
        "total": len(flags),
        "rule_based": sum(1 for f in flags if f.source == "rule"),
        "ai_suggested": sum(1 for f in flags if f.source != "rule"),
    }
