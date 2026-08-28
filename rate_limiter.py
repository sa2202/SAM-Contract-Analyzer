"""
Rolling-window rate limiter so sending multiple chunks back-to-back doesn't
just recreate the same TPM problem across separate calls.

Approach: keep a rolling 60-second window of (timestamp, tokens_used)
entries. Before each call, drop entries older than 60s, sum what's left,
and sleep just long enough for enough old usage to "age out" if the next
call would exceed the budget. This is intentionally simple (no external
deps) -- fine for a single-user local tool making sequential calls, not
meant for concurrent/multi-user throughput.
"""

from __future__ import annotations

import time
from collections import deque


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token for English). Good enough for
    rate-limiting headroom decisions; not meant to be exact."""
    return max(1, len(text) // 4)


class TokenRateLimiter:
    def __init__(self, tpm_budget: int = 8000, safety_margin: float = 0.85):
        # safety_margin leaves headroom below the stated limit, since our
        # token estimate is approximate and actual tokenizers vary by model.
        self.budget = int(tpm_budget * safety_margin)
        self.window_seconds = 60
        self._usage: deque[tuple[float, int]] = deque()

    def _prune(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._usage and self._usage[0][0] < cutoff:
            self._usage.popleft()

    def _current_usage(self) -> int:
        self._prune()
        return sum(tokens for _, tokens in self._usage)

    def reserve(self, estimated_tokens: int, on_wait=None):
        """
        Blocks (sleeps) if needed so that adding `estimated_tokens` now
        would stay within budget for the trailing 60s window, then records
        the reservation. `on_wait(seconds)` is an optional callback so a UI
        can show a "waiting to respect rate limit..." message.
        """
        # A request larger than the entire per-minute budget can never fit,
        # no matter how long we wait. Without this guard the loop falls
        # through to self._usage[0] on an empty deque and raises IndexError --
        # an obscure crash in the middle of a batch, whose message says
        # nothing about rate limits. Proceeding is the right call: the API
        # may well accept it, and if it doesn't, its own 429 is a far clearer
        # error than a deque index failure here.
        if estimated_tokens >= self.budget:
            self._usage.append((time.monotonic(), estimated_tokens))
            return

        while True:
            self._prune()
            current = self._current_usage()
            if current + estimated_tokens <= self.budget:
                break
            if not self._usage:
                break  # nothing left to age out; belt-and-braces
            # sleep until the oldest entry ages out of the window
            oldest_time, _ = self._usage[0]
            wait_for = (oldest_time + self.window_seconds) - time.monotonic()
            wait_for = max(wait_for, 1.0)
            if on_wait:
                on_wait(wait_for)
            time.sleep(wait_for)

        self._usage.append((time.monotonic(), estimated_tokens))
