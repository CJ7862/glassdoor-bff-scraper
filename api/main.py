"""FastAPI application for the Glassdoor scraper service.

Wires together the SQLite store, the asyncio worker pool, the shared rate limiter,
proxy-health tracking, and Prometheus metrics. Built via an ``create_app`` factory so
tests can spin up isolated instances.

Graceful shutdown: uvicorn translates SIGTERM into the lifespan shutdown phase, where
the worker pool stops claiming new jobs and requeues anything in flight, so a deploy
or restart never corrupts a run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from glassdoor_scraper.config import SITES, Settings, get_settings
from glassdoor_scraper.export import jobs_to_csv_str, jobs_to_json_str
from glassdoor_scraper.logging_setup import configure_logging
from glassdoor_scraper.models import Job
from glassdoor_scraper.parser import POSTED_MAP, RATING_MAP, SORT_MAP, WORK_TYPE_MAP
from glassdoor_scraper.scraper import make_health_tracker, make_rate_limiter

from . import __version__
from .auth import AuthContext, RateLimitStore, require_api_key
from .db import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Database,
)
from .logging_ctx import job_log_context
from .metrics import Metrics
from .schemas import (
    JobProgress,
    JobStatusResponse,
    ResultsResponse,
    SearchRequest,
    SubmitResponse,
)
from .webhooks import deliver_webhook  # noqa: F401  (re-exported for consumers/tests)
from .worker import WorkerPool

log = logging.getLogger("api")

# Directory holding the self-contained browser test console served at /ui.
STATIC_DIR = Path(__file__).parent / "static"


def _body_hash(request: SearchRequest) -> str:
    """Stable hash of the search-defining fields (ignores webhook/idempotency)."""
    payload = request.model_dump(exclude={"webhook_url", "idempotency_key"})
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _purge_loop(db: Database, interval_seconds: float, stop_event) -> None:
    """Background task: periodically delete expired result rows."""
    import asyncio

    while not stop_event.is_set():
        try:
            removed = await asyncio.to_thread(db.purge_expired_results)
            if removed:
                log.info("Purged %d expired result row(s).", removed)
        except Exception:  # pragma: no cover - defensive
            log.exception("Result purge failed.")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory."""
    import asyncio

    cfg = settings or get_settings()
    configure_logging(level=cfg.log_level, json_logs=cfg.json_logs)

    metrics = Metrics()

    def metrics_hook(outcome) -> None:
        if outcome.blocked:
            metrics.requests_total.labels(outcome="blocked").inc()
        elif outcome.success:
            metrics.requests_total.labels(outcome="success").inc()
        else:
            metrics.requests_total.labels(outcome="error").inc()
        metrics.proxy_bytes.inc(max(0, outcome.response_bytes))

    health = make_health_tracker(cfg)
    health.metrics_hook = metrics_hook

    db = Database(cfg.db_path)
    rate_limiter = make_rate_limiter(cfg)
    worker_pool = WorkerPool(
        db=db,
        settings=cfg,
        metrics=metrics,
        health=health,
        rate_limiter=rate_limiter,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop_event = asyncio.Event()
        worker_pool.start()
        purge_task = asyncio.create_task(
            _purge_loop(db, interval_seconds=3600.0, stop_event=stop_event)
        )
        log.info("API started (version %s).", __version__)
        try:
            yield
        finally:
            stop_event.set()
            await worker_pool.shutdown()
            purge_task.cancel()
            try:
                await purge_task
            except (asyncio.CancelledError, Exception):
                pass
            log.info("API shut down cleanly.")

    app = FastAPI(
        title="Glassdoor Jobs Scraper API",
        version=__version__,
        description="Submit Glassdoor job searches, poll status, fetch results, or receive signed webhooks.",
        lifespan=lifespan,
    )

    app.state.settings = cfg
    app.state.db = db
    app.state.metrics = metrics
    app.state.health = health
    app.state.rate_limiter = rate_limiter
    app.state.worker_pool = worker_pool
    app.state.rate_store = RateLimitStore()

    @app.middleware("http")
    async def add_context(request: Request, call_next):
        # Reject oversized bodies at the boundary before doing any work.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > cfg.max_payload_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Payload exceeds {cfg.max_payload_bytes} bytes."},
                    )
            except ValueError:
                pass

        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        with job_log_context(request_id):
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # -- endpoints ----------------------------------------------------------
    @app.post("/v1/searches", response_model=SubmitResponse, status_code=status.HTTP_202_ACCEPTED)
    async def submit_search(
        body: SearchRequest, auth: AuthContext = Depends(require_api_key)
    ) -> SubmitResponse:
        body_hash = _body_hash(body)

        # Idempotency: an explicit key, or an identical body within the window,
        # returns the existing job instead of scraping again.
        existing = None
        if body.idempotency_key:
            existing = db.find_by_idempotency_key(auth.api_key_id, body.idempotency_key)
        if existing is None and cfg.idempotency_window_seconds > 0:
            from datetime import timedelta

            from .db import utcnow

            since = (utcnow() - timedelta(seconds=cfg.idempotency_window_seconds)).isoformat()
            candidate = db.find_recent_by_body_hash(auth.api_key_id, body_hash, since)
            if candidate and candidate["status"] != STATUS_FAILED:
                existing = candidate
        if existing is not None and existing["status"] != STATUS_FAILED:
            return SubmitResponse(
                job_id=existing["job_id"],
                status=existing["status"],
                idempotent=True,
                created_at=existing["created_at"],
            )

        # Abuse guards: per-key concurrency cap and daily quota.
        active = db.count_active_for_key(auth.api_key_id)
        if active >= auth.max_concurrent_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many concurrent jobs (max {auth.max_concurrent_jobs}).",
            )
        usage_key = auth.api_key_id or "anonymous"
        usage = db.increment_usage(usage_key, date.today().isoformat())
        if usage > auth.daily_quota:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Daily search quota exceeded ({auth.daily_quota}).",
            )

        job_id = uuid.uuid4().hex
        db.create_job(
            job_id=job_id,
            api_key_id=auth.api_key_id,
            request_body=body.model_dump(),
            body_hash=body_hash,
            idempotency_key=body.idempotency_key,
            webhook_url=body.webhook_url,
            max_attempts=cfg.job_max_attempts,
            pages_requested=body.pages,
        )
        metrics.searches_submitted.inc()
        counts = db.count_by_status()
        metrics.queue_depth.set(counts.get(STATUS_QUEUED, 0))
        job = db.get_job(job_id)
        log.info("Accepted search job %s (keyword=%r).", job_id, body.keyword)
        return SubmitResponse(
            job_id=job_id,
            status=STATUS_QUEUED,
            idempotent=False,
            created_at=job["created_at"] if job else None,
        )

    @app.get("/v1/searches/{job_id}", response_model=JobStatusResponse)
    async def get_status(
        job_id: str, auth: AuthContext = Depends(require_api_key)
    ) -> JobStatusResponse:
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        _authorize_job(job, auth)
        meta = db.get_results_meta(job_id)
        return JobStatusResponse(
            job_id=job_id,
            status=job["status"],
            progress=JobProgress(
                pages_requested=job["pages_requested"],
                pages_done=job["pages_done"],
                jobs_collected=job["jobs_collected"],
            ),
            error=job["error"],
            attempts=job["attempts"],
            max_attempts=job["max_attempts"],
            webhook_status=job["webhook_status"],
            created_at=job["created_at"],
            updated_at=job["updated_at"],
            finished_at=job["finished_at"],
            results_available=meta is not None,
            results_expire_at=meta["expires_at"] if meta else None,
        )

    @app.get("/v1/searches/{job_id}/results", response_model=ResultsResponse)
    async def get_results(
        job_id: str,
        auth: AuthContext = Depends(require_api_key),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=cfg.api_results_page_size, ge=1, le=500),
    ) -> ResultsResponse:
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        _authorize_job(job, auth)
        records = db.get_results(job_id)
        if records is None:
            if job["status"] in (STATUS_QUEUED, STATUS_RUNNING):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Job is {job['status']}; results are not ready yet.",
                )
            if job["status"] == STATUS_FAILED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Job failed: {job['error']}",
                )
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Results have expired (past their TTL).",
            )
        total = len(records)
        start = (page - 1) * page_size
        end = start + page_size
        return ResultsResponse(
            job_id=job_id,
            total=total,
            page=page,
            page_size=page_size,
            results=records[start:end],
        )

    @app.get("/v1/searches/{job_id}/export")
    async def export_results(
        job_id: str,
        auth: AuthContext = Depends(require_api_key),
        format: str = Query(default="json", pattern="^(csv|json)$"),
    ) -> Response:
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        _authorize_job(job, auth)
        records = db.get_results(job_id)
        if records is None:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Results are not available (not ready, failed, or expired).",
            )
        jobs = [Job.from_dict(r) for r in records]
        if format == "csv":
            content = jobs_to_csv_str(jobs)
            media_type = "text/csv"
            filename = f"glassdoor_{job_id}.csv"
        else:
            content = jobs_to_json_str(jobs)
            media_type = "application/json"
            filename = f"glassdoor_{job_id}.json"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        counts = db.count_by_status()
        snap = health.snapshot()
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "workers": cfg.worker_count,
                "queue": {
                    "queued": counts.get(STATUS_QUEUED, 0),
                    "running": counts.get(STATUS_RUNNING, 0),
                    "done": counts.get(STATUS_DONE, 0),
                    "failed": counts.get(STATUS_FAILED, 0),
                },
                "seen_jobs": db.count_seen(),
                "proxy_health": snap.as_dict(),
            }
        )

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        # Reflect live queue depth in the exposition.
        counts = db.count_by_status()
        metrics.queue_depth.set(counts.get(STATUS_QUEUED, 0))
        metrics.running_jobs.set(counts.get(STATUS_RUNNING, 0))
        metrics.block_rate.set(health.block_rate())
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/v1/meta")
    async def meta() -> JSONResponse:
        """Expose the backend's own option lists so the test console stays in sync.

        Unauthenticated on purpose: it returns only static enum names (no data), so
        the browser console can populate its form before a key is entered.
        """
        return JSONResponse(
            {
                "sites": list(SITES.keys()),
                "sorts": list(SORT_MAP.keys()),
                "ratings": list(RATING_MAP.keys()),
                "posted": list(POSTED_MAP.keys()),
                "work_types": list(WORK_TYPE_MAP.keys()),
                "job_fields": Job.field_names(),
                "max_pages_per_request": cfg.max_pages_per_request,
                "require_api_key": cfg.require_api_key,
                "results_page_size": cfg.api_results_page_size,
            }
        )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/ui")

    @app.get("/ui", include_in_schema=False)
    async def ui() -> Response:
        console = STATIC_DIR / "console.html"
        if not console.is_file():
            raise HTTPException(status_code=404, detail="Test console not installed.")
        return FileResponse(console, media_type="text/html")

    return app


def _authorize_job(job: dict, auth: AuthContext) -> None:
    """Reject access to a job that belongs to a different API key.

    Anonymous mode (auth disabled) can see all jobs; otherwise a key may only see
    jobs it created.
    """
    if auth.anonymous:
        return
    if job.get("api_key_id") not in (None, auth.api_key_id):
        raise HTTPException(status_code=404, detail="Job not found.")


# A module-level app so ``uvicorn api.main:app`` and ``import api.main`` both work.
app = create_app()
