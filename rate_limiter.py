"""
Rolling-window rate limiter so sending multiple chunks back-to-back doesn't
recreate the same limits across separate calls.

Tracks TWO independent dimensions, because Gemini enforces both and hitting
EITHER one triggers a 429, regardless of headroom on the other:

  - TPM (tokens per minute)
  - RPM (requests per minute)

This started as token-only, sized against an assumed generous free-tier
budget. Checking the real numbers for this project (Google AI Studio's
own Rate Limit page) showed RPM on the free tier is just 5 -- far more
restrictive than the token budget suggested. A contract long enough to
chunk into even 3-4 pieces could burn through that in under a minute on
its own, well before TPM became the binding constraint. Token-only
tracking made that invisible: the limiter would report "plenty of budget
left" right up until the API rejected the request outright for request
count, not size.

Approach for each dimension: a rolling 60-second window of timestamped
usage. Before each call, drop entries older than 60s, sum what's left, and
sleep long enough for enough old usage to age out if the next call would
exceed either budget. Simple by design -- no external deps -- and correct
for a single-process sequential caller, which is what this tool is.
"""

from __future__ import annotations

import time
from collections import deque


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token for English). Good enough for
    rate-limiting headroom decisions; not meant to be exact."""
    return max(1, len(text) // 4)


class TokenRateLimiter:
    def __init__(
        self,
        tpm_budget: int = 8000,
        rpm_budget: int = 5,
        safety_margin: float = 0.85,
    ):
        # safety_margin leaves headroom below the stated limit -- our token
        # estimate is approximate, and giving zero slack on an RPM budget as
        # tight as 5 means one miscount trips a 429 immediately.
        self.token_budget = int(tpm_budget * safety_margin)
        # RPM is a hard integer count, not an estimate -- rounding it down
        # would throw away real capacity for no benefit, so it isn't scaled
        # by safety_margin the way the token budget is.
        self.request_budget = max(1, rpm_budget)
        self.window_seconds = 60
        self._token_usage: deque[tuple[float, int]] = deque()
        self._request_usage: deque[float] = deque()

    def _prune(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._token_usage and self._token_usage[0][0] < cutoff:
            self._token_usage.popleft()
        while self._request_usage and self._request_usage[0] < cutoff:
            self._request_usage.popleft()

    def _current_tokens(self) -> int:
        self._prune()
        return sum(tokens for _, tokens in self._token_usage)

    def _current_requests(self) -> int:
        self._prune()
        return len(self._request_usage)

    def _seconds_until_capacity(self, estimated_tokens: int) -> float | None:
        """
        None if there's room right now. Otherwise the number of seconds
        until the OLDER of the two constraints (tokens, requests) would
        free up enough room -- whichever dimension is actually binding.
        """
        self._prune()
        token_ok = self._current_tokens() + estimated_tokens <= self.token_budget
        request_ok = self._current_requests() + 1 <= self.request_budget

        if token_ok and request_ok:
            return None

        candidates = []
        if not token_ok and self._token_usage:
            candidates.append(self._token_usage[0][0] + self.window_seconds)
        if not request_ok and self._request_usage:
            candidates.append(self._request_usage[0] + self.window_seconds)

        if not candidates:
            return None  # nothing to age out; belt-and-braces, see reserve()

        return max(candidates[0] - time.monotonic(), 1.0)

    def reserve(self, estimated_tokens: int, on_wait=None):
        """
        Blocks (sleeps) if needed so that one more request, of roughly
        `estimated_tokens` size, would stay within BOTH the token and
        request budgets for the trailing 60s window, then records the
        reservation. `on_wait(seconds)` is an optional callback so a UI can
        show a "waiting to respect rate limit..." message.
        """
        # A request larger than the entire per-minute token budget can never
        # fit no matter how long we wait. Proceeding is the right call here:
        # the API may accept it anyway, and if not, its own 429 is a clearer
        # error than this limiter looping forever waiting for headroom that
        # will never exist.
        if estimated_tokens >= self.token_budget:
            now = time.monotonic()
            self._token_usage.append((now, estimated_tokens))
            self._request_usage.append(now)
            return

        while True:
            wait_for = self._seconds_until_capacity(estimated_tokens)
            if wait_for is None:
                break
            if on_wait:
                on_wait(wait_for)
            time.sleep(wait_for)

        now = time.monotonic()
        self._token_usage.append((now, estimated_tokens))
        self._request_usage.append(now)
