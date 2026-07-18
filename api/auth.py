"""API-key authentication, per-key rate limiting, and quota enforcement.

Keys arrive as ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``, are hashed,
and looked up in the store. Each authenticated request is checked against the key's
per-minute rate limit (a small in-memory sliding window). Search submissions
additionally consume the key's daily quota and respect its max-concurrent-jobs cap.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from .db import Database
from .security import hash_api_key


@dataclass
class AuthContext:
    """The authenticated caller's identity and limits for the current request."""

    api_key_id: str | None
    name: str
    daily_quota: int
    rate_limit_per_min: int
    max_concurrent_jobs: int
    anonymous: bool = False


class RateLimitStore:
    """In-memory per-key sliding-window request counter (requests per minute)."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit_per_min: int) -> bool:
        """Record a request and return True if the key is within its limit."""
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            events = self._events[key]
            while events and events[0] < window_start:
                events.popleft()
            if len(events) >= limit_per_min:
                return False
            events.append(now)
            return True


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key.strip()
    return None


def require_api_key(request: Request) -> AuthContext:
    """FastAPI dependency: authenticate the caller and enforce the per-minute limit."""
    settings = request.app.state.settings
    db: Database = request.app.state.db
    rate_store: RateLimitStore = request.app.state.rate_store

    if not settings.require_api_key:
        ctx = AuthContext(
            api_key_id=None,
            name="anonymous",
            daily_quota=settings.default_daily_quota,
            rate_limit_per_min=settings.default_rate_limit_per_min,
            max_concurrent_jobs=settings.default_max_concurrent_jobs,
            anonymous=True,
        )
    else:
        raw = _extract_key(request)
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing API key. Provide 'Authorization: Bearer <key>' or 'X-API-Key'.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        record = db.get_api_key_by_hash(hash_api_key(raw))
        if not record:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        ctx = AuthContext(
            api_key_id=record["id"],
            name=record["name"],
            daily_quota=record["daily_quota"],
            rate_limit_per_min=record["rate_limit_per_min"],
            max_concurrent_jobs=record["max_concurrent_jobs"],
        )

    limit_key = ctx.api_key_id or "anonymous"
    if not rate_store.check(limit_key, ctx.rate_limit_per_min):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({ctx.rate_limit_per_min} requests/minute).",
            headers={"Retry-After": "60"},
        )
    return ctx
