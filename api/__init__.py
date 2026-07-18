"""FastAPI service wrapping the Glassdoor scraper.

Exposes job submission, status/results polling, bulk export, HMAC-signed webhooks,
health and Prometheus metrics, backed by a SQLite job queue + transient result store
and an in-process asyncio worker pool.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
