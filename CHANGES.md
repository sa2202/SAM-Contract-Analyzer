# Changes

## Pass 3 — clause finder, photos, simpler UI

**The UI was too dense.** The previous results screen stacked a batch-level
banner on a per-contract banner with two sets of counters, so it wasn't clear
which numbers described what. Now: one summary strip, then tabs, each
answering a single question — entitlement summary, clause finder, risks,
contract detail, unreadable files. Nothing is shown twice. 775 lines, down
from 987.

**Clause finder (new).** `schema.py` gained `KeyClause` and a fixed
`CLAUSE_TYPES` catalogue of 20 clause types. The model now captures each
clause **verbatim** (up to ~120 words) with its section heading, page, and a
separate one-line plain-English reading — kept apart so a paraphrase is never
mistaken for contract language. Pick a clause type and read what every
contract in the batch says about it, side by side.

The catalogue is closed on purpose: free-form clause naming produces different
labels per contract, which makes them impossible to line up. Off-catalogue
types returned by the model are dropped in the sanitizer.

**Photos and scans.** `.jpg`, `.png`, `.tif`, `.bmp`, `.webp` now accepted.
Phone photos get EXIF rotation correction (a portrait photo is stored sideways
with a tag Tesseract ignores), greyscale + autocontrast, and upscaling below
1600px — under that, body text falls beneath the character height Tesseract
needs and accuracy collapses.

**Workbook gained a Key Clauses sheet**, sorted by clause type then contract,
with an autofilter — so filtering to "Audit rights" gives you every audit
clause in the batch in one column. Clauses also appear on each per-contract
sheet.

---

## Pass 2 — multi-contract

**Reading messier files** (`parser.py`): PDF tables extracted as tables via
`find_tables()` (flattened text separates a part number from its quantity, and
the model then guesses wrong); `sort=True` for correct reading order in
two-column agreements; hybrid OCR for pages with a digital header over a
scanned body; `--psm 6` and autocontrast for faxed scans. Added `.xlsx`,
`.xlsm`, `.csv`, `.txt`.

**Batch processing** (`batch.py`): per-file failures are recorded, not raised,
so one locked PDF doesn't cost the other eleven. The rate limiter is now shared
across the batch — previously each document built its own and a pack would blow
through the per-minute budget.

**The workbook** (`entitlement.py`): consolidated entitlement summary first,
then overview, clauses, risks, issues, then one sheet per contract.

*Bug caught in testing:* sheet names were keyed by filename, so two contracts
both called `Order Form.pdf` collapsed onto one entry and openpyxl silently
renamed a tab. Now resolved positionally.

---

## Pass 1 — deterministic risk scoring

**Truncation bug.** `prompts.MAX_DOCUMENT_CHARS` was 14,000 while the chunker
was sized at 180,000, so every chunk was silently cut before the model saw it —
on a 60-page agreement only the first several pages were ever analysed. Audit
and termination clauses live deep in contracts, so they came back as "not
found", which reads like a clean result. Chunk size now 45,000 against a 60,000
ceiling, with an import-time assert so the two can't drift apart again.

**Re-run anything long you tested before this fix.**

**Two parsing bugs**, caught by unit tests: `"thirty (30) days"` parsed as 10
(the substring `ten` inside `written`), and `"500k"` parsed as 500.

**Risk scoring moved out of the model** (`risk_rules.py`): seven rules, all
thresholds in one dict, deterministic, run after the merge so a rule can see
facts from different chunks. The prompt was retargeted to tell the model *not*
to flag standard risks — the rulebook owns those — and to spend its slots on
unusual clauses. Unsourced flags are dropped; duplicates lose to the rulebook.

`RiskFlag` gained `source` and `rule_id`; rule-based and AI-suggested findings
are kept visually separate everywhere.

**Excel export added**, plus a risk posture panel in the PDF and a
`KeepTogether` fix for an orphaned heading.

---

## Still open

- **Still on Gemini**, not a local model. A batch now sends a whole client pack
  to a third-party API in one run, so this matters more than it did.
- **No persistence.** Cross-batch querying (the RAG layer) needs somewhere
  extractions land. The workbook is a per-run artifact, not a searchable corpus.
