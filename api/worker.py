"""In-process asyncio worker pool that drains the SQLite job queue.

Each worker repeatedly claims the oldest queued job, runs the (blocking) scraper in
a thread so the event loop stays responsive, streams progress back into the job row,
then stores results with a TTL, updates the persistent ``seen_jobs`` dedup index,
and fires the webhook when one was requested.

Reliability guarantees:
  * bounded retries -- a failing job is requeued until ``max_attempts``, then moved
    to the terminal ``failed`` (dead-letter) state with the error captured;
  * restart safety -- jobs left ``running`` by a crashed process are requeued at
    startup, and a graceful SIGTERM stops new claims and requeues in-flight work
    (the scraper checks a cancel predicate at every page boundary);
  * shared rate limiting -- all workers pull tokens from the one Phase-1 token bucket.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from glassdoor_scraper.config import Settings
from glassdoor_scraper.exceptions import ScraperError
from glassdoor_scraper.health import ProxyHealthTracker
from glassdoor_scraper.ratelimit import TokenBucket
from glassdoor_scraper.reporting import compute_quality_report
from glassdoor_scraper.scraper import ProgressEvent, SearchResult, scrape_jobs

from .db import Database, utcnow
from .logging_ctx import job_log_context
from .metrics import Metrics
from .schemas import SearchRequest
from .webhooks import deliver_webhook

log = logging.getLogger("api.worker")


class WorkerPool:
    """Manages a fixed set of asyncio workers draining the job queue."""

    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        metrics: Metrics,
        health: ProxyHealthTracker,
        rate_limiter: TokenBucket,
        poll_interval: float = 0.5,
    ) -> None:
        self.db = db
        self.settings = settings
        self.metrics = metrics
        self.health = health
        self.rate_limiter = rate_limiter
        self.poll_interval = poll_interval
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # Track fire-and-forget webhook deliveries so they are not garbage-collected
        # before they finish, and can be awaited on shutdown.
        self._bg_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        """Requeue interrupted jobs and launch the worker tasks."""
        requeued = self.db.reset_running_jobs()
        if requeued:
            log.info("Requeued %d job(s) left running by a previous process.", len(requeued))
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._worker_loop(i))
            for i in range(self.settings.worker_count)
        ]
        self._refresh_gauges()
        log.info("Started %d worker(s).", self.settings.worker_count)

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Signal shutdown and wait for workers to finish or checkpoint."""
        if not self._tasks:
            return
        log.info("Shutting down worker pool (stops taking new jobs) ...")
        self._stopping.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True), timeout=timeout
            )
        except TimeoutError:
            log.warning("Workers did not stop within %.0fs; cancelling.", timeout)
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        # Let any in-flight webhook deliveries finish (bounded by their own retries).
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
        # Any job still marked running (e.g. cancelled mid-flight) goes back to queued.
        self.db.reset_running_jobs()
        log.info("Worker pool stopped.")

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def _refresh_gauges(self) -> None:
        counts = self.db.count_by_status()
        self.metrics.queue_depth.set(counts.get("queued", 0))
        self.metrics.running_jobs.set(counts.get("running", 0))
        self.metrics.block_rate.set(self.health.block_rate())

    async def _worker_loop(self, worker_id: int) -> None:
        while not self._stopping.is_set():
            job = await asyncio.to_thread(self.db.claim_next_job)
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.poll_interval
                    )
                except TimeoutError:
                    pass
                continue
            self._refresh_gauges()
            try:
                await self._process(job)
            except Exception:  # pragma: no cover - defensive: never kill the loop
                log.exception("Unhandled error processing job %s.", job.get("job_id"))
            self._refresh_gauges()

    async def _process(self, job: dict) -> None:
        job_id = job["job_id"]
        attempts = job["attempts"]
        max_attempts = job["max_attempts"]

        with job_log_context(job_id):
            try:
                request = SearchRequest.model_validate_json(job["request_body"])
            except Exception as exc:
                log.error("Job %s has an invalid stored body: %s", job_id, exc)
                self.db.mark_failed(job_id, error=f"invalid request body: {exc}")
                self.metrics.searches_failed.inc()
                return

            params = request.to_search_params()
            started = utcnow()
            log.info(
                "Processing job (attempt %d/%d): keyword=%r pages=%d",
                attempts,
                max_attempts,
                params.keyword,
                params.max_pages,
            )

            def progress_cb(event: ProgressEvent) -> None:
                if event.phase in ("page", "done"):
                    self.db.update_progress(
                        job_id,
                        pages_done=event.page,
                        jobs_collected=event.jobs_collected,
                    )

            def cancel_cb() -> bool:
                return self._stopping.is_set()

            try:
                result: SearchResult = await asyncio.to_thread(
                    scrape_jobs,
                    params,
                    settings=self.settings,
                    rate_limiter=self.rate_limiter,
                    observer=self.health,
                    progress=progress_cb,
                    cancel=cancel_cb,
                )
            except (ScraperError, ValueError) as exc:
                await self._handle_failure(job_id, attempts, max_attempts, str(exc))
                return

            # If shutdown cancelled the run mid-flight, requeue it untouched so it
            # resumes cleanly after restart rather than persisting partial results.
            if result.stats.cancelled and self._stopping.is_set():
                log.info("Job cancelled by shutdown; requeuing for later.")
                self.db.requeue(job_id, error="requeued after graceful shutdown")
                return

            self._finish_success(job_id, params, request, result, started)

    def _finish_success(
        self,
        job_id: str,
        params,
        request: SearchRequest,
        result: SearchResult,
        started,
    ) -> None:
        records = [job.to_dict() for job in result.jobs]
        quality = compute_quality_report(result.jobs, "jobs")
        quality_payload = {
            "total": quality.total,
            "ghost_fields": quality.ghost_fields,
            "sparse_fields": [
                {"field": name, "pct": round(pct, 1)} for name, pct in quality.sparse_fields
            ],
        }
        expires_at = (utcnow() + timedelta(hours=self.settings.results_ttl_hours)).isoformat()

        self.db.save_results(
            job_id,
            records=records,
            stats=result.stats.as_dict(),
            quality=quality_payload,
            expires_at_iso=expires_at,
        )
        seen = self.db.record_seen(
            [job.job_id for job in result.jobs],
            keyword=params.keyword,
            location=result.stats.resolved_location_name or params.city,
            site=params.site,
        )
        self.db.mark_done(
            job_id,
            jobs_collected=result.stats.jobs_collected,
            pages_done=result.stats.pages_fetched,
        )

        self.metrics.searches_succeeded.inc()
        self.metrics.jobs_collected.inc(len(records))
        self.metrics.search_duration.observe((utcnow() - started).total_seconds())

        if quality.ghost_fields:
            log.warning(
                "ALERT: schema drift suspected - %d ghost field(s) in results: %s",
                len(quality.ghost_fields),
                ", ".join(quality.ghost_fields),
                extra={"alert": "schema_drift", "ghost_fields": quality.ghost_fields},
            )

        log.info(
            "Job done: %d jobs (%d new listings, %d repeat) over %d pages.",
            result.stats.jobs_collected,
            seen["new"],
            seen["repeat"],
            result.stats.pages_fetched,
        )

        if request.webhook_url:
            payload = {
                "job_id": job_id,
                "status": "done",
                "stats": result.stats.as_dict(),
                "quality": quality_payload,
                "results": records,
            }
            task = asyncio.create_task(self._deliver(job_id, request.webhook_url, payload))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _deliver(self, job_id: str, url: str, payload: dict) -> None:
        with job_log_context(job_id):
            ok = await deliver_webhook(
                url,
                payload,
                secret=self.settings.webhook_secret,
                timeout=self.settings.webhook_timeout,
                max_attempts=self.settings.webhook_max_attempts,
            )
            self.db.set_webhook_status(job_id, "delivered" if ok else "failed")
            self.metrics.webhook_deliveries.labels(
                outcome="delivered" if ok else "failed"
            ).inc()

    async def _handle_failure(
        self, job_id: str, attempts: int, max_attempts: int, error: str
    ) -> None:
        if attempts >= max_attempts:
            log.error("Job failed permanently after %d attempts: %s", attempts, error)
            self.db.mark_failed(job_id, error=error)
            self.metrics.searches_failed.inc()
        else:
            log.warning(
                "Job failed (attempt %d/%d): %s. Requeuing.", attempts, max_attempts, error
            )
            self.db.requeue(job_id, error=error)
