"""Parsing of Glassdoor's BFF JSON into normalized :class:`Job` records.

Everything that knows the *shape* of Glassdoor's undocumented BFF payload lives
here and nowhere else, so a future connector for another job board only has to
produce ``Job`` objects without touching the rest of the package. All extraction is
defensive: the BFF nests its data differently across payload versions, so each
lookup tolerates several known nesting variants and degrades to empty values rather
than raising.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .models import (
    PAY_PERIOD_UNKNOWN,
    Job,
    infer_pay_period_from_amount,
    normalize_pay_period,
)

log = logging.getLogger(__name__)

# CLI-facing filter maps (user choice -> BFF value). Kept next to the payload logic
# they feed so they stay in sync with the parser.
SORT_MAP = {"relevant": "relevant_desc", "date": "date_desc"}
RATING_MAP: dict[str, float | None] = {
    "any": None,
    "1": 1.0,
    "2": 2.0,
    "3": 3.0,
    "4": 4.0,
}
POSTED_MAP: dict[str, int | None] = {
    "any": None,
    "1d": 1,
    "3d": 3,
    "1w": 7,
    "2w": 14,
    "1m": 30,
}
LOC_TYPE_MAP = {"C": "CITY", "S": "STATE", "N": "COUNTRY", "M": "METRO"}
WORK_TYPE_MAP = {"remote": "1", "onsite": "0"}


def _first_present(data: Any, paths: list[Callable[[Any], Any]]) -> Any:
    """Return the first non-empty value produced by any of ``paths``.

    Each path is a callable that indexes into ``data``; ``KeyError``/``TypeError``
    /``IndexError`` from a mismatched shape are swallowed so the next variant is
    tried. Returns ``None`` when no path matches.
    """
    for path_fn in paths:
        try:
            result = path_fn(data)
        except (KeyError, TypeError, IndexError):
            continue
        if result:
            return result
    return None


def extract_listings(data: Any) -> list[dict]:
    """Extract the raw job-listing dicts from a BFF response, tolerating variants."""
    listings = _first_present(
        data,
        [
            lambda d: d["data"]["jobListings"]["jobListings"],
            lambda d: d["data"]["jobListings"],
            lambda d: d["jobListings"]["jobListings"],
            lambda d: d["jobListings"],
        ],
    )
    if isinstance(listings, list):
        return [item for item in listings if isinstance(item, dict)]
    return []


def _extract_pay(header: dict) -> tuple[str, str, str, str]:
    """Return ``(salary_min, salary_max, currency, pay_period)`` from a header.

    ``pay_period`` prefers the explicit BFF token; when absent it is inferred from
    the magnitude of the pay figures so hourly (e.g. 40-60) and annual (e.g. 110000)
    salaries are always distinguishable.
    """
    pay = header.get("payPeriodAdjustedPay") or {}
    if not isinstance(pay, dict):
        pay = {}
    p10 = pay.get("p10")
    p90 = pay.get("p90")

    salary_min = str(int(p10)) if isinstance(p10, (int, float)) and p10 > 0 else ""
    salary_max = str(int(p90)) if isinstance(p90, (int, float)) and p90 > 0 else ""
    currency = header.get("payCurrency", "") or ""

    period = normalize_pay_period(header.get("payPeriod"))
    if period == PAY_PERIOD_UNKNOWN:
        period = normalize_pay_period(pay.get("payPeriod"))
    if period == PAY_PERIOD_UNKNOWN:
        # Fall back to a magnitude heuristic using whichever figure is present.
        reference = p10 if isinstance(p10, (int, float)) and p10 > 0 else p90
        period = infer_pay_period_from_amount(reference)

    return salary_min, salary_max, currency, period


def parse_job(listing: dict, reference_date: datetime | None = None) -> Job:
    """Convert one raw BFF listing dict into a normalized :class:`Job`.

    ``reference_date`` anchors the ``ageInDays`` -> ``posted_date`` conversion; it
    defaults to :func:`datetime.now` and is injectable for deterministic tests.
    """
    now = reference_date or datetime.now()

    jobview = listing.get("jobview", listing)
    if not isinstance(jobview, dict):
        jobview = listing
    header = jobview.get("header", {}) or {}
    job_data = jobview.get("job", {}) or {}
    employer = header.get("employer", {}) or {}

    job = Job()
    job.job_id = str(job_data.get("listingId", "") or header.get("jobId", "") or "")
    job.title = header.get("jobTitleText", "") or ""
    job.company = header.get("employerNameFromSearch", "") or employer.get("name", "") or ""
    job.location = header.get("locationName", "") or ""

    job.salary_min, job.salary_max, job.salary_currency, job.pay_period = _extract_pay(header)

    age = header.get("ageInDays")
    if isinstance(age, (int, float)):
        job.posted_date = (now - timedelta(days=int(age))).strftime("%Y-%m-%d")

    job.easy_apply = bool(header.get("easyApply", False))

    ratings = employer.get("ratings", {}) or {}
    overall = ratings.get("overallRating", "")
    job.company_rating = (
        str(round(overall, 1)) if isinstance(overall, (int, float)) and overall > 0 else ""
    )

    job.job_url = header.get("seoJobLink", "") or ""

    desc_frags = (
        job_data.get("descriptionFragmentsText")
        or job_data.get("descriptionFragments")
        or []
    )
    if isinstance(desc_frags, list) and desc_frags:
        job.description_snippet = " ".join(str(f) for f in desc_frags if f).strip()

    return job


def parse_jobs(data: Any, reference_date: datetime | None = None) -> list[Job]:
    """Extract and parse every listing in a BFF response into :class:`Job` records.

    Listings without a usable ``job_id`` are dropped; duplicates within the same
    response are collapsed (first occurrence wins), matching the original behavior.
    """
    jobs: list[Job] = []
    seen: set[str] = set()
    for listing in extract_listings(data):
        job = parse_job(listing, reference_date=reference_date)
        if job.job_id and job.job_id not in seen:
            seen.add(job.job_id)
            jobs.append(job)
    return jobs


def _extract_pagination_cursors(data: Any) -> list[dict]:
    """Extract the pagination-cursor list from a BFF response, tolerating variants."""
    cursors = _first_present(
        data,
        [
            lambda d: d["data"]["jobListings"]["paginationCursors"],
            lambda d: d["data"]["paginationCursors"],
            lambda d: d["jobListings"]["paginationCursors"],
            lambda d: d["paginationCursors"],
        ],
    )
    if isinstance(cursors, list):
        return [c for c in cursors if isinstance(c, dict)]
    return []


def select_next_cursor(data: Any, page_num: int) -> str:
    """Return the pagination cursor for the page *after* ``page_num``.

    ``page_num`` is the current zero-based page index. Glassdoor labels its cursor
    entries with a one-based ``pageNumber``, so the current page is
    ``page_num + 1`` and the *next* page is ``page_num + 2``. We select the cursor
    whose ``pageNumber`` matches that value instead of blindly taking the last
    cursor (which pointed at the wrong page whenever the BFF returned a window of
    cursors rather than a simple next-pointer). If no exact match exists we fall
    back to the last cursor so pagination still advances.
    """
    cursors = _extract_pagination_cursors(data)
    if not cursors:
        return ""

    target_page = page_num + 2
    for entry in cursors:
        if entry.get("pageNumber") == target_page:
            cursor = entry.get("cursor", "")
            return str(cursor) if cursor else ""

    last = cursors[-1]
    cursor = last.get("cursor", "")
    return str(cursor) if cursor else ""


def parse_location_response(data: Any, city: str) -> tuple[int, str, str]:
    """Parse the ``findPopularLocationAjax`` response into ``(id, type, name)``.

    Args:
        data: The already-validated JSON body (a list of location dicts).
        city: The original query, used as a fallback display name.

    Returns:
        ``(location_id, location_type, location_name)``.

    Raises:
        ValueError: If no usable location is present in the response.
    """
    import re

    if not isinstance(data, list) or not data:
        raise ValueError(f"No location found for '{city}'")

    top = data[0]
    if not isinstance(top, dict) or "locationId" not in top:
        raise ValueError(f"No location found for '{city}'")

    loc_id = int(top["locationId"])
    loc_type = LOC_TYPE_MAP.get(top.get("locationType", "C"), "CITY")
    loc_name = re.sub(r"\s*\(.*?\)\s*", "", top.get("longName", city)).strip() or city
    return loc_id, loc_type, loc_name
