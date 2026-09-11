# Automatic model rotation — no more editing settings

Replace `app.py`, `extract.py`, `batch.py`.

## The problem

Free-tier daily quota is counted **per model**. When one runs out, the app
stopped and someone had to edit `GEMINI_MODEL` in Streamlit secrets — for
everyone, potentially daily. That puts one administrator in the loop for
every other person's failure, at exactly the moment they're mid-task.

## What it does now

The app keeps an ordered list of models and moves to the next one by itself
when a daily quota is exhausted. Nobody edits anything.

- Default order: `gemini-3.6-flash` → `gemini-3.7-flash` →
  `gemini-flash-lite-latest`
- Override with a `GEMINI_MODELS` secret (comma-separated). `GEMINI_MODEL`,
  if set, still leads the list.
- The switch is announced in the progress log, not silent — a user should
  know why results came from a different model, and it's the signal that the
  day's quota on the first one is gone.

Three deliberate limits on the behaviour:

- **Only daily quota errors trigger a switch.** A per-minute limit is handled
  by waiting; rotating would spend another model's scarce daily allowance to
  dodge a 60-second pause. Any other error is a real failure and is raised
  immediately rather than retried across every model, which would turn one
  bad document into several failures.
- **Exhausted models are remembered per API key**, so a batch doesn't
  rediscover the same dead model once per contract. Tracked against a short
  hash of the key, never the key itself — one person's exhausted quota says
  nothing about another's.
- **A fully-exhausted list still attempts every model** rather than failing
  without a call. Quotas reset at midnight Pacific and that cache has no
  clock, so a stale entry must never be the only reason a request isn't made.

## Install

```bash
cd ~/Downloads/SAM-Contract-Analyzer
cp /path/to/updated-files/*.py .
git add app.py extract.py batch.py
git commit -m "Automatic model fallback on daily quota exhaustion"
git push
```

## Honest limits

This multiplies your daily headroom by the number of models available, and
removes the admin task. It does not make the free tier suitable for sustained
team use — roughly 20 requests per model per day, and a long contract can
cost several. Combined with per-person keys in the sidebar it stretches
considerably further, but billing remains the only thing that actually
removes the ceiling.
