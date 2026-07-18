"""Exception types shared across the scraper package."""

from __future__ import annotations


class ScraperError(Exception):
    """Base class for all scraper-specific errors."""


class CloudflareBlockError(ScraperError):
    """Raised when Cloudflare (or DataDome) serves a challenge or block page."""


class SessionBootstrapError(ScraperError):
    """Raised when session bootstrap fails to establish cookies."""


class CircuitBreakerTripped(ScraperError):
    """Raised when consecutive Cloudflare blocks exceed the configured threshold."""


class LocationResolutionError(ScraperError):
    """Raised when a city name cannot be resolved to a Glassdoor location id."""
