"""
SAM Contract Analyzer -- Streamlit app.

One job: a reviewer drops in a folder of contracts (PDF, Word, vendor
spreadsheets, or photos of pages) and gets back the two things that would
otherwise take a day by hand --

  1. an entitlement summary: every licence line item across every contract,
     in one table, ready for an ELP;
  2. a clause finder: the important clauses pulled out of every contract
     verbatim and grouped by type, so "what do all of these say about audit
     rights?" is one click instead of twelve documents.

LAYOUT: one summary bar, then tabs. Each tab answers exactly one question and
nothing is repeated between them. An earlier version stacked a batch banner on
a per-contract banner with two sets of counters and read as a wall -- you
couldn't tell which numbers described what.

ESCAPING: every piece of contract-derived text rendered into an HTML block
goes through h(). Streamlit's unsafe_allow_html means a clause containing an
angle bracket would otherwise be parsed as markup -- silently swallowing the
rest of the card. See h() for why this matters more here than in most apps.

STORAGE: none. Uploads live in a temp directory deleted as soon as their text
has been read; results live in st.session_state and vanish with the tab.
"""

from __future__ import annotations

import html
import os
from datetime import datetime

import streamlit as st

# Streamlit Cloud exposes secrets via st.secrets; locally extract.py reads a
# .env file. Copying secrets into os.environ makes both paths identical.
try:
    for _key in ("GEMINI_API_KEY", "GEMINI_MODEL"):
        if _key in st.secrets:
            os.environ[_key] = st.secrets[_key]
except Exception:
    pass

from batch import analyze_batch
from entitlement import generate_batch_workbook
from parser import SUPPORTED_SUFFIXES, SessionWorkspace
from report_generator import generate_pdf_report
from risk_rules import THRESHOLDS, risk_summary
from schema import CLAUSE_TYPES

st.set_page_config(
    page_title="SAM Contract Analyzer",
    page_icon="📑",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def h(value) -> str:
    """
    Escape contract-derived text before it goes into an HTML block.

    Not defensive boilerplate. Every card below is rendered with
    unsafe_allow_html=True, which means the browser parses the string as
    markup -- so a clause reading "if headcount <b then" opens a bold tag that
    never closes, and everything after it in the card disappears. Contract
    language contains angle brackets ("<REDACTED>", "<30 days") and ampersands
    ("S&S", "R&D") often enough that this is a matter of when, not if.

    The failure mode is what makes it serious: nothing errors. The clause is
    simply not on screen, and a reviewer trusting this tool to surface clauses
    has no way to know one went missing.
    """
    if value is None:
        return ""
    return html.escape(str(value))


CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }
h1, h2, h3, h4, h5 {
    font-family: 'Source Serif 4', serif !important;
    letter-spacing: -0.01em;
}

