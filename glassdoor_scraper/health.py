"""Proxy-health and block-rate tracking.

A :class:`ProxyHealthTracker` is a request observer (see ``session.RequestOutcome``)
that accumulates success/block/error counts and rough bandwidth, and maintains a
rolling window of recent outcomes so the *current* Cloudflare block rate is visible
before it tanks a whole batch. When the rolling block rate crosses a configured
threshold it fires an alert (a structured log line by default) -- this is the early
warning that the pinned fingerprint has stopped passing.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .session import RequestOutcome

log = logging.getLogger(__name__)

# Called with (block_rate, window_size) when the rolling block rate crosses the
# threshold. Defaults to a structured warning log.
AlertHook = Callable[[float, int], None]
# Called with each RequestOutcome so external systems (Prometheus) can observe.
MetricsHook = Callable[[RequestOutcome], None]


@dataclass
class ProxyHealthSnapshot:
    """A point-in-time view of proxy health, safe to serialize."""

    total_requests: int = 0
    successes: int = 0
    blocks: int = 0
    errors: int = 0
    bytes_downloaded: int = 0
    rolling_block_rate: float = 0.0
    rolling_window: int = 0

    def as_dict(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "successes": self.successes,
            "blocks": self.blocks,
            "errors": self.errors,
            "bytes_downloaded": self.bytes_downloaded,
            "rolling_block_rate": round(self.rolling_block_rate, 4),
            "rolling_window": self.rolling_window,
        }


@dataclass
class ProxyHealthTracker:
    """Thread-safe accumulator of proxy-health statistics and a block-rate alarm."""

    window: int = 20
    alert_threshold: float = 0.5
    on_alert: AlertHook | None = None
    metrics_hook: MetricsHook | None = None

    _total: int = field(default=0, init=False)
    _success: int = field(default=0, init=False)
    _blocked: int = field(default=0, init=False)
    _errors: int = field(default=0, init=False)
    _bytes: int = field(default=0, init=False)
    _recent: deque[bool] = field(default_factory=deque, init=False)
    _alerting: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __call__(self, outcome: RequestOutcome) -> None:
        """Observer entry point: record one request outcome."""
        self.record(outcome)

    def record(self, outcome: RequestOutcome) -> None:
        """Update counters and rolling window, then check the block-rate alarm."""
        should_alert = False
        rate = 0.0
        window_len = 0
        with self._lock:
            self._total += 1
            self._bytes += max(0, outcome.response_bytes)
            if outcome.blocked:
                self._blocked += 1
            elif outcome.success:
                self._success += 1
            else:
                self._errors += 1

            self._recent.append(outcome.blocked)
            while len(self._recent) > self.window:
                self._recent.popleft()

            window_len = len(self._recent)
            if window_len:
                rate = sum(1 for b in self._recent if b) / window_len

            # Fire the alarm only on the transition into the alerting state so we do
            # not spam a line per request while a block wave is ongoing.
            if window_len >= min(self.window, 5) and rate >= self.alert_threshold:
                if not self._alerting:
                    self._alerting = True
                    should_alert = True
            elif rate < self.alert_threshold:
                self._alerting = False

        if self.metrics_hook is not None:
            try:
                self.metrics_hook(outcome)
            except Exception:  # pragma: no cover - metrics must never break scraping
                log.debug("Metrics hook raised; ignoring.", exc_info=True)

        if should_alert:
            self._fire_alert(rate, window_len)

    def _fire_alert(self, rate: float, window_len: int) -> None:
        if self.on_alert is not None:
            try:
                self.on_alert(rate, window_len)
                return
            except Exception:  # pragma: no cover
                log.debug("Alert hook raised; falling back to log.", exc_info=True)
        log.warning(
            "ALERT: Cloudflare block rate %.0f%% over last %d requests "
            "exceeds threshold %.0f%%. The pinned fingerprint may have stopped passing.",
            rate * 100,
            window_len,
            self.alert_threshold * 100,
            extra={"alert": "block_rate", "block_rate": rate, "window": window_len},
        )

    def block_rate(self) -> float:
        """Return the current rolling block rate (0.0-1.0)."""
        with self._lock:
            if not self._recent:
                return 0.0
            return sum(1 for b in self._recent if b) / len(self._recent)

    def snapshot(self) -> ProxyHealthSnapshot:
        """Return an immutable snapshot of the current statistics."""
        with self._lock:
            window_len = len(self._recent)
            rate = (
                sum(1 for b in self._recent if b) / window_len if window_len else 0.0
            )
            return ProxyHealthSnapshot(
                total_requests=self._total,
                successes=self._success,
                blocks=self._blocked,
                errors=self._errors,
                bytes_downloaded=self._bytes,
                rolling_block_rate=rate,
                rolling_window=window_len,
            )
