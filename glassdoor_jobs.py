"""Backward-compatible shim for the Glassdoor scraper.

The scraper now lives in the ``glassdoor_scraper`` package (see the README). This
thin shim keeps the original invocation working:

    python glassdoor_jobs.py -k "data engineer" --city "San Francisco"

and re-exports the most commonly imported public names so any external scripts that
did ``from glassdoor_jobs import scrape_jobs, Job`` keep working. Prefer the package
directly for new code:

    python -m glassdoor_scraper -k "data engineer" --city "San Francisco"
"""

from __future__ import annotations

from glassdoor_scraper import (  # noqa: F401 (re-exported for compatibility)
    SITES,
    Job,
    SearchParams,
    SearchResult,
    compute_quality_report,
    export_csv,
    export_json,
    scrape_jobs,
)
from glassdoor_scraper.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
