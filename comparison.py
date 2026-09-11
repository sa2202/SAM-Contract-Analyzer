"""
Side-by-side comparison of the same term across every contract in a batch.

WHY THIS EXISTS
---------------
The clause finder answers "what does each contract say about audit rights?"
by stacking the clauses vertically. That is the right shape for reading the
actual language, and the wrong shape for the question a SAM reviewer asks
next: "which of these is the worst?"

Answering that from stacked prose means holding five numbers in your head
while scrolling. This module pulls the comparable value out of each contract
and puts them in one row, so 10 / 45 / 30 / 30 / 7 days is a single glance
rather than five paragraphs.

WHAT IT DOES NOT DO
-------------------
It does not re-read the contracts or call the model. Every value here is
already extracted; this only reorganises it. That matters because a
comparison is only as trustworthy as its inputs, and reusing the extracted
values means the page numbers and quotes behind them still apply --- a
reviewer can check any cell against the source.
"""

from __future__ import annotations

from dataclasses import dataclass

from risk_rules import parse_days, parse_money, parse_percent


@dataclass
class ComparisonCell:
    """One contract's answer for one comparison row."""
    raw: str | None          # the extracted text, verbatim
    numeric: float | None    # parsed value for ranking, None if unparseable
    page: int | None
    quote: str | None

    @property
    def display(self) -> str:
        return self.raw if self.raw else "—"

    @property
    def found(self) -> bool:
        return bool(self.raw)


@dataclass
class ComparisonRow:
    """One term, compared across every contract."""
    label: str
    cells: list[ComparisonCell]
    # "low" = a smaller number is worse for the customer (audit notice: 7 days
    # is worse than 45). "high" = a bigger number is worse (price cap: 10% is
    # worse than 3%). None = not rankable, show values without highlighting.
    worse_direction: str | None
    note: str | None = None
    # For some terms, having NO value is the worst case rather than a gap in
    # the data. A missing price-escalation cap means increases are unlimited,
    # which is worse than any stated cap -- so ranking only the contracts
    # that HAVE a cap would flag the mildest one as "worst" while two
    # contracts with unlimited increases sat unmarked. That is not a cosmetic
    # error; it points the reviewer at the wrong contract.
    absent_is_worst: bool = False

    @property
    def any_found(self) -> bool:
        return any(c.found for c in self.cells)

    def worst_index(self) -> int | None:
        """
        Index of the least favourable contract, or None if it can't be
        determined. Deliberately conservative: with fewer than two comparable
        values there is no comparison to make, and marking one cell as
        "worst" out of one would imply an analysis that didn't happen.
        """
        if not self.worse_direction:
            return None

        # Absence beats any number where absence is the bad case -- see the
        # absent_is_worst comment above.
        if self.absent_is_worst:
            missing = [i for i, c in enumerate(self.cells) if not c.found]
            if missing and len(self.cells) > 1:
                return missing[0]

        scored = [(i, c.numeric) for i, c in enumerate(self.cells) if c.numeric is not None]
        if len(scored) < 2:
            return None
        if self.worse_direction == "low":
            return min(scored, key=lambda pair: pair[1])[0]
        return max(scored, key=lambda pair: pair[1])[0]

    def absent_indices(self) -> list[int]:
        """Contracts where this term is missing AND that absence is the risk."""
        if not self.absent_is_worst:
            return []
        return [i for i, c in enumerate(self.cells) if not c.found]

    def spread(self) -> str | None:
        """A one-line summary of the range, e.g. '7 to 45 days'."""
        values = [c.numeric for c in self.cells if c.numeric is not None]
        if len(values) < 2:
            return None
        low, high = min(values), max(values)
        if low == high:
            return None
        fmt = lambda v: f"{v:g}"
        return f"{fmt(low)} to {fmt(high)}"


# ---------------------------------------------------------------------------
# What gets compared
# ---------------------------------------------------------------------------
# Each entry: (label, schema field, parser, worse_direction, note)
#
# Only fields where a side-by-side actually answers something are included.
# Vendor name and contract title differ by definition and comparing them says
# nothing; they identify the columns instead.

_DAYS = "days"
_PCT = "percent"
_MONEY = "money"
_TEXT = None

