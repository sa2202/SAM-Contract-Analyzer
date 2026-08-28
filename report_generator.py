"""
Generates a clean, formatted PDF report from a ContractExtraction result.

This is the "download your results" deliverable -- a polished single PDF a
person can save, forward, or drop into a vendor file, rather than raw JSON.
Built with reportlab's platypus layer (not raw canvas drawing) so sections,
tables and page breaks are handled for us.
"""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    KeepTogether,
)

from risk_rules import risk_summary
from schema import ContractExtraction, ExtractedField, ProductLineItem

from xml.sax.saxutils import escape as _xml_escape


def esc(value) -> str:
    """
    Escape text before it goes into a reportlab Paragraph.

    reportlab parses Paragraph content as a small XML dialect, so contract
    text is not inert data to it -- anything resembling a tag is interpreted.
    A clause containing "<REDACTED>" or a comparison like "if headcount <b
    then" raises "parse ended with 1 unclosed tags" and kills the entire PDF.
    Not a corrupted paragraph: no report at all.

    This became load-bearing when verbatim clause text started going into the
    PDF. Short evidence quotes rarely contained angle brackets; a 120-word
    passage of real contract language is a much better bet to.
    """
    if value is None:
        return ""
    return _xml_escape(str(value))



# ---------------------------------------------------------------------------
# Palette (matches the Streamlit app's ledger/audit theme)
# ---------------------------------------------------------------------------

INK = colors.HexColor("#1C2B33")
MUTED = colors.HexColor("#5B6B66")
ACCENT = colors.HexColor("#2B6E63")
BORDER = colors.HexColor("#D7DED9")
RISK_HIGH = colors.HexColor("#B23A32")
RISK_MEDIUM = colors.HexColor("#C98A2C")
RISK_LOW = colors.HexColor("#3F7D5C")

RISK_COLOR = {"high": RISK_HIGH, "medium": RISK_MEDIUM, "low": RISK_LOW}


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle(
        name="ReportTitle", fontName="Helvetica-Bold", fontSize=20,
        textColor=INK, spaceAfter=10, leading=24,
    ))
    ss.add(ParagraphStyle(
        name="ReportSubtitle", fontName="Helvetica", fontSize=9,
        textColor=MUTED, spaceAfter=18, leading=12,
    ))
    ss.add(ParagraphStyle(
        name="SectionHeading", fontName="Helvetica-Bold", fontSize=12,
        textColor=ACCENT, spaceBefore=16, spaceAfter=8,
    ))
    ss.add(ParagraphStyle(
        name="FieldLabel", fontName="Helvetica-Bold", fontSize=8.5,
        textColor=MUTED,
    ))
    ss.add(ParagraphStyle(
        name="FieldValue", fontName="Helvetica", fontSize=10,
        textColor=INK, spaceAfter=6,
    ))
    ss.add(ParagraphStyle(
        name="Evidence", fontName="Helvetica-Oblique", fontSize=8,
        textColor=MUTED, spaceAfter=10,
    ))
    ss.add(ParagraphStyle(
        name="BodyTextTight", fontName="Helvetica", fontSize=10,
        textColor=INK, leading=14,
    ))
    ss.add(ParagraphStyle(
        name="Disclaimer", fontName="Helvetica-Oblique", fontSize=7.5,
        textColor=MUTED,
    ))
    ss.add(ParagraphStyle(
        name="VerdictLabel", fontName="Helvetica-Bold", fontSize=7.5,
        textColor=MUTED, spaceAfter=2,
    ))
    ss.add(ParagraphStyle(
        name="VerdictHeadline", fontName="Helvetica-Bold", fontSize=15,
        textColor=INK, spaceAfter=4, leading=18,
    ))
    ss.add(ParagraphStyle(
        name="SourceTag", fontName="Helvetica-Oblique", fontSize=7.5,
        textColor=MUTED, spaceAfter=6,
    ))
    return ss


def _field_block(styles, label: str, field: ExtractedField | None):
    """Renders one labeled field + its evidence quote, or nothing if empty."""
    flow = []
    if field is None or not field.value:
        return flow
    flow.append(Paragraph(label.upper(), styles["FieldLabel"]))
    flow.append(Paragraph(esc(field.value), styles["FieldValue"]))
    if field.evidence and field.evidence.quote:
        page_note = f" (p. {field.evidence.page})" if field.evidence.page else ""
        flow.append(Paragraph(f'&ldquo;{esc(field.evidence.quote)}&rdquo;{page_note}', styles["Evidence"]))
    return flow