/* --- Header --- */
.eyebrow {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem; letter-spacing: 0.16em; text-transform: uppercase;
    color: #4A9382; margin-bottom: 0.1rem;
}
.subtitle { color: #7C8A85; font-size: 0.95rem; margin: -0.5rem 0 1.4rem 0; max-width: 62ch; }

/* --- Summary bar: one row of numbers, shown once --- */
.bar { display: flex; gap: 0.55rem; flex-wrap: wrap; margin: 0.2rem 0 1.1rem 0; }
.stat {
    border: 1px solid rgba(150,170,160,0.28); border-radius: 8px;
    padding: 0.6rem 1rem; min-width: 116px;
}
.stat-n {
    font-family: 'Source Serif 4', serif; font-size: 1.7rem;
    font-weight: 700; line-height: 1;
}
.stat-l {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.6rem;
    letter-spacing: 0.1em; text-transform: uppercase;
    color: #7C8A85; margin-top: 0.3rem;
}
.n-high { color: #E06C65; } .n-med { color: #E0A44A; }
.n-low { color: #5FAE85; } .n-ink { color: inherit; }
.stat.alert { border-color: rgba(224,108,101,0.5); }

/* --- Clause cards: the clause finder's main surface --- */
.clause {
    border: 1px solid rgba(150,170,160,0.25);
    border-left: 3px solid #4A9382;
    border-radius: 8px; padding: 0.95rem 1.15rem; margin-bottom: 0.7rem;
}
.clause.none { border-left-color: rgba(150,170,160,0.3); border-style: dashed; opacity: 0.72; }
.clause-who {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.66rem;
    letter-spacing: 0.08em; text-transform: uppercase; color: #4A9382;
}
.clause-head { font-weight: 600; font-size: 0.97rem; margin-top: 0.15rem; }
.clause-body {
    font-size: 0.87rem; line-height: 1.7;
    border-left: 2px solid rgba(150,170,160,0.3);
    padding-left: 0.85rem; margin-top: 0.6rem;
}
.clause-plain {
    color: #7C8A85; font-size: 0.85rem; margin-top: 0.55rem; font-style: italic;
}
.clause-absent { color: #8A968F; font-size: 0.86rem; font-style: italic; }

/* --- Field cards --- */
.fld {
    border: 1px solid rgba(150,170,160,0.25); border-radius: 8px;
    padding: 0.7rem 0.95rem; margin-bottom: 0.45rem;
}
.fld.gone { border-style: dashed; opacity: 0.6; }
.fld-l {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.62rem;
    letter-spacing: 0.09em; text-transform: uppercase; color: #7C8A85;
}
.fld-v { font-size: 0.97rem; font-weight: 500; margin-top: 0.15rem; }
.fld-v.gone { color: #8A968F; font-weight: 400; font-style: italic; }
.fld-e {
    font-size: 0.74rem; color: #8A968F; margin-top: 0.35rem; font-style: italic;
    border-left: 2px solid rgba(150,170,160,0.28); padding-left: 0.6rem;
}
.fld-noev {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.66rem;
    color: #E0A44A; margin-top: 0.35rem;
}

/* --- Risk rows --- */
.stamp {
    display: inline-block; font-family: 'IBM Plex Mono', monospace;
    font-size: 0.63rem; font-weight: 600; letter-spacing: 0.11em;
    text-transform: uppercase; padding: 0.13rem 0.5rem;
    border: 1.5px solid currentColor; border-radius: 3px;
    transform: rotate(-1.5deg); flex-shrink: 0; margin-top: 0.1rem;
}
.stamp-high { color: #E06C65; } .stamp-medium { color: #E0A44A; } .stamp-low { color: #5FAE85; }
.risk {
    display: flex; align-items: flex-start; gap: 0.7rem;
    padding: 0.75rem 0; border-bottom: 1px solid rgba(150,170,160,0.2);
}
.risk-name { font-weight: 600; }
.risk-why { font-size: 0.9rem; opacity: 0.88; }
.risk-ev {
    font-size: 0.74rem; color: #8A968F; font-style: italic; margin-top: 0.3rem;
}
.tag {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.6rem;
    letter-spacing: 0.06em; text-transform: uppercase;
    padding: 0.05rem 0.4rem; border-radius: 3px; margin-left: 0.4rem;
}
.tag-rule { background: rgba(74,147,130,0.18); color: #4A9382; }
.tag-ai { background: rgba(224,164,74,0.16); color: #C08A3E; }

.note {
    font-size: 0.78rem; line-height: 1.6; color: #7C8A85;
    border-left: 2px solid rgba(74,147,130,0.4);
    padding: 0.1rem 0 0.1rem 0.7rem; margin-bottom: 0.9rem;
}

/* --- How it works --- */
.step { display: flex; gap: 0.85rem; padding: 0.65rem 0; }
.step-n {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.66rem; font-weight: 600;
    color: #4A9382; border: 1.5px solid #4A9382; border-radius: 3px;
    padding: 0.06rem 0.36rem; flex-shrink: 0; height: fit-content; margin-top: 0.15rem;
}
.step-t { font-weight: 600; font-size: 0.91rem; }
.step-b { color: #7C8A85; font-size: 0.85rem; line-height: 1.55; }

section[data-testid="stFileUploaderDropzone"] { border: 1.5px dashed #4A9382 !important; }
div.stButton > button, div.stDownloadButton > button { border-radius: 7px; font-weight: 500; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Field layout -- declared once, reused by the coverage count and the detail
# view so the two cannot drift apart.
# ---------------------------------------------------------------------------

FIELD_SECTIONS = [
    ("Identification", [
        ("Vendor", "vendor_name"),
        ("Customer", "customer_name"),
        ("Contract title", "contract_title"),
    ]),
    ("Term & renewal", [
        ("Effective date", "effective_date"),
        ("Term end date", "term_end_date"),
        ("Term length", "term_length"),
        ("Auto-renewal", "auto_renewal"),
        ("Renewal notice period", "renewal_notice_period_days"),
    ]),
    ("Commercial terms", [
        ("Contract value", "contract_value"),
        ("Pricing model", "pricing_model"),
        ("Price escalation cap", "price_escalation_cap"),
        ("Payment terms", "payment_terms"),
    ]),
    ("Licence & entitlement", [
        ("Licence metric", "license_metric"),
        ("True-up rights", "true_up_rights"),
    ]),
    ("Compliance & audit", [
        ("Audit rights present", "audit_rights_present"),
        ("Audit notice period", "audit_notice_period_days"),
        ("Audit frequency", "audit_frequency"),
    ]),
    ("Termination", [
        ("Termination for convenience", "termination_for_convenience"),
        ("Termination for cause", "termination_for_cause"),
        ("Early termination fee", "early_termination_fee"),
    ]),
    ("SLA & exit", [
        ("SLA summary", "sla_summary"),
        ("Data exit / transition period", "data_exit_transition_period"),
    ]),
]
ALL_FIELD_NAMES = [attr for _, fields in FIELD_SECTIONS for _, attr in fields]

_DEFAULTS = {"stage": "upload", "batch": None, "error_message": None, "pending_files": None}
for _k, _v in _DEFAULTS.items():
    st.session_state.setdefault(_k, _v)
st.session_state.setdefault("show_how", False)


def reset_session():
    for key, value in _DEFAULTS.items():
        st.session_state[key] = value


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

left, right = st.columns([5, 1])
with left:
    st.markdown('<div class="eyebrow">Software Asset Management</div>', unsafe_allow_html=True)
    st.markdown("# Contract Analyzer")
with right:
    st.write("")
    if st.button("How it works", use_container_width=True):
        st.session_state.show_how = not st.session_state.show_how

st.markdown(
    '<div class="subtitle">Drop in a folder of contracts. Get every licence line item '
    "in one entitlement summary, and every important clause pulled out verbatim — so you "
    "don't have to read each agreement to find them.</div>",
    unsafe_allow_html=True,
)

HOW_IT_WORKS = [
    ("1", "Read whatever you upload",
     "PDFs, Word files, vendor spreadsheets, CSVs, and photos or scans of pages, which are "
     "OCR'd automatically. Tables in order forms are read as tables, so a part number stays "
     "attached to its quantity."),
    ("2", "Pull out the facts",
     "Each contract is read for a fixed set of terms — dates, notice periods, audit rights, "
     "pricing, licence line items. Every value comes back with a page number and a quote. "
     "A term that isn't there is reported as not found, never guessed."),
    ("3", "Capture the clauses",
     "Important clauses are captured word-for-word against a fixed list, each with its "
     "section heading and page. Because the list is fixed, the same clause lines up across "
     "every contract in the batch."),
    ("4", "Score the risks",
     f"Severity comes from a fixed rulebook, not the AI. Audit notice under "
     f"{THRESHOLDS['audit_notice_high_days']} days is high; a renewal window under "
     f"{THRESHOLDS['renewal_notice_high_days']} days is high. The same contract always "
     "scores the same way, and the thresholds are tunable."),
    ("5", "Consolidate",
     "Line items from every contract stack into one entitlement summary, each row tagged "
     "with its source. Files that couldn't be read are listed separately rather than "
     "quietly dropped."),
]

if st.session_state.show_how:
    with st.container(border=True):
        for num, title, body in HOW_IT_WORKS:
            st.markdown(
                f'<div class="step"><div class="step-n">{num}</div>'
                f'<div><div class="step-t">{title}</div>'
                f'<div class="step-b">{body}</div></div></div>',
                unsafe_allow_html=True,
            )
        st.markdown(
            '<div class="note">This reads what a contract says; it does not interpret what '
            "it means legally. Every value and clause shows its page number, so checking one "
            "against the original takes seconds. Nothing is stored after you close the tab.</div>",
            unsafe_allow_html=True,
        )
        if st.button("Close", key="close_how"):
            st.session_state.show_how = False
            st.rerun()


# ---------------------------------------------------------------------------
# UPLOAD
# ---------------------------------------------------------------------------

if st.session_state.stage == "upload":
    files = st.file_uploader(
        "Contract files",
        type=[s.lstrip(".") for s in SUPPORTED_SUFFIXES],
        accept_multiple_files=True,
        label_visibility="collapsed",
        help="PDF, Word, Excel, CSV, or photos/scans of pages.",
    )

    if files:
        total_mb = sum(len(f.getvalue()) for f in files) / (1024 * 1024)
        st.caption(f"**{len(files)} file(s)** · {total_mb:.1f} MB")
        if len(files) > 8:
            st.caption(
                "Files are analyzed one at a time and spaced out to respect the API rate "
                "limit, so a pack this size takes a few minutes."
            )
    else:
        st.caption(
            "Master agreements, order forms, licence schedules, renewal amendments — "
            "including scans and vendor spreadsheets. Upload a whole pack at once."
        )

    if st.button("Analyze", disabled=not files, type="primary"):
        st.session_state.pending_files = [(f.name, f.getvalue()) for f in files]
        st.session_state.stage = "analyzing"
        st.rerun()


# ---------------------------------------------------------------------------
# ANALYZING
# ---------------------------------------------------------------------------
# batch.analyze_one turns every per-file failure into a recorded result rather
# than an exception, so one locked PDF in a pack of twelve doesn't cost the
# other eleven.

elif st.session_state.stage == "analyzing":
    pending = st.session_state.pending_files or []
    total = len(pending)

    with st.status(f"Analyzing {total} file(s)…", expanded=True) as status:
        try:
            with SessionWorkspace() as ws:
                paths = [ws.save_upload(name, data) for name, data in pending]
                bar = st.progress(0.0)

                def on_start(i, n, filename):
                    st.write(f"**{i}/{n}** · {filename}")

                def on_done(i, n, result):
                    if result.ok:
                        bits = [f"{result.pages}p"]
                        if result.ocr_pages:
                            bits.append(f"{result.ocr_pages} OCR'd")
                        bits.append(f"{result.line_item_count} line items")
                        bits.append(f"{len(result.extraction.key_clauses)} clauses")
                        st.write(f"　✓ {result.vendor} — {', '.join(bits)}")
                    else:
                        st.write(f"　✗ {result.filename} — {result.error}")
                    bar.progress(i / n)

                batch = analyze_batch(paths, on_file_start=on_start, on_file_done=on_done)

            if not batch.successful:
                st.session_state.error_message = (
                    "**None of the uploaded files could be analyzed.**\n\n"
                    + "\n".join(f"- `{r.filename}` — {r.error}" for r in batch.failed)
                )
                st.session_state.stage = "error"
                status.update(label="Analysis failed", state="error")
                st.rerun()

            st.session_state.batch = batch
            st.session_state.stage = "results"
            status.update(label=f"Analyzed {len(batch.successful)} of {total}", state="complete")
            st.rerun()

        except Exception as e:  # noqa: BLE001 -- never surface a raw traceback
            st.session_state.error_message = (
                f"**Something went wrong.** ({type(e).__name__}: {e})\n\n"
                "Try again, or with fewer files at once."
            )
            st.session_state.stage = "error"
            status.update(label="Analysis failed", state="error")
            st.rerun()


# ---------------------------------------------------------------------------
# ERROR
# ---------------------------------------------------------------------------

elif st.session_state.stage == "error":
    st.error(st.session_state.error_message)
    st.caption(
        "Password-protected PDF? Remove the password and re-upload. Legacy `.doc`? Save it "
        "as `.docx` or print to PDF. Photo of a page? Retake it straight-on, in good light, "
        "with the page filling the frame."
    )
    if st.button("Start over"):
        reset_session()
        st.rerun()


# ---------------------------------------------------------------------------
# RESULTS
# ---------------------------------------------------------------------------

elif st.session_state.stage == "results":
    import pandas as pd

    batch = st.session_state.batch
    successful = batch.successful

    agg = {"high": 0, "medium": 0, "low": 0}
    for result in successful:
        for level, count in risk_summary(result.extraction.risk_flags)["counts"].items():
            agg[level] += count
    clause_total = sum(len(r.extraction.key_clauses) for r in successful)

    stats = [
        (len(successful), "Contracts", "n-ink", False),
        (batch.total_line_items, "Line items", "n-ink", False),
        (clause_total, "Clauses", "n-ink", False),
        (agg["high"], "High risk", "n-high", False),
        (agg["medium"], "Medium", "n-med", False),
    ]
    if batch.failed:
        stats.append((len(batch.failed), "Unreadable", "n-high", True))

    st.markdown(
        '<div class="bar">'
        + "".join(
            f'<div class="stat{" alert" if alert else ""}">'
            f'<div class="stat-n {cls}">{n}</div><div class="stat-l">{label}</div></div>'
            for n, label, cls, alert in stats
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    act_l, act_r = st.columns([3, 1])
    with act_l:
        try:
            st.download_button(
                "⬇  Download entitlement summary workbook",
                data=generate_batch_workbook(batch),
                file_name=f"entitlement_summary_{datetime.now():%Y%m%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"Couldn't build the workbook ({e}). Results are still shown below.")
    with act_r:
        if st.button("Analyze more", use_container_width=True):
            reset_session()
            st.rerun()

    tab_names = ["Entitlement summary", "Clause finder", "Risks", "Contract detail"]
    if batch.failed:
        tab_names.append(f"Not read ({len(batch.failed)})")
    tabs = st.tabs(tab_names)

    # --- Entitlement summary ------------------------------------------------
    with tabs[0]:
        if batch.total_line_items:
            rows = [
                {
                    "Contract": r.filename,
                    "Vendor": r.vendor,
                    "Part #": p.publisher_part_number or "—",
                    "Product": p.product_name or "—",
                    "Type": p.license_type or "—",
                    "Metric": p.license_metric or "—",
                    "Purchased rights": p.purchased_rights or "—",
                    "Unit cost": p.unit_cost or "—",
                    "Start": p.start_date or "—",
                    "End": p.end_date or "—",
                    "Page": p.evidence.page if p.evidence and p.evidence.page else "—",
                }
                for r in successful for p in r.extraction.products
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                         hide_index=True, height=440)
            st.caption(
                "Every licence line item across the batch. The workbook's first sheet has "
                "this table plus the source quote for each row."
            )
            empty = [r for r in successful if not r.extraction.products]
            if empty:
                st.caption(
                    "No licence schedule found in: "
                    + ", ".join(f"`{r.filename}`" for r in empty)
                    + " — each appears in the workbook as a flagged row, so they aren't "
                    "silently missing."
                )
        else:
            st.info(
                "No licence line items were found. That's normal for master agreements with "
                "no order form attached — clauses and terms were still extracted."
            )

    # --- Clause finder ------------------------------------------------------
    # The reason the tool exists for anyone who currently opens each contract
    # to find a clause. Pick a clause type, read what every contract says
    # about it, verbatim, one under the other.
    with tabs[1]:
        coverage = {
            ct: sum(1 for r in successful
                    if any(c.clause_type == ct for c in r.extraction.key_clauses))
            for ct in CLAUSE_TYPES
        }
        found = [ct for ct in CLAUSE_TYPES if coverage[ct]]

        if not found:
            st.info(
                "No clauses were captured. That happens with order forms and quotes, which "
                "carry line items but no contractual language — the terms usually live in a "
                "master agreement instead."
            )
        else:
            pick, panel = st.columns([1, 2.6])
            with pick:
                st.caption("CLAUSE TYPE")
                choice = st.radio(
                    "Clause type",
                    options=found,
                    format_func=lambda t: f"{t}  ({coverage[t]}/{len(successful)})",
                    label_visibility="collapsed",
                )
                st.caption(
                    "The count is how many contracts contain that clause. "
                    "Types absent from every contract are hidden."
                )

            with panel:
                st.markdown(f"### {h(choice)}")
                st.markdown(
                    '<div class="note">Text below is copied verbatim from each contract. '
                    "The italic line is a plain-English reading, not contract language.</div>",
                    unsafe_allow_html=True,
                )
                for result in successful:
                    clause = next(
                        (c for c in result.extraction.key_clauses
                         if c.clause_type == choice), None,
                    )
                    if clause is None:
                        st.markdown(
                            f'<div class="clause none"><div class="clause-who">'
                            f"{h(result.vendor)}</div>"
                            f'<div class="clause-absent">No {h(choice.lower())} clause '
                            f"found in {h(result.filename)}.</div></div>",
                            unsafe_allow_html=True,
                        )
                        continue

                    page = f" · p.{clause.page}" if clause.page else ""
                    body = f'<div class="clause-body">{h(clause.text)}</div>' if clause.text else ""
                    plain = (f'<div class="clause-plain">{h(clause.summary)}</div>'
                             if clause.summary else "")
                    st.markdown(
                        f'<div class="clause"><div class="clause-who">'
                        f"{h(result.vendor)}{page}</div>"
                        f'<div class="clause-head">{h(clause.heading or choice)}</div>'
                        f"{body}{plain}</div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(f"Source: {result.filename}")

    # --- Risks --------------------------------------------------------------
    with tabs[2]:
        st.markdown(
            '<div class="note"><b>Rule-based</b> findings come from fixed thresholds applied '
            "to the extracted terms — the same contract always produces the same flags. "
            "<b>AI-suggested</b> findings are unusual clauses outside the rulebook and need "
            "a human read.</div>",
            unsafe_allow_html=True,
        )
        severities = st.multiselect(
            "Severity", ["high", "medium", "low"], default=["high", "medium"],
            label_visibility="collapsed",
        )

        shown = False
        for result in successful:
            flags = [f for f in result.extraction.risk_flags
                     if f.severity.lower() in severities]
            if not flags:
                continue
            shown = True
            st.markdown(f"##### {h(result.vendor)}")
            st.caption(result.filename)
            for flag in flags:
                sev = flag.severity.lower()
                ev = ""
                if flag.evidence and flag.evidence.quote:
                    page = f" · p.{flag.evidence.page}" if flag.evidence.page else ""
                    ev = (f'<div class="risk-ev">&ldquo;{h(flag.evidence.quote)}'
                          f"&rdquo;{page}</div>")
                is_rule = flag.source == "rule"
                tag = (f'<span class="tag {"tag-rule" if is_rule else "tag-ai"}">'
                       f'{"rule" if is_rule else "AI"}</span>')
                st.markdown(
                    f'<div class="risk"><span class="stamp stamp-{sev}">{sev}</span>'
                    f'<div><span class="risk-name">{h(flag.clause)}</span>{tag}<br>'
                    f'<span class="risk-why">{h(flag.explanation)}</span>{ev}</div></div>',
                    unsafe_allow_html=True,
                )
            st.write("")

        if not shown:
            st.success("No flags at the selected severities.")

    # --- Contract detail ----------------------------------------------------
    with tabs[3]:
        index = st.selectbox(
            "Contract",
            options=list(range(len(successful))),
            format_func=lambda i: f"{successful[i].vendor} — {successful[i].filename}",
            label_visibility="collapsed",
        )
        result = successful[index]
        extraction = result.extraction

        found_fields = sum(
            1 for name in ALL_FIELD_NAMES
            if getattr(extraction, name, None) and getattr(extraction, name).value
        )
        meta = [f"{result.pages} page(s)"]
        if result.ocr_pages:
            meta.append(f"{result.ocr_pages} OCR'd")
        meta += [f"{found_fields}/{len(ALL_FIELD_NAMES)} terms found",
                 f"{len(extraction.products)} line items"]
        st.caption(" · ".join(meta))

        if extraction.plain_english_summary:
            st.markdown(extraction.plain_english_summary)

        if result.conflicts:
            with st.expander("⚠️ Conflicting values across sections of this document"):
                st.caption(
                    "This contract was long enough to be read in sections, and these fields "
                    "came back differently in different sections. The first value found is "
                    "shown; check them against the original."
                )
                for name, values in result.conflicts.items():
                    st.write(f"**{name.replace('_', ' ')}**: {', '.join(values)}")

        show_missing = st.toggle(
            "Show terms not found", value=False,
            help="An absent term is often a finding in itself — no termination-for-"
                 "convenience clause tells you something.",
        )

        def render_field(label, field):
            if field is None or not field.value:
                if show_missing:
                    st.markdown(
                        f'<div class="fld gone"><div class="fld-l">{h(label)}</div>'
                        f'<div class="fld-v gone">Not found</div></div>',
                        unsafe_allow_html=True,
                    )
                return
            if field.evidence and field.evidence.quote:
                page = f" · p.{field.evidence.page}" if field.evidence.page else ""
                ev = (f'<div class="fld-e">&ldquo;{h(field.evidence.quote)}'
                      f"&rdquo;{page}</div>")
            else:
                # Surfaced, not hidden: a value with no quote behind it is the
                # one most worth re-checking against the source.
                ev = '<div class="fld-noev">No supporting quote — verify manually</div>'
            st.markdown(
                f'<div class="fld"><div class="fld-l">{h(label)}</div>'
                f'<div class="fld-v">{h(field.value)}</div>{ev}</div>',
                unsafe_allow_html=True,
            )

        col_l, col_r = st.columns(2)
        for position, (section, fields) in enumerate(FIELD_SECTIONS):
            resolved = [(label, getattr(extraction, attr, None)) for label, attr in fields]
            n_found = sum(1 for _, f in resolved if f is not None and f.value)
            if not n_found and not show_missing:
                continue
            with (col_l if position % 2 == 0 else col_r):
                st.markdown(f"**{section}**  ·  {n_found}/{len(resolved)}")
                for label, field in resolved:
                    render_field(label, field)

        if extraction.products:
            st.markdown("**Licence line items**")
            st.dataframe(
                pd.DataFrame([
                    {
                        "Part #": p.publisher_part_number or "—",
                        "Product": p.product_name or "—",
                        "Type": p.license_type or "—",
                        "Metric": p.license_metric or "—",
                        "Purchased rights": p.purchased_rights or "—",
                        "Unit cost": p.unit_cost or "—",
                        "Page": p.evidence.page if p.evidence and p.evidence.page else "—",
                    }
                    for p in extraction.products
                ]),
                use_container_width=True, hide_index=True,
            )

        try:
            st.download_button(
                f"PDF report — {result.vendor}",
                data=generate_pdf_report(extraction, result.filename),
                file_name=f"{result.vendor.replace(' ', '_').replace('/', '-')}_analysis.pdf",
                mime="application/pdf",
            )
        except Exception as e:
            st.caption(f"PDF unavailable for this contract ({e}).")

    # --- Unreadable files ---------------------------------------------------
    if batch.failed:
        with tabs[4]:
            st.warning(
                "These files are **not** in the entitlement summary or the clause finder. "
                "Fix and re-run them before treating the summary as complete."
            )
            for result in batch.failed:
                st.markdown(f"**`{result.filename}`** — {result.error}")

    st.caption(
        "AI-generated analysis, not legal advice. Verify anything you act on against the "
        "signed original."
    )
