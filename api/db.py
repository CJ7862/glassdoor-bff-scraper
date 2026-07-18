"""Thin SQLite repository for the API service.

Everything the service persists lives here behind a narrow method surface -- job
queue, transient results (with TTL), the persistent ``seen_jobs`` dedup index, API
keys, and per-key daily usage. Keeping SQL in one place means the store can later be
swapped for Postgres/Supabase by reimplementing this class without touching the
endpoints or worker.

Concurrency model: one fresh connection per operation with WAL mode and a busy
timeout. WAL allows concurrent readers with a single writer; the busy timeout makes
competing writers wait rather than raise. This is safe across the mix of asyncio
tasks and worker threads the service uses, with no shared-connection locking to get
wrong.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

# Job lifecycle states.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"  # terminal dead-letter state after retries are exhausted


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (lexicographically sortable)."""
    return utcnow().isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    api_key_id        TEXT,
    status            TEXT NOT NULL,
    request_body      TEXT NOT NULL,
    body_hash         TEXT NOT NULL,
    idempotency_key   TEXT,
    webhook_url       TEXT,
    webhook_status    TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    pages_requested   INTEGER NOT NULL DEFAULT 0,
    pages_done        INTEGER NOT NULL DEFAULT 0,
    jobs_collected    INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_idem ON jobs(api_key_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_jobs_bodyhash ON jobs(api_key_id, body_hash, created_at);

CREATE TABLE IF NOT EXISTS results (
    job_id      TEXT PRIMARY KEY,
    records     TEXT NOT NULL,
    stats       TEXT,
    quality     TEXT,
    record_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_expires ON results(expires_at);

CREATE TABLE IF NOT EXISTS seen_jobs (
    listing_id    TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1,
    keyword       TEXT,
    location      TEXT,
    site          TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
    id                   TEXT PRIMARY KEY,
    key_hash             TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    daily_quota          INTEGER NOT NULL,
    rate_limit_per_min   INTEGER NOT NULL,
    max_concurrent_jobs  INTEGER NOT NULL,
    active               INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_key_usage (
    api_key_id  TEXT NOT NULL,
    day         TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, day)
);
"""


class Database:
    """A thin, thread-safe (connection-per-op) SQLite repository."""

    def __init__(self, path: str, busy_timeout_ms: int = 5000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # -- jobs / queue -------------------------------------------------------
    def create_job(
        self,
        *,
        job_id: str,
        api_key_id: str | None,
        request_body: dict[str, Any],
        body_hash: str,
        idempotency_key: str | None,
        webhook_url: str | None,
        max_attempts: int,
        pages_requested: int,
    ) -> None:
        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, api_key_id, status, request_body, body_hash,
                    idempotency_key, webhook_url, webhook_status, attempts,
                    max_attempts, pages_requested, pages_done, jobs_collected,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, 0, ?, ?)
                """,
                (
                    job_id,
                    api_key_id,
                    STATUS_QUEUED,
                    json.dumps(request_body),
                    body_hash,
                    idempotency_key,
                    webhook_url,
                    "pending" if webhook_url else None,
                    max_attempts,
                    pages_requested,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_by_idempotency_key(
        self, api_key_id: str | None, idempotency_key: str
    ) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE idempotency_key = ?
                  AND (api_key_id IS ? OR api_key_id = ?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (idempotency_key, api_key_id, api_key_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_recent_by_body_hash(
        self, api_key_id: str | None, body_hash: str, since_iso: str
    ) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE body_hash = ?
                  AND (api_key_id IS ? OR api_key_id = ?)
                  AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (body_hash, api_key_id, api_key_id, since_iso),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def claim_next_job(self) -> dict[str, Any] | None:
        """Atomically move the oldest queued job to running and return it."""
        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                UPDATE jobs
                SET status = ?, started_at = COALESCE(started_at, ?),
                    attempts = attempts + 1, updated_at = ?
                WHERE job_id = (
                    SELECT job_id FROM jobs
                    WHERE status = ?
                    ORDER BY created_at
                    LIMIT 1
                )
                RETURNING *
                """,
                (STATUS_RUNNING, now, now, STATUS_QUEUED),
            ).fetchone()
            conn.commit()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_progress(
        self, job_id: str, *, pages_done: int, jobs_collected: int
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE jobs
                SET pages_done = ?, jobs_collected = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (pages_done, jobs_collected, utcnow_iso(), job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_done(self, job_id: str, *, jobs_collected: int, pages_done: int) -> None:
        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, jobs_collected = ?, pages_done = ?,
                    finished_at = ?, updated_at = ?, error = NULL
                WHERE job_id = ?
                """,
                (STATUS_DONE, jobs_collected, pages_done, now, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def requeue(self, job_id: str, *, error: str | None = None) -> None:
        """Return a job to the queue for another attempt (bounded retry)."""
        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?, error = ?
                WHERE job_id = ?
                """,
                (STATUS_QUEUED, now, error, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_failed(self, job_id: str, *, error: str) -> None:
        """Move a job to the terminal dead-letter (failed) state."""
        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, finished_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (STATUS_FAILED, error, now, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_webhook_status(self, job_id: str, status: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE jobs SET webhook_status = ?, updated_at = ? WHERE job_id = ?",
                (status, utcnow_iso(), job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def count_active_for_key(self, api_key_id: str | None) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM jobs
                WHERE (api_key_id IS ? OR api_key_id = ?)
                  AND status IN (?, ?)
                """,
                (api_key_id, api_key_id, STATUS_QUEUED, STATUS_RUNNING),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def count_by_status(self) -> dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
            return {row["status"]: int(row["n"]) for row in rows}
        finally:
            conn.close()

    def reset_running_jobs(self) -> list[str]:
        """Requeue jobs left ``running`` by a previous process (crash/restart)."""
        now = utcnow_iso()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT job_id FROM jobs WHERE status = ?", (STATUS_RUNNING,)
            ).fetchall()
            ids = [r["job_id"] for r in rows]
            if ids:
                conn.executemany(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                    [(STATUS_QUEUED, now, jid) for jid in ids],
                )
                conn.commit()
            return ids
        finally:
            conn.close()

    # -- results ------------------------------------------------------------
    def save_results(
        self,
        job_id: str,
        *,
        records: list[dict[str, Any]],
        stats: dict[str, Any],
        quality: dict[str, Any],
        expires_at_iso: str,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO results (job_id, records, stats, quality, record_count, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    records = excluded.records,
                    stats = excluded.stats,
                    quality = excluded.quality,
                    record_count = excluded.record_count,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    job_id,
                    json.dumps(records),
                    json.dumps(stats),
                    json.dumps(quality),
                    len(records),
                    utcnow_iso(),
                    expires_at_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_results_meta(self, job_id: str) -> dict[str, Any] | None:
        """Return result metadata (no records payload) if present and unexpired."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT job_id, stats, quality, record_count, created_at, expires_at "
                "FROM results WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] <= utcnow_iso():
                return None
            return dict(row)
        finally:
            conn.close()

    def get_results(self, job_id: str) -> list[dict[str, Any]] | None:
        """Return the full record list if present and unexpired, else None."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT records, expires_at FROM results WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not row or row["expires_at"] <= utcnow_iso():
                return None
            data = json.loads(row["records"])
            return data if isinstance(data, list) else None
        finally:
            conn.close()

    def purge_expired_results(self, now_iso: str | None = None) -> int:
        now_iso = now_iso or utcnow_iso()
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM results WHERE expires_at <= ?", (now_iso,)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # -- seen_jobs dedup index ---------------------------------------------
    def record_seen(
        self,
        listing_ids: list[str],
        *,
        keyword: str = "",
        location: str = "",
        site: str = "",
    ) -> dict[str, int]:
        """Upsert seen listing ids; return counts of new vs repeat listings."""
        now = utcnow_iso()
        new = 0
        repeat = 0
        conn = self._connect()
        try:
            for lid in listing_ids:
                if not lid:
                    continue
                cur = conn.execute(
                    """
                    UPDATE seen_jobs
                    SET last_seen_at = ?, times_seen = times_seen + 1
                    WHERE listing_id = ?
                    """,
                    (now, lid),
                )
                if cur.rowcount:
                    repeat += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO seen_jobs
                            (listing_id, first_seen_at, last_seen_at, times_seen, keyword, location, site)
                        VALUES (?, ?, ?, 1, ?, ?, ?)
                        """,
                        (lid, now, now, keyword, location, site),
                    )
                    new += 1
            conn.commit()
            return {"new": new, "repeat": repeat}
        finally:
            conn.close()

    def get_seen(self, listing_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM seen_jobs WHERE listing_id = ?", (listing_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def count_seen(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM seen_jobs").fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    # -- api keys -----------------------------------------------------------
    def create_api_key(
        self,
        *,
        key_id: str,
        key_hash: str,
        name: str,
        daily_quota: int,
        rate_limit_per_min: int,
        max_concurrent_jobs: int,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO api_keys
                    (id, key_hash, name, daily_quota, rate_limit_per_min,
                     max_concurrent_jobs, active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    key_id,
                    key_hash,
                    name,
                    daily_quota,
                    rate_limit_per_min,
                    max_concurrent_jobs,
                    utcnow_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_api_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND active = 1",
                (key_hash,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_api_keys(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, daily_quota, rate_limit_per_min, "
                "max_concurrent_jobs, active, created_at FROM api_keys ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def revoke_api_key(self, key_id: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE api_keys SET active = 0 WHERE id = ?", (key_id,)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # -- usage / quota ------------------------------------------------------
    def increment_usage(self, api_key_id: str, day: str) -> int:
        """Increment and return today's usage count for a key."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO api_key_usage (api_key_id, day, count)
                VALUES (?, ?, 1)
                ON CONFLICT(api_key_id, day) DO UPDATE SET count = count + 1
                """,
                (api_key_id, day),
            )
            row = conn.execute(
                "SELECT count FROM api_key_usage WHERE api_key_id = ? AND day = ?",
                (api_key_id, day),
            ).fetchone()
            conn.commit()
            return int(row["count"]) if row else 0
        finally:
            conn.close()

    def get_usage(self, api_key_id: str, day: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT count FROM api_key_usage WHERE api_key_id = ? AND day = ?",
                (api_key_id, day),
            ).fetchone()
            return int(row["count"]) if row else 0
        finally:
            conn.close()
