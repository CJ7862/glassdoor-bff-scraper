"""The hardened Glassdoor scraping engine.

``scrape_jobs`` drives one search end to end: sticky-proxy bootstrap, location
resolution, then paginated collection over a rotating proxy. On top of the original
behavior it adds fingerprint fallback, a per-run circuit breaker, a shared
token-bucket rate limiter, cooperative cancellation, and structured progress events
so the CLI and the API can both render live progress.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .config import Settings, get_settings
from .exceptions import CloudflareBlockError, LocationResolutionError
from .health import ProxyHealthTracker
from .models import Job
from .parser import parse_jobs, parse_location_response, select_next_cursor
from .ratelimit import TokenBucket
from .session import (
    RequestOutcome,
    bootstrap_session,
    build_proxy_url,
    create_session,
    safe_request,
    set_api_headers,
    validate_json_response,
)

log = logging.getLogger(__name__)


@dataclass
class SearchParams:
    """All inputs that define a single Glassdoor search."""

    keyword: str
    city: str = ""
    location_id: int = 0
    location_name: str = ""
    location_type: str = "CITY"
    site: str = "com"
    max_pages: int = 2
    sort: str = "relevant_desc"
    country: str = "us"
    min_rating: float | None = None
    min_salary: int | None = None
    max_salary: int | None = None
    posted_days: int | None = None
    easy_apply_only: bool = False
    work_type: str | None = None


@dataclass
class SearchStats:
    """Per-search telemetry surfaced in the CLI summary and API status."""

    pages_requested: int = 0
    pages_fetched: int = 0
    jobs_collected: int = 0
    blocks: int = 0
    retries: int = 0
    invalid_responses: int = 0
    circuit_broken: bool = False
    cancelled: bool = False
    fingerprint_used: str = ""
    fingerprint_attempts: list[str] = field(default_factory=list)
    resolved_location_id: int = 0
    resolved_location_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages_requested": self.pages_requested,
            "pages_fetched": self.pages_fetched,
            "jobs_collected": self.jobs_collected,
            "blocks": self.blocks,
            "retries": self.retries,
            "invalid_responses": self.invalid_responses,
            "circuit_broken": self.circuit_broken,
            "cancelled": self.cancelled,
            "fingerprint_used": self.fingerprint_used,
            "fingerprint_attempts": list(self.fingerprint_attempts),
            "resolved_location_id": self.resolved_location_id,
            "resolved_location_name": self.resolved_location_name,
        }


@dataclass
class ProgressEvent:
    """A progress signal emitted during a search (consumed by CLI/API renderers)."""

    phase: str  # "bootstrap" | "location" | "page" | "done"
    message: str = ""
    page: int = 0
    total_pages: int = 0
    jobs_collected: int = 0


@dataclass
class SearchResult:
    """The outcome of a single search: collected jobs plus telemetry."""

    jobs: list[Job]
    stats: SearchStats


ProgressCallback = Callable[[ProgressEvent], None]
CancelCheck = Callable[[], bool]


def _emit(progress: ProgressCallback | None, event: ProgressEvent) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception:  # pragma: no cover - progress rendering must never break a run
        log.debug("Progress callback raised; ignoring.", exc_info=True)


def _build_payload(
    params: SearchParams,
    base_url: str,
    loc_id: int,
    loc_name: str,
    loc_type: str,
    page_num: int,
    page_cursor: str,
) -> dict[str, Any]:
    """Build the BFF ``jobSearchResultsQuery`` POST body for one page."""
    kw_slug = params.keyword.lower().replace(" ", "-")
    loc_slug = loc_name.lower().replace(" ", "-").replace(",", "")
    seo_url = f"{loc_slug}-{kw_slug}-jobs"
    kw_start = len(loc_slug) + 1
    param_url = f"IL.0,{len(loc_slug)}_IC{loc_id}_KO{kw_start},{kw_start + len(params.keyword)}"

    filters: list[dict[str, str]] = [{"filterKey": "sortBy", "values": params.sort}]
    if params.work_type is not None:
        filters.append({"filterKey": "remoteWorkType", "values": params.work_type})
    if params.min_rating is not None:
        filters.append({"filterKey": "minRating", "values": str(params.min_rating)})
    if params.min_salary is not None:
        filters.append({"filterKey": "minSalary", "values": str(params.min_salary)})
    if params.max_salary is not None:
        filters.append({"filterKey": "maxSalary", "values": str(params.max_salary)})
    if params.posted_days is not None:
        filters.append({"filterKey": "fromAge", "values": str(params.posted_days)})
    if params.easy_apply_only:
        filters.append({"filterKey": "applicationType", "values": "1"})

    return {
        "excludeJobListingIds": [],
        "filterParams": filters,
        "includeIndeedJobAttributes": True,
        "keyword": params.keyword,
        "locationId": loc_id,
        "locationType": loc_type,
        "numJobsToShow": 30,
        "originalPageUrl": f"{base_url}/Job/{seo_url}-SRCH_{param_url}.htm",
        "pageCursor": page_cursor,
        "pageNumber": page_num,
        "pageType": "SERP",
        "parameterUrlInput": param_url,
        "seoFriendlyUrlInput": seo_url,
        "seoUrl": True,
    }


def scrape_jobs(
    params: SearchParams,
    *,
    settings: Settings | None = None,
    rate_limiter: TokenBucket | None = None,
    observer: Callable[[RequestOutcome], None] | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    proxy_user: str | None = None,
    proxy_pass: str | None = None,
    debug: bool = False,
    reference_date: datetime | None = None,
) -> SearchResult:
    """Scrape Glassdoor job listings via the internal BFF API.

    Args:
        params: The search definition.
        settings: Config override (defaults to the process settings).
        rate_limiter: Shared token bucket; a token is taken before each request.
        observer: Per-request outcome observer (proxy health / metrics).
        progress: Optional progress-event callback for live rendering.
        cancel: Optional predicate; when it returns True the run stops and returns
            whatever has been collected so far (used for graceful shutdown).
        proxy_user / proxy_pass: DataImpulse creds (default from settings).
        debug: Enable curl_cffi debug output.
        reference_date: Anchor date for ``ageInDays`` -> ``posted_date`` (tests).

    Returns:
        A :class:`SearchResult` with the collected jobs and telemetry.
    """
    cfg = settings or get_settings()
    p_user = proxy_user if proxy_user is not None else cfg.proxy_user
    p_pass = proxy_pass if proxy_pass is not None else cfg.proxy_pass

    stats = SearchStats(pages_requested=params.max_pages)

    def counting_observer(outcome: RequestOutcome) -> None:
        if outcome.blocked:
            stats.blocks += 1
        if outcome.attempts > 1:
            stats.retries += outcome.attempts - 1
        if observer is not None:
            observer(outcome)

    fingerprints = cfg.ordered_fingerprints()
    last_block: CloudflareBlockError | None = None

    for index, fingerprint in enumerate(fingerprints):
        stats.fingerprint_attempts.append(fingerprint)
        try:
            result = _collect_with_fingerprint(
                params=params,
                fingerprint=fingerprint,
                cfg=cfg,
                stats=stats,
                proxy_user=p_user,
                proxy_pass=p_pass,
                observer=counting_observer,
                rate_limiter=rate_limiter,
                progress=progress,
                cancel=cancel,
                debug=debug,
                reference_date=reference_date,
            )
            stats.fingerprint_used = fingerprint
            if index > 0:
                log.info(
                    "Fingerprint fallback succeeded with '%s' (after %s).",
                    fingerprint,
                    ", ".join(fingerprints[:index]),
                )
            _emit(
                progress,
                ProgressEvent(
                    phase="done",
                    message="Search complete.",
                    total_pages=params.max_pages,
                    page=stats.pages_fetched,
                    jobs_collected=stats.jobs_collected,
                ),
            )
            return result
        except CloudflareBlockError as exc:
            last_block = exc
            if index + 1 < len(fingerprints):
                log.warning(
                    "Fingerprint '%s' persistently blocked. Falling back to '%s'.",
                    fingerprint,
                    fingerprints[index + 1],
                )
                continue
            log.error(
                "All %d fingerprints blocked (%s). Giving up on this search.",
                len(fingerprints),
                ", ".join(fingerprints),
            )

    # Every fingerprint was blocked before any data came back.
    raise last_block or CloudflareBlockError("All fingerprints blocked.")


def _collect_with_fingerprint(
    *,
    params: SearchParams,
    fingerprint: str,
    cfg: Settings,
    stats: SearchStats,
    proxy_user: str,
    proxy_pass: str,
    observer: Callable[[RequestOutcome], None],
    rate_limiter: TokenBucket | None,
    progress: ProgressCallback | None,
    cancel: CancelCheck | None,
    debug: bool,
    reference_date: datetime | None,
) -> SearchResult:
    """Run one full collection attempt with a specific impersonation target.

    Raises :class:`CloudflareBlockError` only when the run is blocked before any
    jobs are collected, which is the signal for the caller to try the next
    fingerprint. Once at least one page has been collected, a later block ends the
    run gracefully and returns the partial results instead of re-raising.
    """
    base_url = f"https://www.glassdoor.{params.site}"
    search_endpoint = f"{base_url}/job-search-next/bff/jobSearchResultsQuery"

    def take_token() -> None:
        if rate_limiter is not None:
            rate_limiter.acquire()

    # Phase 1: bootstrap on a sticky proxy (unique sessid per bootstrap).
    sticky_proxy = build_proxy_url(
        proxy_user, proxy_pass, sticky=True, country=params.country, settings=cfg
    )
    session = create_session(
        sticky_proxy, impersonate=fingerprint, debug=debug, settings=cfg
    )
    _emit(
        progress,
        ProgressEvent(
            phase="bootstrap",
            message=f"Establishing session (fingerprint {fingerprint}).",
            total_pages=params.max_pages,
        ),
    )
    take_token()
    csrf_token = bootstrap_session(session, base_url, settings=cfg)
    set_api_headers(session, base_url, csrf_token)

    # Resolve location if only a city name was provided.
    loc_id = params.location_id
    loc_name = params.location_name
    loc_type = params.location_type

    if params.city and not loc_id:
        _emit(
            progress,
            ProgressEvent(
                phase="location",
                message=f"Resolving location '{params.city}'.",
                total_pages=params.max_pages,
            ),
        )
        term = quote(params.city)
        loc_url = (
            f"{base_url}/findPopularLocationAjax.htm?maxLocationsToReturn=10&term={term}"
        )
        take_token()
        loc_resp = safe_request(
            session, "get", loc_url, observer=observer, settings=cfg
        )
        loc_data = validate_json_response(loc_resp, context="location resolution")
        if loc_data is None:
            # A block page here with no data collected -> let fallback try again.
            raise CloudflareBlockError(
                f"Location resolution blocked or unparseable for '{params.city}'."
            )
        loc_id, loc_type, loc_name = parse_location_response(loc_data, params.city)
        log.info("Resolved '%s' -> %s (ID: %d)", params.city, loc_name, loc_id)

    if not loc_id:
        raise LocationResolutionError("Provide either a city or a location id.")
    if not loc_name:
        loc_name = params.city or "jobs"

    stats.resolved_location_id = loc_id
    stats.resolved_location_name = loc_name

    # Phase 2: switch to a rotating proxy for data collection.
    rotating_proxy = build_proxy_url(
        proxy_user, proxy_pass, sticky=False, country=params.country, settings=cfg
    )
    if rotating_proxy:
        session.proxies = {"http": rotating_proxy, "https": rotating_proxy}

    all_jobs: list[Job] = []
    seen_ids: set[str] = set()
    page_cursor = ""
    consecutive_blocks = 0

    for page_num in range(params.max_pages):
        if cancel is not None and cancel():
            stats.cancelled = True
            log.info("Search cancelled before page %d.", page_num + 1)
            break

        _emit(
            progress,
            ProgressEvent(
                phase="page",
                message=f"Fetching page {page_num + 1} of {params.max_pages}.",
                page=page_num + 1,
                total_pages=params.max_pages,
                jobs_collected=len(all_jobs),
            ),
        )
        log.info("Fetching jobs page %d/%d ...", page_num + 1, params.max_pages)

        payload = _build_payload(
            params, base_url, loc_id, loc_name, loc_type, page_num, page_cursor
        )

        take_token()
        try:
            resp = safe_request(
                session, "post", search_endpoint, json=payload, observer=observer, settings=cfg
            )
        except CloudflareBlockError:
            consecutive_blocks += 1
            if not all_jobs and page_num == 0:
                # Blocked immediately with nothing collected -> trigger fallback.
                raise
            if consecutive_blocks >= cfg.circuit_breaker_threshold:
                stats.circuit_broken = True
                log.error(
                    "Circuit breaker tripped after %d consecutive blocks. Stopping run.",
                    consecutive_blocks,
                )
            else:
                log.error("Blocked by Cloudflare on page %d. Stopping.", page_num + 1)
            break

        data = validate_json_response(resp, context=f"jobs page {page_num + 1}")
        if data is None:
            stats.invalid_responses += 1
            consecutive_blocks += 1
            if consecutive_blocks >= cfg.circuit_breaker_threshold:
                stats.circuit_broken = True
                log.error(
                    "Circuit breaker tripped after %d consecutive invalid responses.",
                    consecutive_blocks,
                )
                break
            time.sleep(random.uniform(*cfg.delay))
            continue

        consecutive_blocks = 0
        page_jobs = parse_jobs(data, reference_date=reference_date)
        stats.pages_fetched += 1

        if not page_jobs:
            log.info("No more jobs found on page %d.", page_num + 1)
            break

        new_count = 0
        for job in page_jobs:
            if job.job_id and job.job_id not in seen_ids:
                seen_ids.add(job.job_id)
                all_jobs.append(job)
                new_count += 1

        stats.jobs_collected = len(all_jobs)
        log.info(
            "Page %d: %d listings (%d new, total unique: %d)",
            page_num + 1,
            len(page_jobs),
            new_count,
            len(all_jobs),
        )
        _emit(
            progress,
            ProgressEvent(
                phase="page",
                message=f"Collected {len(all_jobs)} jobs after page {page_num + 1}.",
                page=page_num + 1,
                total_pages=params.max_pages,
                jobs_collected=len(all_jobs),
            ),
        )

        page_cursor = select_next_cursor(data, page_num)
        if not page_cursor:
            log.info("No more pages available.")
            break

        if page_num + 1 < params.max_pages:
            time.sleep(random.uniform(*cfg.delay))

    stats.jobs_collected = len(all_jobs)
    return SearchResult(jobs=all_jobs, stats=stats)


def make_rate_limiter(settings: Settings | None = None) -> TokenBucket:
    """Construct the shared token-bucket rate limiter from settings."""
    cfg = settings or get_settings()
    return TokenBucket(rate=cfg.rate_limit_per_sec, capacity=cfg.rate_limit_burst)


def make_health_tracker(settings: Settings | None = None) -> ProxyHealthTracker:
    """Construct a proxy-health tracker wired to the configured alert threshold."""
    cfg = settings or get_settings()
    return ProxyHealthTracker(
        window=cfg.block_rate_window,
        alert_threshold=cfg.block_rate_alert_threshold,
    )
