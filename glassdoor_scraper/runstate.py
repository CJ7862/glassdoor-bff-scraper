"""Checkpoint/resume state for batch runs.

Each batch row's completion is recorded to a small JSON state file so a re-run with
``--resume`` skips rows that already finished. The file is written atomically after
every row, so an interrupted batch (Ctrl-C, crash, SIGTERM) can always be resumed
from exactly where it stopped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


def row_key(keyword: str, city: str, site: str, country: str, pages: int) -> str:
    """Return a stable identifier for a batch row (order-independent of dict repr)."""
    raw = f"{keyword}\x1f{city}\x1f{site}\x1f{country}\x1f{pages}".lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class RunState:
    """Tracks per-row completion for a batch run, persisted to ``path``."""

    path: str
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> RunState:
        """Load existing state from ``path`` (empty if the file does not exist)."""
        state = cls(path=path)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and isinstance(data.get("rows"), dict):
                    state.rows = data["rows"]
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Could not read run-state file %s: %s", path, exc)
        return state

    def is_done(self, key: str) -> bool:
        """Return True if the row completed successfully in a previous run."""
        entry = self.rows.get(key)
        return bool(entry and entry.get("status") == "done")

    def mark_done(self, key: str, jobs: int, meta: dict[str, Any] | None = None) -> None:
        self.rows[key] = {"status": "done", "jobs": jobs, **(meta or {})}
        self._flush()

    def mark_failed(self, key: str, error: str, meta: dict[str, Any] | None = None) -> None:
        self.rows[key] = {"status": "failed", "error": error, **(meta or {})}
        self._flush()

    def _flush(self) -> None:
        """Persist the state atomically (temp file + ``os.replace``)."""
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"rows": self.rows}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
