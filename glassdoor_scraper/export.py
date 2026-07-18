"""Serialization and atomic file export for :class:`Job` records.

Two concerns:
  * In-memory serialization (``jobs_to_json_str`` / ``jobs_to_csv_str``) used by the
    API to build downloadable payloads without touching disk.
  * Atomic file writes (``export_json`` / ``export_csv``) used by the CLI: data is
    written to a temp file in the same directory and then ``os.replace``-d over the
    target, so a crash mid-write never leaves a truncated output file.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
from collections.abc import Iterable

from .models import Job

log = logging.getLogger(__name__)


def jobs_to_dicts(jobs: Iterable[Job]) -> list[dict]:
    """Return a list of plain dicts for the given jobs."""
    return [job.to_dict() for job in jobs]


def jobs_to_json_str(jobs: Iterable[Job]) -> str:
    """Serialize jobs to a pretty-printed JSON string."""
    return json.dumps(jobs_to_dicts(jobs), indent=2, ensure_ascii=False)


def jobs_to_csv_str(jobs: Iterable[Job]) -> str:
    """Serialize jobs to a CSV string with the stable :class:`Job` column order."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=Job.field_names(), extrasaction="ignore"
    )
    writer.writeheader()
    for job in jobs:
        writer.writerow(job.to_dict())
    return buffer.getvalue()


def _atomic_write(path: str, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up the temp file on any failure so we never litter the directory.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def export_json(jobs: list[Job], filename: str) -> None:
    """Atomically export jobs to a JSON file."""
    _atomic_write(filename, jobs_to_json_str(jobs))
    log.info("Saved %d jobs to %s", len(jobs), filename)


def export_csv(jobs: list[Job], filename: str) -> None:
    """Atomically export jobs to a CSV file."""
    _atomic_write(filename, jobs_to_csv_str(jobs))
    log.info("Saved %d jobs to %s", len(jobs), filename)