def _products_table(products: list[ProductLineItem]):
    """
    Renders the product/license line-item table. Uses Paragraph cells
    (rather than plain strings) so long product names wrap instead of
    overflowing -- 9 columns on a letter-width page leaves little room per
    column, so wrapping is essential rather than cosmetic.
    """
    header_style = ParagraphStyle(
        name="TableHeader", fontName="Helvetica-Bold", fontSize=6.5,
        textColor=colors.white, leading=8,
    )
    cell_style = ParagraphStyle(
        name="TableCell", fontName="Helvetica", fontSize=6.5,
        textColor=INK, leading=8,
    )

    headers = [
        "Part #", "Product Name", "License Type", "License Metric",
        "Purchased Rights", "Unit Cost", "Start Date", "End Date", "Country",
    ]
    table_data = [[Paragraph(h, header_style) for h in headers]]

    for p in products:
        row_values = [
            p.publisher_part_number, p.product_name, p.license_type,
            p.license_metric, p.purchased_rights, p.unit_cost,
            p.start_date, p.end_date, p.country_of_agreement,
        ]
        table_data.append([Paragraph(esc(v) or "—", cell_style) for v in row_values])

    col_widths = [
        0.7 * inch, 1.3 * inch, 0.65 * inch, 0.75 * inch, 0.75 * inch,
        0.6 * inch, 0.55 * inch, 0.55 * inch, 0.55 * inch,
    ]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F9F7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    return [table, Spacer(1, 10)]


