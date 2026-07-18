"""Glassdoor jobs scraper package.

Public surface: the search engine (``scrape_jobs``), its parameter/result types, the
normalized :class:`Job` model, config, and export/reporting helpers. Anything a
consumer or the API layer needs is re-exported here so imports stay stable even if
internal module boundaries move.
"""

from __future__ import annotations

from .config import SITES, Settings, get_settings, reset_settings_cache
from .exceptions import (
    CircuitBreakerTripped,
    CloudflareBlockError,
    LocationResolutionError,
    ScraperError,
    SessionBootstrapError,
)
from .export import (
    export_csv,
    export_json,
    jobs_to_csv_str,
    jobs_to_dicts,
    jobs_to_json_str,
)
from .health import ProxyHealthSnapshot, ProxyHealthTracker
from .models import Job, normalize_pay_period
from .parser import (
    POSTED_MAP,
    RATING_MAP,
    SORT_MAP,
    WORK_TYPE_MAP,
    parse_jobs,
    select_next_cursor,
)
from .ratelimit import TokenBucket
from .reporting import QualityReport, compute_quality_report, format_report_plaintext
from .scraper import (
    ProgressEvent,
    SearchParams,
    SearchResult,
    SearchStats,
    make_health_tracker,
    make_rate_limiter,
    scrape_jobs,
)

__version__ = "1.0.0"

__all__ = [
    "SITES",
    "Settings",
    "get_settings",
    "reset_settings_cache",
    "ScraperError",
    "CloudflareBlockError",
    "SessionBootstrapError",
    "CircuitBreakerTripped",
    "LocationResolutionError",
    "Job",
    "normalize_pay_period",
    "SearchParams",
    "SearchResult",
    "SearchStats",
    "ProgressEvent",
    "scrape_jobs",
    "make_rate_limiter",
    "make_health_tracker",
    "TokenBucket",
    "ProxyHealthTracker",
    "ProxyHealthSnapshot",
    "parse_jobs",
    "select_next_cursor",
    "SORT_MAP",
    "RATING_MAP",
    "POSTED_MAP",
    "WORK_TYPE_MAP",
    "compute_quality_report",
    "format_report_plaintext",
    "QualityReport",
    "export_csv",
    "export_json",
    "jobs_to_csv_str",
    "jobs_to_json_str",
    "jobs_to_dicts",
    "__version__",
]
