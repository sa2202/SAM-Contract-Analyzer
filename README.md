# SAM Contract Analyzer

Drop in a folder of contracts. Get back the two things that otherwise take a
day by hand:

- **An entitlement summary** — every licence line item across every contract,
  in one table, ready to work into an ELP.
- **A clause finder** — the important clauses pulled out of every contract
  word-for-word and grouped by type, so "what do all of these say about audit
  rights?" is one click instead of twelve documents.

Every value and every clause carries the page number it came from, so checking
one against the signed original takes seconds.

---

## What you can upload

| Format | Notes |
|---|---|
| PDF | Native or scanned. Scanned pages are OCR'd automatically. |
| Word (`.docx`) | Paragraphs and tables both read. |
| Excel (`.xlsx`, `.xlsm`) | For order forms and quotes that arrive as spreadsheets. |
| CSV | Delimiter is detected, not assumed. |
| Photos (`.jpg`, `.png`, `.tif`, …) | Phone snaps of pages. Rotation and contrast are corrected before OCR. |

Ten to fifteen files in one go is a normal batch. Larger packs work but take
longer, because files are spaced out to respect the API rate limit.

**Legacy `.doc` won't work** — save as `.docx` or print to PDF first.
**Password-protected PDFs** are tried with an empty password (many are locked
only against printing); if that fails you'll be told to remove the password.

---

## What comes out

### In the app

Five tabs, each answering one question:

| Tab | Answers |
|---|---|
| **Entitlement summary** | What are we entitled to, across everything? |
| **Clause finder** | What does every contract say about *this* clause? |
| **Risks** | What should worry me, and how sure are we? |
| **Contract detail** | What's in this one agreement? |
| **Not read** | What didn't make it in? (only appears if something failed) |

### The workbook

One `.xlsx`, sheets in this order:

1. **Entitlement Summary** — all line items, stacked, each row tagged with its
   contract, vendor, page and quote.
2. **Contracts Overview** — one row per contract: vendor, term, renewal, audit
   posture, risk counts.
3. **Key Clauses** — every captured clause, verbatim, sorted by clause type so
   the same clause reads down the column across all contracts. Filter the
   Clause Type column to compare one clause everywhere.
4. **Risk Register** — every flag, filterable by severity and source.
5. **Issues** — files that couldn't be read, and why.
6. **One sheet per contract** — full terms, clauses and flags, tab named after
   the contract.

---

## What's AI and what isn't

This matters when you're putting numbers in front of a client.

**The AI reads and transcribes.** It pulls out field values and copies clause
text. It's required to supply a page and a quote for everything, and to report
a term as not found rather than guess it.

**A fixed rulebook decides severity** (`risk_rules.py`). Audit notice under 15
days is high. A renewal window under 30 days is high. An early termination fee
over 25% of contract value is high. The same contract always produces the same
flags — run it twice, get the same answer.

Flags are labelled **rule-based** or **AI-suggested** everywhere they appear.
AI-suggested ones are unusual clauses the rulebook doesn't cover
(most-favoured-customer, odd assignment restrictions) — useful prompts for a
human read, not settled findings. Any flag without a supporting quote is
discarded rather than shown.

**Tuning:** every threshold is in the `THRESHOLDS` dict at the top of
`risk_rules.py`. Change a number there and the whole rulebook shifts. Nothing
else needs editing.

---

## Clause types captured

License grant · License restrictions · Deployment / virtualisation rights ·
Audit rights · True-up / over-deployment · Term & renewal · Termination ·
Pricing & payment · Price increases · Support & maintenance · Service levels
(SLA) · Assignment / change of control · Transfer & resale · Limitation of
liability · Indemnity · Confidentiality · Data protection · Exit & transition ·
Governing law · Warranty

The list is fixed on purpose. Free-form clause naming produces a different set
of labels per contract, which makes them impossible to line up side by side.
A closed list means the audit clause in contract A sits in the same row as the
audit clause in contract B — which is the whole point.

To add a type, add it to `CLAUSE_TYPES` in `schema.py`; the prompt, the app and
the workbook all read from that list.

---

## Running it

```bash
pip install -r requirements.txt
# Tesseract must also be installed (see packages.txt) for OCR:
#   macOS:  brew install tesseract
#   Ubuntu: sudo apt install tesseract-ocr
```

Create a `.env` file:

```
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.6-flash
```

**Model IDs get retired.** If you see "The model ... is not available", Google
has dropped that ID — the error message names a current one, and
[the model list](https://ai.google.dev/gemini-api/docs/models) is authoritative.
As of August 2026, `gemini-3.6-flash` is stable and `gemini-3.7-flash` is the
newest Flash model.

Note that Gemini 3.x ignores the `temperature` parameter (Google deprecated it),
so extraction wording varies slightly between runs on those models. Risk
severities do **not** vary — those come from the rulebook in `risk_rules.py`,
which is deterministic regardless of model.

Then:

```bash
streamlit run app.py
```

On Streamlit Cloud, put the same two values in **Settings → Secrets** instead
of a `.env` file — the app reads either.

---

## Verify it works on your machine

```bash
python selfcheck.py           # environment + all fixes, no API calls
python selfcheck.py --api     # also makes one small live call
python selfcheck.py -v        # show tracebacks on failure
```

Run it after setup, and again after any change to `schema.py` or `prompts.py`.
It checks dependencies and the Tesseract binary, confirms the size invariants
between chunk budget / prompt ceiling / output tokens still hold, and pushes a
deliberately messy payload through the sanitizer into real pydantic validation
— the path most likely to break silently when the schema changes.

## Storage and privacy

Uploads land in a temp directory that is deleted as soon as the text has been
read out of them. Results live in the browser session and disappear when the
tab closes. Nothing is written to a database.

**But:** contract text is sent to Google's Gemini API for extraction. Before
running real client contracts through this, that needs signing off — or
swapping for a local model. The extractor uses an OpenAI-compatible client, and
Ollama exposes the same interface, so the swap is close to a base-URL change in
`extract.py`.

---

## Files

| File | Job |
|---|---|
| `app.py` | Streamlit UI |
| `parser.py` | Reads PDF / Word / Excel / CSV / images, OCR, table extraction |
| `chunking.py` | Splits long documents into overlapping sections |
| `prompts.py` | What the model is asked for |
| `extract.py` | Calls the model, validates and repairs the response |
| `merge.py` | Combines results from a chunked document |
| `risk_rules.py` | **Deterministic severity scoring — tune thresholds here** |
| `batch.py` | Runs a whole pack, isolating per-file failures |
| `entitlement.py` | Builds the multi-contract workbook |
| `exporters.py` | Single-contract sheets |
| `report_generator.py` | Per-contract PDF report |
| `schema.py` | **The extraction schema and clause catalogue** |
| `rate_limiter.py` | Keeps the batch inside the API's token budget |

---

## If you add clause types

`MAX_OUTPUT_TOKENS` in `extract.py` is sized against the schema. The 20 clause
types, each carrying ~120 words of verbatim text, account for roughly 4,200 of
the ~6,700 tokens a dense contract needs. If you extend `CLAUSE_TYPES` in
`schema.py` or raise the per-clause word limit in `prompts.py`, raise that
number too — otherwise the response gets cut off mid-JSON. The app now detects
this and says so explicitly rather than reporting a parse error.

## Known limits

- **Not a legal review.** It reports what a contract says, not what it means.
- **No cross-batch memory.** Each run is independent; there's no stored corpus
  to query across past batches yet.
- **Line-item extraction is only as good as the source table.** A badly
  photographed order form will produce gaps — the page numbers are there so you
  can spot them.
