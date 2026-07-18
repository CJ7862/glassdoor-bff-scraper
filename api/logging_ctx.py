"""Request/job id context for structured logs.

Wraps the scraper package's context variable so that every log line emitted while a
worker processes a job -- including lines from deep inside the scraper -- carries the
same ``request_id``. Used as a context manager around request handling and job
processing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from glassdoor_scraper.logging_setup import reset_request_id, set_request_id


@contextmanager
def job_log_context(request_id: str) -> Iterator[None]:
    """Bind ``request_id`` to all log lines emitted within the block."""
    token = set_request_id(request_id)
    try:
        yield
    finally:
        reset_request_id(token)
