"""Centralized settings for the Glassdoor scraper package and API service.

Settings are read from environment variables (and an optional ``.env`` file) via
``pydantic-settings``. No secret is ever hard-coded here; proxy credentials and
the webhook signing secret come from the environment only.

Backward compatibility: the original single-file scraper honored
``GLASSDOOR_IMPERSONATE``, ``DATAIMPULSE_USER`` and ``DATAIMPULSE_PASS``. Those
exact names still work here (declared as ``validation_alias`` values) so existing
deployments do not break.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Glassdoor regional sites -> tldp country IDs. Kept here so both the scraper and
# the API schema validation can reference a single source of truth.
SITES: dict[str, int] = {
    "com": 1,
    "co.uk": 2,
    "ca": 3,
    "com.au": 4,
    "co.in": 115,
    "sg": 217,
    "de": 96,
    "fr": 84,
    "com.hk": 103,
    "co.nz": 160,
}

# DataImpulse residential proxy gateway (docs.dataimpulse.com).
#   Rotating HTTP/HTTPS: port 823 (new IP per request)
#   Sticky HTTP/HTTPS:   ports 10000-20000 (one IP pinned per port)
DATAIMPULSE_HOST_DEFAULT = "gw.dataimpulse.com"
DATAIMPULSE_ROTATING_PORT_DEFAULT = 823
DATAIMPULSE_STICKY_PORT_DEFAULT = 10000


class Settings(BaseSettings):
    """Runtime configuration for scraping, concurrency, storage, and the API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GLASSDOOR_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Anti-detection / fingerprint --------------------------------------
    impersonate: str = Field(
        default="chrome136",
        validation_alias=AliasChoices("GLASSDOOR_IMPERSONATE", "GLASSDOOR_IMPERSONATE_TARGET"),
        description="curl_cffi impersonation target that currently passes Cloudflare.",
    )
    # NoDecode: read the raw env string as-is (not JSON) so the validator below can
    # accept a plain comma-separated list like "chrome136,chrome142".
    impersonate_fallbacks: Annotated[list[str], NoDecode] = Field(
        default=["chrome136", "chrome142", "chrome145", "chrome133a", "chrome124"],
        description="Ordered fingerprint targets to try when the primary keeps getting blocked.",
    )

    # --- Proxy (DataImpulse) -----------------------------------------------
    proxy_user: str = Field(
        default="",
        validation_alias=AliasChoices("DATAIMPULSE_USER", "GLASSDOOR_PROXY_USER"),
    )
    proxy_pass: str = Field(
        default="",
        validation_alias=AliasChoices("DATAIMPULSE_PASS", "GLASSDOOR_PROXY_PASS"),
    )
    proxy_host: str = Field(default=DATAIMPULSE_HOST_DEFAULT)
    proxy_rotating_port: int = Field(default=DATAIMPULSE_ROTATING_PORT_DEFAULT)
    proxy_sticky_port: int = Field(default=DATAIMPULSE_STICKY_PORT_DEFAULT)
    proxy_country: str = Field(default="us", description="Default 2-letter proxy geo country.")

    # --- Request pacing / retries ------------------------------------------
    delay_min: float = Field(default=3.0, ge=0.0)
    delay_max: float = Field(default=5.0, ge=0.0)
    request_timeout: int = Field(default=20, ge=1)
    max_retries: int = Field(default=3, ge=1)
    backoff_base: float = Field(default=5.0, ge=0.0)

    # --- Global token-bucket rate limiter ----------------------------------
    rate_limit_per_sec: float = Field(
        default=1.0,
        gt=0.0,
        description="Sustained requests/sec allowed across all workers combined.",
    )
    rate_limit_burst: float = Field(
        default=3.0,
        gt=0.0,
        description="Token-bucket burst capacity shared across all workers.",
    )

    # --- Concurrency / resilience ------------------------------------------
    worker_count: int = Field(default=3, ge=1)
    circuit_breaker_threshold: int = Field(
        default=5,
        ge=1,
        description="Stop a run after this many consecutive Cloudflare blocks.",
    )
    max_pages_per_request: int = Field(
        default=20,
        ge=1,
        description="Hard cap on pages a single API search may request.",
    )

    # --- Storage / TTL / dedup ---------------------------------------------
    db_path: str = Field(
        default="glassdoor_scraper.db",
        description="Path to the SQLite database file (queue + results + seen_jobs + api_keys).",
    )
    results_ttl_hours: int = Field(
        default=48,
        ge=1,
        description="How long full result rows are retained before background purge.",
    )
    idempotency_window_seconds: int = Field(
        default=300,
        ge=0,
        description="Window in which an identical submission returns the existing job_id.",
    )

    # --- Job retries / dead-letter -----------------------------------------
    job_max_attempts: int = Field(
        default=3,
        ge=1,
        description="Attempts before a failed job moves to the dead-letter state.",
    )

    # --- API service --------------------------------------------------------
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_results_page_size: int = Field(default=50, ge=1, le=500)
    max_payload_bytes: int = Field(
        default=16_384,
        ge=256,
        description="Reject request bodies larger than this at the boundary.",
    )

    # --- Auth / quotas ------------------------------------------------------
    require_api_key: bool = Field(
        default=True,
        description="When true, all /v1 endpoints require a valid API key.",
    )
    default_daily_quota: int = Field(
        default=500,
        ge=1,
        description="Default per-key daily search quota when a key does not set its own.",
    )
    default_rate_limit_per_min: int = Field(
        default=60,
        ge=1,
        description="Default per-key request rate limit (requests/minute).",
    )
    default_max_concurrent_jobs: int = Field(
        default=5,
        ge=1,
        description="Default per-key cap on simultaneously running jobs.",
    )

    # --- Webhooks -----------------------------------------------------------
    webhook_secret: str = Field(
        default="",
        validation_alias=AliasChoices("GLASSDOOR_WEBHOOK_SECRET", "WEBHOOK_SECRET"),
        description="HMAC signing secret for outbound webhook deliveries.",
    )
    webhook_timeout: float = Field(default=10.0, gt=0.0)
    webhook_max_attempts: int = Field(default=3, ge=1)

    # --- Observability / alerting ------------------------------------------
    log_level: str = Field(default="INFO")
    json_logs: bool = Field(
        default=True,
        description="Emit structured JSON logs (recommended for the API service).",
    )
    block_rate_alert_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Alert when the rolling Cloudflare block rate exceeds this fraction.",
    )
    block_rate_window: int = Field(
        default=20,
        ge=1,
        description="Number of recent requests used to compute the rolling block rate.",
    )

    @field_validator("impersonate_fallbacks", mode="before")
    @classmethod
    def _split_fallbacks(cls, value: object) -> object:
        """Allow the fallback list to be provided as a comma-separated string."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def delay(self) -> tuple[float, float]:
        """Return the (min, max) delay pair, normalized so min <= max."""
        lo, hi = self.delay_min, self.delay_max
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi)

    def ordered_fingerprints(self) -> list[str]:
        """Return the fallback chain with the pinned primary tried first, de-duplicated."""
        chain = [self.impersonate, *self.impersonate_fallbacks]
        seen: set[str] = set()
        ordered: list[str] = []
        for fp in chain:
            if fp and fp not in seen:
                seen.add(fp)
                ordered.append(fp)
        return ordered


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests that patch the environment)."""
    get_settings.cache_clear()
