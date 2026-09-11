# Multi-contract comparison

New file `comparison.py`, plus updated `app.py` and `entitlement.py`.
Copy all three in. No new dependencies.

## What it adds

A **Compare** tab (appears with 2+ contracts) and a **Comparison** sheet in
the workbook. Two modes:

- **Key terms** — one row per term, one column per contract. Audit notice
  across five agreements reads as 10 / 45 / 30 / 30 / 7 in a single glance
  instead of five paragraphs of stacked prose.
- **Clause text** — the actual language side by side in columns, so wording
  differences are visible without scrolling between them.

The least favourable value in each row is marked red, with a plain sentence
underneath: "Ranges from 7 days (Initrode) to 45 days (Initech)."

## One thing worth knowing

**Absence can outrank any number.** A missing price-escalation cap means
increases are *unlimited*, which is worse than any stated cap. The first
version ranked only contracts that HAD a cap — and so flagged a 5% cap as
the worst while two contracts with uncapped increases sat unmarked. That
isn't cosmetic; it points the reviewer at the wrong contract. Terms where
absence is the risk now mark the missing ones instead, and the summary line
names them before describing the range.

## No extra API calls

Every value is already extracted. This only reorganises it, so the page
numbers and quotes behind each cell still apply and any figure can be checked
against the source. Nothing is re-read and no quota is spent.

## Install

```bash
cd ~/Downloads/SAM-Contract-Analyzer
cp /path/to/updated-files/*.py .
source .venv/bin/activate
streamlit run app.py        # test locally FIRST
git add app.py comparison.py entitlement.py
git commit -m "Add multi-contract comparison"
git push
```

`comparison.py` is a new file — make sure `git add` picks it up. An untracked
new file is the most common way a deploy ends up half-applied.
