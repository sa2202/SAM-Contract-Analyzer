# Full audit — every file read, every blocker fixed

Ten defects found by reading all 4,400 lines. Six would have stopped your
teammates cold; four of those produce no error message at all, which is worse.

---

## Crashes and hangs

### 1. Infinite hang on an oversized page — `chunking.py`
A single page larger than the chunk budget put the packing loop into an
endless cycle: flush buffer, refill with overlap tail, retry the same page,
still doesn't fit, repeat. **The app freezes with no error and no output** —
the worst failure mode there is, because there's nothing to report or debug.
Triggered by one dense OCR'd page or a PDF whose pages are long continuous
sheets. Oversized pages are now split with overlap.

### 2. Same class of bug in the single-page path — `chunking.py`
Found while testing the fix above. Text with no paragraph breaks (OCR of one
dense page, a DOCX saved as a single block, any CSV) came back as one
oversized chunk, which `MAX_DOCUMENT_CHARS` then silently truncated. **Exactly
the invisible truncation the chunker exists to prevent**, hiding in the branch
I hadn't exercised. A 200,000-character document now splits into 5 chunks
instead of being cut to 60,000 with no notice.

### 3. `IndexError` in the rate limiter — `rate_limiter.py`
A request larger than the entire per-minute budget can never fit, so the wait
loop fell through to `self._usage[0]` on an empty deque. Crashes mid-batch
with a message about deque indexing that says nothing about rate limits.
Reachable via the class's own default budget of 8,000. Now proceeds and lets
the API's own 429 speak, which is a far clearer error.

### 4. PDF generation dies on ordinary contract text — `report_generator.py`
reportlab parses `Paragraph` content as a small XML dialect, so contract text
is not inert data to it. `<REDACTED>` or `if headcount <b then` raises
"parse ended with 1 unclosed tags" and **kills the whole report** — not a
corrupted paragraph, no PDF at all. This became reachable when verbatim clause
text started going into the PDF; a 120-word passage of real contract language
is far likelier to contain a bracket than a 25-word evidence quote. All
dynamic text now escaped.

### 5. Excel export dies on OCR output — `exporters.py`, `entitlement.py`
Excel rejects most ASCII control characters and openpyxl raises
`IllegalCharacterError`. OCR output is full of them — stray `\x00` and `\x0b`
turn up routinely in text recovered from scans and photos. **One such
character anywhere in the batch aborted the entire workbook**, losing every
contract's data because one page was photographed badly. Cells are also capped
at 32,767 characters, which a long verbatim clause approaches. Both handled at
the write layer.

---

## Silent data loss

### 6. A number in the licence schedule discarded the whole contract — `extract.py`
Pydantic v2 does **not** accept an `int` where a `str` is declared — that lax
coercion existed in v1 and was deliberately removed. The fields most likely to
come back as bare numbers are precisely the ones an ELP is built from: a
quantity of `128`, a unit cost of `4200`, a notice period of `30`. One numeric
quantity failed validation and threw away the entire extraction — a
catastrophic response to a trivial type difference. Everything is now coerced
before validation, including booleans (`True` → `"yes"`, since `bool` is a
subclass of `int` in Python and `"True"` is not a valid `auto_renewal` value).

### 7. A missing summary sentence discarded the whole contract — `schema.py`
`plain_english_summary` was a required field. If the model omitted it, 22 good
fields, all the clauses and the entire licence schedule were thrown away — over
the least load-bearing thing in the object. Now defaults to empty.

### 8. Page references lost to formatting — `extract.py`
Models return `"p. 11"` and `"page 7"` as readily as `11`. These raised
validation errors on fields that carry a single citation. Now parsed
leniently: losing a page number costs one citation, losing the extraction
costs the contract.

### 9. Clauses vanishing from the app — `app.py`
Every card renders with `unsafe_allow_html=True`, so the browser parses
clause text as markup. A clause containing `<` opens a tag that never closes
and **everything after it in the card disappears**. Nothing errors — the
clause is simply not on screen, and a reviewer trusting this tool to surface
clauses has no way to know one went missing. All contract-derived text now
passes through `h()`.

### 10. Unrecognised severity values — `extract.py`
A severity outside low/medium/high sorted last and rendered unstyled, so a
flag the model considered critical would appear at the bottom in plain text.
Now normalised to `medium`.

---

## UI rebuild

The previous results screen stacked a batch banner on a per-contract banner
with two sets of counters. You couldn't tell which numbers described what.

Now: **one summary bar, then four tabs**, each answering a single question and
sharing no numbers with the others.

- Colours moved off hardcoded light-theme values, so it works in dark mode —
  the screenshots you sent showed near-black-on-black headings.
- Clause finder given the space it deserves: clause list on the left with
  per-type coverage counts, verbatim text on the right.
- Risk tab filterable by severity, with rule-based and AI-suggested tagged
  inline rather than split into separate sections.
- Contract detail in two columns instead of a single long scroll of expanders.

---

## What I could not test here

No network in this sandbox, so pydantic, streamlit and pymupdf aren't
installed. Verified by AST analysis, isolated unit tests against extracted
functions, and stub schemas: every cross-module import resolves, every schema
field reference is valid, the chunk/prompt/token invariants hold, and the
sanitizer, JSON repair, chunker, rate limiter, PDF and Excel writers were all
exercised against hostile input directly.

The live path — a real Gemini response through real pydantic — is the one
thing only your machine can confirm.
