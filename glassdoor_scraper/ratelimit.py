"""A global token-bucket rate limiter shared by the CLI batch runner and API workers.

The bucket refills continuously at ``rate`` tokens/second up to a ceiling of
``capacity`` tokens (the allowed burst). Every outbound request consumes one token;
if none are available the caller waits just long enough for one to refill.

The same bucket exposes a blocking :meth:`acquire` for synchronous callers (the
CLI) and a coroutine :meth:`acquire_async` for the asyncio worker pool. State is
mutated only inside a short, non-blocking critical section guarded by a
``threading.Lock`` -- the actual waiting always happens *outside* the lock, so the
sync and async paths can share one bucket safely.
"""

from __future__ import annotations

import asyncio
import threading
import time


class TokenBucket:
    """A thread-safe, monotonic-clock token bucket."""

    def __init__(self, rate: float, capacity: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._rate = float(rate)
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _reserve(self, tokens: float) -> float:
        """Reserve ``tokens`` and return how many seconds the caller must wait.

        The bucket is allowed to go negative; that debt simply pushes out the next
        caller's wait time, which keeps the *average* rate correct under contention.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            self._updated = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._tokens -= tokens
            if self._tokens >= 0:
                return 0.0
            return -self._tokens / self._rate

    def acquire(self, tokens: float = 1.0) -> None:
        """Block (synchronously) until ``tokens`` are available."""
        wait = self._reserve(tokens)
        if wait > 0:
            time.sleep(wait)

    async def acquire_async(self, tokens: float = 1.0) -> None:
        """Await until ``tokens`` are available, without blocking the event loop."""
        wait = self._reserve(tokens)
        if wait > 0:
            await asyncio.sleep(wait)

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity
