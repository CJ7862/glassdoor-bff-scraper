"""Command-line interface for the Glassdoor scraper.

Preserves every flag and behavior of the original ``glassdoor_jobs.py`` CLI (single
search, batch mode, CSV/JSON export, the data-quality report) and runs as
``python -m glassdoor_scraper ...``. New on top of the original:

  * a rich, always-readable presentation layer (progress, summary + quality tables)
    that auto-falls back to plain text when piped, plus ``--no-color``,
  * ``--resume`` for checkpointed batch runs,
  * ``--impersonate`` to match the fingerprint override documented in the README,
  * a shared token-bucket rate limiter and proxy-health tracking.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import time

from .config import SITES, Settings, get_settings
from .exceptions import CloudflareBlockError, LocationResolutionError, SessionBootstrapError
from .export import export_csv, export_json
from .logging_setup import configure_logging
from .models import Job
from .parser import POSTED_MAP, RATING_MAP, SORT_MAP, WORK_TYPE_MAP
from .presenter import Presenter
from .reporting import compute_quality_report
from .runstate import RunState, row_key
from .scraper import (
    SearchParams,
    SearchResult,
    make_health_tracker,
    make_rate_limiter,
    scrape_jobs,
)

log = logging.getLogger("glassdoor_scraper.cli")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (kept equivalent to the original CLI)."""
    parser = argparse.ArgumentParser(
        prog="glassdoor_scraper",
        description="Scrape Glassdoor job listings via their internal BFF API with DataImpulse proxies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Single search:\n"
            '    python -m glassdoor_scraper -k "data engineer" --city "San Francisco"\n'
            '    python -m glassdoor_scraper -k "data scientist" --city "New York" --sort date\n'
            '    python -m glassdoor_scraper -k "devops" --city Berlin --site de --country de\n'
            "\n"
            "  Batch mode (CSV input):\n"
            "    python -m glassdoor_scraper --batch searches.csv --format both\n"
            "    CSV columns: keyword (required), city (required), site, country, pages\n"
            "\n"
            "Supported sites: " + ", ".join(SITES.keys()) + "\n"
            "Sort options: relevant, date\n"
            "Posted options: any, 1d, 3d, 1w, 2w, 1m\n"
            "Rating options: any, 1 (1+), 2 (2+), 3 (3+), 4 (4+)\n"
            "\n"
            "Proxy credentials are read from DATAIMPULSE_USER / DATAIMPULSE_PASS\n"
            "env vars unless overridden with --proxy-user / --proxy-pass.\n"
        ),
    )

    loc_group = parser.add_mutually_exclusive_group(required=True)
    loc_group.add_argument(
        "--city", "-c",
        help="City name (e.g. 'San Francisco'). Auto-resolves to a location ID.",
    )
    loc_group.add_argument(
        "--location-id", "-l", type=int,
        help="Glassdoor location ID. Use if you know the ID directly.",
    )
    loc_group.add_argument(
        "--batch", "-b",
        help="CSV file with job searches. Columns: keyword, city, site, country, pages.",
    )

    parser.add_argument("--keyword", "-k", default="", help="Job search keyword (e.g. 'data engineer').")
    parser.add_argument("--location-name", default="", help="Location name for the SEO URL (auto-set when using --city).")
    parser.add_argument("--site", default="com", choices=list(SITES.keys()), help="Glassdoor regional site (default: com).")
    parser.add_argument("--country", default="us", help="2-letter country code for DataImpulse geo-targeting (default: us).")
    parser.add_argument("--pages", "-p", type=int, default=2, help="Number of search result pages (30 jobs/page, default: 2).")
    parser.add_argument("--sort", "-s", choices=list(SORT_MAP.keys()), default="relevant", help="Sort order (default: relevant).")
    parser.add_argument("--work-type", "-w", choices=list(WORK_TYPE_MAP.keys()), default=None, help="Work type filter: remote or onsite (default: all).")
    parser.add_argument("--easy-apply", "-e", action="store_true", help="Show only Easy Apply jobs.")
    parser.add_argument("--rating", choices=list(RATING_MAP.keys()), default="any", help="Minimum company rating: any, 1, 2, 3, 4.")
    parser.add_argument("--min-salary", type=int, default=None, help="Minimum salary filter (e.g. 80000).")
    parser.add_argument("--max-salary", type=int, default=None, help="Maximum salary filter (e.g. 200000).")
    parser.add_argument("--posted", choices=list(POSTED_MAP.keys()), default="any", help="Date posted: any, 1d, 3d, 1w, 2w, 1m.")
    parser.add_argument("--output", "-o", default="glassdoor_jobs", help="Output filename without extension (default: glassdoor_jobs).")
    parser.add_argument("--format", "-f", choices=["csv", "json", "both"], default="csv", help="Output format (default: csv).")
    parser.add_argument("--delay-min", type=float, default=None, help="Minimum delay between requests in seconds (default: 3.0).")
    parser.add_argument("--delay-max", type=float, default=None, help="Maximum delay between requests in seconds (default: 5.0).")
    parser.add_argument("--proxy-user", default="", help="DataImpulse login (or set DATAIMPULSE_USER).")
    parser.add_argument("--proxy-pass", default="", help="DataImpulse password (or set DATAIMPULSE_PASS).")
    parser.add_argument("--impersonate", default="", help="Override the pinned curl_cffi fingerprint (e.g. chrome142).")
    parser.add_argument("--resume", action="store_true", help="Batch mode: skip rows already completed in a previous run.")
    parser.add_argument("--no-color", action="store_true", help="Disable colors and rich formatting (plain text output).")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging (raw log stream).")
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    """Return settings with any CLI overrides applied on top of the environment."""
    base = get_settings()
    overrides: dict[str, object] = {}
    if args.impersonate:
        overrides["impersonate"] = args.impersonate
    if args.proxy_user:
        overrides["proxy_user"] = args.proxy_user
    if args.proxy_pass:
        overrides["proxy_pass"] = args.proxy_pass
    if args.delay_min is not None:
        overrides["delay_min"] = args.delay_min
    if args.delay_max is not None:
        overrides["delay_max"] = args.delay_max
    if not overrides:
        return base
    return base.model_copy(update=overrides)


def _location_label(params: SearchParams, stats_name: str = "") -> str:
    """Human-friendly location label for the summary table."""
    if stats_name:
        return stats_name
    if params.city:
        return params.city
    if params.location_name:
        return params.location_name
    if params.location_id:
        return f"location ID {params.location_id}"
    return "-"


def _params_from_args(args: argparse.Namespace, *, keyword: str, city: str, site: str,
                      country: str, pages: int) -> SearchParams:
    """Build a SearchParams from CLI args plus per-row overrides (batch)."""
    return SearchParams(
        keyword=keyword,
        city=city,
        location_id=args.location_id or 0,
        location_name=args.location_name,
        site=site,
        max_pages=pages,
        sort=SORT_MAP[args.sort],
        country=country,
        min_rating=RATING_MAP[args.rating],
        min_salary=args.min_salary,
        max_salary=args.max_salary,
        posted_days=POSTED_MAP[args.posted],
        easy_apply_only=args.easy_apply,
        work_type=WORK_TYPE_MAP.get(args.work_type) if args.work_type else None,
    )


def _run_single(
    args: argparse.Namespace,
    settings: Settings,
    presenter: Presenter,
    params: SearchParams,
    rate_limiter,
    health,
    proxy_user: str,
    proxy_pass: str,
) -> SearchResult:
    """Run one search with a live progress display and return its result."""
    title = f"{params.keyword} in {_location_label(params)}"
    with presenter.search_progress(title, params.max_pages) as progress_sink:
        return scrape_jobs(
            params,
            settings=settings,
            rate_limiter=rate_limiter,
            observer=health,
            progress=progress_sink,
            proxy_user=proxy_user,
            proxy_pass=proxy_pass,
            debug=args.debug,
        )


def run_batch(
    args: argparse.Namespace,
    settings: Settings,
    presenter: Presenter,
    rate_limiter,
    health,
    proxy_user: str,
    proxy_pass: str,
) -> tuple[list[Job], list[dict]]:
    """Process multiple searches from a CSV file, with checkpoint/resume.

    Returns the combined job list (union of all rows in the batch file, drawing on
    checkpointed rows when resuming) and the per-row summary rows for the table.
    """
    with open(args.batch, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    state_path = f"{args.batch}.runstate.json"
    state = RunState.load(state_path)
    presenter.info(f"Batch mode: {len(rows)} searches from {args.batch}")
    if args.resume:
        presenter.info(f"Resume enabled: using checkpoint {state_path}")

    circuit_threshold = settings.circuit_breaker_threshold
    consecutive_blocked_searches = 0
    summary_rows: list[dict] = []

    for i, row in enumerate(rows, 1):
        keyword = (row.get("keyword") or "").strip()
        city = (row.get("city") or "").strip()
        if not keyword or not city:
            presenter.warn(f"Row {i}: missing keyword or city, skipping.")
            continue

        site = (row.get("site") or "").strip() or args.site
        country = (row.get("country") or "").strip() or args.country
        pages = int(row.get("pages") or 0) or args.pages
        key = row_key(keyword, city, site, country, pages)

        if args.resume and state.is_done(key):
            entry = state.rows.get(key, {})
            presenter.info(f"[{i}/{len(rows)}] Skipping (already done): '{keyword}' in '{city}'")
            summary_rows.append(
                {
                    "keyword": keyword,
                    "location": city,
                    "pages_fetched": entry.get("pages_fetched", 0),
                    "pages_requested": pages,
                    "jobs": entry.get("jobs", 0),
                    "blocks": entry.get("blocks", 0),
                    "retries": entry.get("retries", 0),
                    "status": "Skipped (resumed)",
                }
            )
            continue

        params = _params_from_args(
            args, keyword=keyword, city=city, site=site, country=country, pages=pages
        )
        title = f"{keyword} in {city}"
        presenter.info(
            f"[{i}/{len(rows)}] Searching '{keyword}' in '{city}' "
            f"(site={site}, country={country}, pages={pages})"
        )

        try:
            with presenter.search_progress(title, pages) as progress_sink:
                result = scrape_jobs(
                    params,
                    settings=settings,
                    rate_limiter=rate_limiter,
                    observer=health,
                    progress=progress_sink,
                    proxy_user=proxy_user,
                    proxy_pass=proxy_pass,
                    debug=args.debug,
                )
        except (CloudflareBlockError, LocationResolutionError, SessionBootstrapError, ValueError) as exc:
            presenter.error(f"[{i}/{len(rows)}] Failed for '{keyword}' in '{city}': {exc}")
            state.mark_failed(
                key,
                str(exc),
                meta={"keyword": keyword, "city": city, "pages_requested": pages},
            )
            summary_rows.append(
                {
                    "keyword": keyword,
                    "location": city,
                    "pages_fetched": 0,
                    "pages_requested": pages,
                    "jobs": 0,
                    "blocks": 0,
                    "retries": 0,
                    "status": "Failed",
                }
            )
            consecutive_blocked_searches += 1
            if isinstance(exc, CloudflareBlockError) and consecutive_blocked_searches >= circuit_threshold:
                presenter.error(
                    f"Circuit breaker: {consecutive_blocked_searches} consecutive blocked "
                    "searches. Aborting the rest of the batch to conserve proxy bandwidth."
                )
                break
            continue

        consecutive_blocked_searches = 0
        stats = result.stats
        status = "OK"
        if stats.circuit_broken:
            status = "Partial"
        elif stats.pages_fetched < pages and stats.jobs_collected > 0:
            status = "Partial"
        elif stats.jobs_collected == 0:
            status = "Partial"

        state.mark_done(
            key,
            stats.jobs_collected,
            meta={
                "keyword": keyword,
                "city": city,
                "pages_requested": pages,
                "pages_fetched": stats.pages_fetched,
                "blocks": stats.blocks,
                "retries": stats.retries,
                "records": [j.to_dict() for j in result.jobs],
            },
        )
        summary_rows.append(
            {
                "keyword": keyword,
                "location": city,
                "pages_fetched": stats.pages_fetched,
                "pages_requested": pages,
                "jobs": stats.jobs_collected,
                "blocks": stats.blocks,
                "retries": stats.retries,
                "status": status,
            }
        )
        presenter.success(
            f"[{i}/{len(rows)}] Collected {stats.jobs_collected} jobs for '{keyword}' in '{city}'."
        )

        if i < len(rows):
            pause = random.uniform(5, 10)
            log.info("Pausing %.1fs before next search ...", pause)
            time.sleep(pause)

    # Build the combined output from every row in the batch file, using checkpointed
    # records so a resumed run still produces a complete combined export.
    combined: list[Job] = []
    seen: set[str] = set()
    for row in rows:
        keyword = (row.get("keyword") or "").strip()
        city = (row.get("city") or "").strip()
        if not keyword or not city:
            continue
        site = (row.get("site") or "").strip() or args.site
        country = (row.get("country") or "").strip() or args.country
        pages = int(row.get("pages") or 0) or args.pages
        done_entry = state.rows.get(row_key(keyword, city, site, country, pages))
        if not done_entry or done_entry.get("status") != "done":
            continue
        for rec in done_entry.get("records", []):
            job = Job.from_dict(rec)
            if job.job_id and job.job_id not in seen:
                seen.add(job.job_id)
                combined.append(job)

    return combined, summary_rows


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    presenter = Presenter(no_color=args.no_color)

    # Logging levels: debug -> raw DEBUG stream; interactive non-debug -> quiet
    # (the pretty UI carries the info); piped non-debug -> INFO so logs stay useful.
    if args.debug:
        level = "DEBUG"
    elif presenter.interactive:
        level = "WARNING"
    else:
        level = "INFO"
    configure_logging(level=level, json_logs=False)

    settings = _settings_from_args(args)
    proxy_user = args.proxy_user or settings.proxy_user
    proxy_pass = args.proxy_pass or settings.proxy_pass

    if not proxy_user or not proxy_pass:
        presenter.warn(
            "No DataImpulse credentials provided. Running WITHOUT a proxy -- "
            "Cloudflare will almost certainly block a datacenter/home IP. "
            "Set DATAIMPULSE_USER/DATAIMPULSE_PASS or pass --proxy-user/--proxy-pass."
        )

    rate_limiter = make_rate_limiter(settings)
    health = make_health_tracker(settings)

    summary_rows: list[dict] = []

    if args.batch:
        if not os.path.isfile(args.batch):
            presenter.error(f"Batch file not found: {args.batch}")
            return 1
        jobs, summary_rows = run_batch(
            args, settings, presenter, rate_limiter, health, proxy_user, proxy_pass
        )
    else:
        if not args.keyword:
            parser.error("--keyword is required for single searches (not needed in batch mode).")

        params = _params_from_args(
            args,
            keyword=args.keyword,
            city=args.city or "",
            site=args.site,
            country=args.country,
            pages=args.pages,
        )
        try:
            result = _run_single(
                args, settings, presenter, params, rate_limiter, health, proxy_user, proxy_pass
            )
        except (SessionBootstrapError, CloudflareBlockError, LocationResolutionError, ValueError) as exc:
            presenter.error(f"Scraping failed: {exc}")
            return 1

        jobs = result.jobs
        stats = result.stats
        status = "OK" if stats.jobs_collected and not stats.circuit_broken else "Partial"
        summary_rows.append(
            {
                "keyword": params.keyword,
                "location": _location_label(params, stats.resolved_location_name),
                "pages_fetched": stats.pages_fetched,
                "pages_requested": stats.pages_requested,
                "jobs": stats.jobs_collected,
                "blocks": stats.blocks,
                "retries": stats.retries,
                "status": status,
            }
        )

    # Always show the summary and proxy-health line, even when nothing was found.
    presenter.summary_table(summary_rows)
    snap = health.snapshot()
    presenter.info(
        f"Proxy health: {snap.successes} succeeded, {snap.blocks} blocked, "
        f"{snap.errors} errored across {snap.total_requests} requests "
        f"(rolling block rate {snap.rolling_block_rate * 100:.0f}%)."
    )

    if not jobs:
        presenter.warn(
            "No jobs found. Try different keywords or location, or check proxy quality."
        )
        return 0

    report = compute_quality_report(jobs, "jobs")
    presenter.quality_report(report)

    out = args.output
    if args.format in ("csv", "both"):
        export_csv(jobs, f"{out}.csv")
        presenter.success(f"Saved {len(jobs)} jobs to {out}.csv")
    if args.format in ("json", "both"):
        export_json(jobs, f"{out}.json")
        presenter.success(f"Saved {len(jobs)} jobs to {out}.json")

    presenter.success(f"Done. Scraped {len(jobs)} jobs.")
    return 0