def generate_pdf_report(extraction: ContractExtraction, source_filename: str) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
    )
    styles = _styles()
    story = []

    # --- Header ---
    title = extraction.vendor_name.value if extraction.vendor_name and extraction.vendor_name.value else "Vendor"
    story.append(Paragraph(f"Contract Analysis: {esc(title)}", styles["ReportTitle"]))
    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    story.append(Paragraph(
        f"Source document: {esc(source_filename)} &nbsp;|&nbsp; Generated {generated} &nbsp;|&nbsp; "
        f"SAM Contract Analyzer",
        styles["ReportSubtitle"],
    ))
    story.append(HRFlowable(width="100%", color=BORDER, thickness=1))

    # --- Risk posture panel: the headline a reader needs before the detail ---
    summary_stats = risk_summary(extraction.risk_flags)
    counts = summary_stats["counts"]
    posture_color = {
        "Elevated": RISK_HIGH, "Attention needed": RISK_MEDIUM,
        "Manageable": RISK_MEDIUM, "Low concern": RISK_LOW,
    }.get(summary_stats["posture"], MUTED)

    verdict = Table(
        [[
            Paragraph("RISK POSTURE", styles["VerdictLabel"]),
        ], [
            Paragraph(
                f'<font color="{posture_color.hexval()}">{summary_stats["posture"]}</font>',
                styles["VerdictHeadline"],
            ),
        ], [
            Paragraph(
                f'<font color="{RISK_HIGH.hexval()}"><b>{counts["high"]} high</b></font> &nbsp;·&nbsp; '
                f'<font color="{RISK_MEDIUM.hexval()}"><b>{counts["medium"]} medium</b></font> &nbsp;·&nbsp; '
                f'<font color="{RISK_LOW.hexval()}"><b>{counts["low"]} low</b></font> &nbsp;·&nbsp; '
                f'{summary_stats["rule_based"]} rule-based, {summary_stats["ai_suggested"]} AI-suggested',
                styles["BodyTextTight"],
            ),
        ]],
        colWidths=[6.25 * inch],
    )
    verdict.setStyle(TableStyle([
        ("LINEBEFORE", (0, 0), (0, -1), 3, posture_color),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(Spacer(1, 12))
    story.append(verdict)
    story.append(Spacer(1, 6))

    # --- Summary ---
    story.append(Paragraph("Summary", styles["SectionHeading"]))
    story.append(Paragraph(esc(extraction.plain_english_summary), styles["BodyTextTight"]))

    # --- Risk flags, split by how they were produced ---
    # Rule-based findings are reproducible; AI-surfaced ones are prompts for
    # a human read. Collapsing the two into one list would let a suggestion
    # inherit the authority of a deterministic finding, so the report keeps
    # them under separate headings with the distinction stated once each.
    def _flag_rows(flags):
        rows = []
        for flag in flags:
            sev = (flag.severity or "").lower()
            color = RISK_COLOR.get(sev, MUTED)
            body = f"<b>{esc(flag.clause)}</b> &mdash; {esc(flag.explanation)}"
            if flag.evidence and flag.evidence.quote:
                page_note = f" (p. {flag.evidence.page})" if flag.evidence.page else ""
                body += (
                    f'<br/><font size="7.5" color="{MUTED.hexval()}">'
                    f"&ldquo;{esc(flag.evidence.quote)}&rdquo;{page_note}</font>"
                )
            row = Table(
                [[Paragraph(f'<font color="{color.hexval()}"><b>{sev.upper()}</b></font>', styles["FieldValue"]),
                  Paragraph(body, styles["FieldValue"])]],
                colWidths=[0.9 * inch, 5.35 * inch],
            )
            row.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, BORDER),
            ]))
            rows.append(row)
        return rows

    rule_flags = [f for f in extraction.risk_flags if getattr(f, "source", "ai") == "rule"]
    ai_flags = [f for f in extraction.risk_flags if getattr(f, "source", "ai") != "rule"]

    if rule_flags:
        story.append(Paragraph("Risk Flags", styles["SectionHeading"]))
        story.append(Paragraph(
            "Scored by fixed rules against the extracted terms — deterministic and "
            "reproducible for this contract.", styles["SourceTag"],
        ))
        story.extend(_flag_rows(rule_flags))
        story.append(Spacer(1, 8))

    if ai_flags:
        story.append(Paragraph("Unusual Clauses Worth a Read", styles["SectionHeading"]))
        story.append(Paragraph(
            "Surfaced by the model as outside the standard rulebook. Prompts for human "
            "review, not settled findings.", styles["SourceTag"],
        ))
        story.extend(_flag_rows(ai_flags))
        story.append(Spacer(1, 8))

    # --- Products / license line items ---
    # Wrapped in KeepTogether so the heading can't be left stranded at the
    # foot of a page with its table starting overleaf. repeatRows=1 on the
    # table itself already carries the column headers onto continuation
    # pages, so a genuinely long schedule still splits readably.
    if extraction.products:
        story.append(KeepTogether([
            Paragraph("Products / License Line Items", styles["SectionHeading"]),
            *_products_table(extraction.products),
        ]))

    # --- Sections of extracted fields ---
    sections = [
        ("Identification", [
            ("Vendor", extraction.vendor_name),
            ("Customer", extraction.customer_name),
            ("Contract title", extraction.contract_title),
        ]),
        ("Term & Renewal", [
            ("Effective date", extraction.effective_date),
            ("Term end date", extraction.term_end_date),
            ("Term length", extraction.term_length),
            ("Auto-renewal", extraction.auto_renewal),
            ("Renewal notice period", extraction.renewal_notice_period_days),
        ]),
        ("Commercial Terms", [
            ("Contract value", extraction.contract_value),
            ("Pricing model", extraction.pricing_model),
            ("Price escalation cap", extraction.price_escalation_cap),
            ("Payment terms", extraction.payment_terms),
        ]),
        ("License & Entitlement", [
            ("License metric", extraction.license_metric),
            ("True-up rights", extraction.true_up_rights),
        ]),
        ("Compliance & Audit", [
            ("Audit rights present", extraction.audit_rights_present),
            ("Audit notice period", extraction.audit_notice_period_days),
            ("Audit frequency", extraction.audit_frequency),
        ]),
        ("Termination", [
            ("Termination for convenience", extraction.termination_for_convenience),
            ("Termination for cause", extraction.termination_for_cause),
            ("Early termination fee", extraction.early_termination_fee),
        ]),
        ("SLA & Exit", [
            ("SLA summary", extraction.sla_summary),
            ("Data exit / transition period", extraction.data_exit_transition_period),
        ]),
    ]

    for section_name, fields in sections:
        rendered = []
        for label, field in fields:
            rendered.extend(_field_block(styles, label, field))
        if rendered:  # skip sections where every field was empty
            story.append(Paragraph(section_name, styles["SectionHeading"]))
            story.extend(rendered)

    # --- Fields not found ---
    if extraction.fields_not_found:
        story.append(Paragraph("Not Found in This Document", styles["SectionHeading"]))
        story.append(Paragraph(
            "The following were not mentioned or could not be confidently located in the "
            "source text: " + ", ".join(f.replace("_", " ") for f in extraction.fields_not_found),
            styles["BodyTextTight"],
        ))

    # --- Footer / disclaimer ---
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", color=BORDER, thickness=0.5))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "This report was generated by an AI extraction tool and is provided for informational "
        "purposes only. It is not legal advice. Always verify extracted terms against the "
        "original signed contract before making decisions.",
        styles["Disclaimer"],
    ))

    doc.build(story)
    return buffer.getvalue()