# (label, schema field, parser, worse_direction, absent_is_worst, note)
COMPARISON_SPEC = [
    ("Audit notice period", "audit_notice_period_days", _DAYS, "low", False,
     "Shorter notice means less time to reconcile deployment data before an audit starts."),
    ("Audit frequency", "audit_frequency", _TEXT, None, False, None),
    ("Renewal notice period", "renewal_notice_period_days", _DAYS, "low", False,
     "Shorter windows are easier to miss, which locks in another full term."),
    ("Auto-renewal", "auto_renewal", _TEXT, None, False, None),
    ("Term end date", "term_end_date", _TEXT, None, False, None),
    ("Price escalation cap", "price_escalation_cap", _PCT, "high", True,
     "A higher cap allows steeper increases at renewal — and NO cap means "
     "increases are unlimited, which is worse than any stated cap."),
    ("Termination for convenience", "termination_for_convenience", _TEXT, None, False, None),
    ("Early termination fee", "early_termination_fee", _MONEY, "high", False, None),
    ("Contract value", "contract_value", _MONEY, None, False, None),
    ("Licence metric", "license_metric", _TEXT, None, False, None),
    ("True-up rights", "true_up_rights", _TEXT, None, False, None),
    ("Payment terms", "payment_terms", _TEXT, None, False, None),
    ("SLA", "sla_summary", _TEXT, None, False, None),
    ("Data exit / transition", "data_exit_transition_period", _DAYS, "low", False, None),
]

_PARSERS = {_DAYS: parse_days, _PCT: parse_percent, _MONEY: parse_money}


def _cell_for(extraction, attr: str, parser_kind) -> ComparisonCell:
    field = getattr(extraction, attr, None)
    if field is None or not field.value:
        return ComparisonCell(raw=None, numeric=None, page=None, quote=None)

    parser = _PARSERS.get(parser_kind)
    numeric = None
    if parser:
        try:
            numeric = parser(field.value)
        except Exception:
            # A cell that won't parse still shows its text; only the ranking
            # is lost. Failing the whole comparison over one odd value would
            # be a poor trade.
            numeric = None

    evidence = field.evidence
    return ComparisonCell(
        raw=field.value,
        numeric=float(numeric) if numeric is not None else None,
        page=evidence.page if evidence else None,
        quote=evidence.quote if evidence else None,
    )


def build_comparison(results, include_empty: bool = False) -> list[ComparisonRow]:
    """
    Build the comparison table from a list of successful ContractResults.

    `include_empty` keeps rows where no contract had a value. Off by default:
    a table of dashes is noise. But an absent term is sometimes the finding
    itself, which is why it's available at all.
    """
    rows = []
    for label, attr, parser_kind, direction, absent_worst, note in COMPARISON_SPEC:
        cells = [_cell_for(r.extraction, attr, parser_kind) for r in results]
        row = ComparisonRow(
            label=label, cells=cells, worse_direction=direction,
            note=note, absent_is_worst=absent_worst,
        )
        if row.any_found or include_empty:
            rows.append(row)
    return rows


def clause_comparison(results, clause_type: str) -> list[dict]:
    """
    Per-contract view of ONE clause type, for a side-by-side of the actual
    language rather than a single extracted value.
    """
    out = []
    for result in results:
        clause = next(
            (c for c in result.extraction.key_clauses if c.clause_type == clause_type),
            None,
        )
        out.append({
            "vendor": result.vendor,
            "filename": result.filename,
            "heading": clause.heading if clause else None,
            "text": clause.text if clause else None,
            "summary": clause.summary if clause else None,
            "page": clause.page if clause else None,
            "found": clause is not None,
        })
    return out


def comparison_headline(row: ComparisonRow, results) -> str | None:
    """
    One sentence stating what the row shows, e.g.
    "Ranges from 7 days (Initrode) to 45 days (Initech)."

    Written only when there are at least two comparable numbers -- otherwise
    there is no range to describe and the sentence would be filler.
    """
    # Where absence is the risk, say so FIRST. "Ranges from 4% to 5%" is
    # technically true and badly misleading when two other contracts have no
    # cap at all -- it describes the mild cases and omits the severe ones.
    absent = row.absent_indices()
    absent_note = ""
    if absent:
        names = ", ".join(results[i].vendor for i in absent)
        absent_note = (
            f"No cap found in {names} — increases there are uncapped. "
            if len(absent) > 1 else
            f"No cap found in {names} — increases there are uncapped. "
        )

    scored = [(i, c.numeric) for i, c in enumerate(row.cells) if c.numeric is not None]
    if len(scored) < 2:
        return absent_note.strip() or None

    low_i, low_v = min(scored, key=lambda p: p[1])
    high_i, high_v = max(scored, key=lambda p: p[1])
    if low_v == high_v:
        return absent_note + f"The other {len(scored)} agree at {row.cells[low_i].display}."

    return (
        absent_note
        + f"Ranges from {row.cells[low_i].display} ({results[low_i].vendor}) "
          f"to {row.cells[high_i].display} ({results[high_i].vendor})."
    )
