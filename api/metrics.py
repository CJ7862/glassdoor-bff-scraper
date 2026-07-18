"""Prometheus metrics for the API service.

A dedicated registry (rather than the global default) keeps metrics isolated so the
test suite can build fresh app instances without ``Duplicated timeseries`` errors.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class Metrics:
    """Holds every metric the service exposes, bound to one registry."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.searches_submitted = Counter(
            "glassdoor_searches_submitted_total",
            "Number of search jobs accepted onto the queue.",
            registry=self.registry,
        )
        self.searches_succeeded = Counter(
            "glassdoor_searches_succeeded_total",
            "Number of search jobs that completed successfully.",
            registry=self.registry,
        )
        self.searches_failed = Counter(
            "glassdoor_searches_failed_total",
            "Number of search jobs that landed in the dead-letter (failed) state.",
            registry=self.registry,
        )
        self.jobs_collected = Counter(
            "glassdoor_jobs_collected_total",
            "Total number of job listings collected across all searches.",
            registry=self.registry,
        )
        self.requests_total = Counter(
            "glassdoor_proxy_requests_total",
            "Outbound requests to Glassdoor, labeled by outcome.",
            ["outcome"],
            registry=self.registry,
        )
        self.proxy_bytes = Counter(
            "glassdoor_proxy_bytes_total",
            "Approximate bytes downloaded through the proxy.",
            registry=self.registry,
        )
        self.webhook_deliveries = Counter(
            "glassdoor_webhook_deliveries_total",
            "Webhook delivery attempts, labeled by outcome.",
            ["outcome"],
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "glassdoor_queue_depth",
            "Current number of queued jobs.",
            registry=self.registry,
        )
        self.running_jobs = Gauge(
            "glassdoor_running_jobs",
            "Current number of running jobs.",
            registry=self.registry,
        )
        self.block_rate = Gauge(
            "glassdoor_rolling_block_rate",
            "Rolling Cloudflare block rate (0-1) across recent requests.",
            registry=self.registry,
        )
        self.search_duration = Histogram(
            "glassdoor_search_duration_seconds",
            "Wall-clock duration of a completed search job.",
            registry=self.registry,
            buckets=(1, 5, 10, 30, 60, 120, 300, 600),
        )

    def render(self) -> bytes:
        """Return the Prometheus text exposition for all metrics."""
        return generate_latest(self.registry)
