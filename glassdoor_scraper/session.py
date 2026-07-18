"""Session construction, anti-detection, and resilient HTTP for the scraper.

This module preserves the exact anti-detection behavior of the original
single-file scraper -- the ``EXTRA_FP`` TLS/HTTP2 tweaks, the navigate->cors header
transition, the TCP options, and the challenge/DataDome detection heuristics -- and
adds:

  * per-bootstrap unique sticky session ids (uuid4 instead of a fixed label),
  * a configurable impersonation target so the fingerprint-fallback logic in
    ``scraper.py`` can rebuild a session with a different Chrome version,
  * an optional per-request observer hook for proxy-health / block-rate tracking.
"""

from __future__ import annotations

import logging
import random
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from curl_cffi import CurlOpt
from curl_cffi import requests as curl_requests

from .config import Settings, get_settings
from .exceptions import CloudflareBlockError

log = logging.getLogger(__name__)

# Extra TLS/HTTP2 fingerprint settings that real Chrome uses but curl_cffi does not
# enable by default. Missing any of these is itself a signal of a non-browser client.
EXTRA_FP: dict[str, Any] = {
    # Chrome randomizes TLS extension order on every request.
    "tls_permute_extensions": True,
    # GREASE (Generate Random Extensions And Sustain Extensibility).
    "tls_grease": True,
    # Chrome compresses certificates with brotli.
    "tls_cert_compression": "brotli",
    # Chrome's default TLS signature algorithms (in order).
    "tls_signature_algorithms": [
        "ecdsa_secp256r1_sha256",
        "rsa_pss_rsae_sha256",
        "rsa_pkcs1_sha256",
        "ecdsa_secp384r1_sha384",
        "rsa_pss_rsae_sha384",
        "rsa_pkcs1_sha384",
        "rsa_pss_rsae_sha512",
        "rsa_pkcs1_sha512",
    ],
}

# Markers that indicate a Cloudflare challenge or block page.
CF_CHALLENGE_MARKERS = [
    "Just a moment",
    "Checking your browser",
    "cf-browser-verification",
    "challenges.cloudflare.com",
    "_cf_chl_opt",
    "Attention Required! | Cloudflare",
    "Sorry, you have been blocked",
    "Enable JavaScript and cookies to continue",
]

# Markers for a DataDome interstitial.
DATADOME_MARKERS = [
    "datadome",
    "geo.captcha-delivery.com",
    "interstitial",
]

# Essential cookies that confirm a real Glassdoor session. gdId is the primary
# persistent identifier.
SESSION_COOKIES = ["gdId"]


@dataclass
class RequestOutcome:
    """Result of a single outbound request, reported to an optional observer."""

    url: str
    success: bool
    blocked: bool
    status_code: int | None
    response_bytes: int
    attempts: int
    error: str | None = None


# An observer is any callable that consumes a RequestOutcome (proxy health, metrics).
Observer = Callable[[RequestOutcome], None]


def build_proxy_url(
    proxy_user: str = "",
    proxy_pass: str = "",
    sticky: bool = False,
    country: str = "us",
    session_id: str | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Build a DataImpulse residential proxy URL.

    Sticky sessions pin the same IP for ~30 minutes (required during session
    bootstrap, where the cookie chain must stay on one IP). DataImpulse binds sticky
    IPs to ports 10000-20000 and additionally supports the ``sessid`` username
    parameter to request a stable IP label. Rotating sessions assign a new IP per
    request (port 823) -- used for data collection to distribute load.

    Args:
        proxy_user: DataImpulse login.
        proxy_pass: DataImpulse password.
        sticky: Use a sticky session (same IP for ~30 min).
        country: 2-letter country code for geo-targeting (cr parameter).
        session_id: Session label for sticky proxies. When ``None`` a unique uuid4
            label is generated so concurrent bootstraps never collide on one IP.
        settings: Optional settings override (host/ports); falls back to defaults.

    Returns:
        A proxy URL string, or ``None`` if credentials are missing.
    """
    if not proxy_user or not proxy_pass:
        return None

    cfg = settings or get_settings()

    # Targeting params live in the username: login__cr.<country>[;sessid.<id>]
    username = f"{proxy_user}__cr.{country}"
    if sticky:
        label = session_id or uuid.uuid4().hex[:16]
        username += f";sessid.{label}"
        port = cfg.proxy_sticky_port
    else:
        port = cfg.proxy_rotating_port

    return f"http://{username}:{proxy_pass}@{cfg.proxy_host}:{port}"


def create_session(
    proxy_url: str | None = None,
    impersonate: str | None = None,
    debug: bool = False,
    settings: Settings | None = None,
) -> curl_requests.Session:
    """Create a curl_cffi session with full Chrome TLS/HTTP2 impersonation.

    - Pins to ``impersonate`` (the settings default when not given).
    - Enables TLS extension randomization, GREASE, and brotli cert compression.
    - Lets ``default_headers=True`` generate headers matching the impersonated Chrome
      version, then adds navigation-specific headers for the initial page load.
    """
    cfg = settings or get_settings()
    target = impersonate or cfg.impersonate
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    # curl_cffi types ``impersonate`` as a fixed Literal and ``extra_fp``/``proxies``
    # as its own TypedDicts; our values are validated at runtime, so cast to Any.
    session: curl_requests.Session = curl_requests.Session(
        impersonate=cast(Any, target),
        proxies=cast(Any, proxies),
        extra_fp=cast(Any, EXTRA_FP),
        default_headers=True,
        timeout=cfg.request_timeout,
        debug=debug,
        curl_options={
            # TCP Fast Open: send data in the SYN packet to cut latency.
            CurlOpt.TCP_FASTOPEN: 1,
            # Keep TCP connections alive between requests.
            CurlOpt.TCP_KEEPALIVE: 1,
            CurlOpt.TCP_KEEPIDLE: 60,
            CurlOpt.TCP_KEEPINTVL: 30,
        },
    )

    # default_headers=True already sets User-Agent, Accept, Accept-Encoding,
    # Accept-Language, sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform.
    # Add navigation-specific headers for the initial homepage load.
    session.headers.update(
        {
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        }
    )

    return session


def is_challenge_page(resp: Any) -> bool:
    """Return True if a response is a Cloudflare challenge or DataDome block."""
    if resp.status_code in (403, 429, 503):
        # Cloudflare Managed Challenge signals via this header.
        if resp.headers.get("cf-mitigated") == "challenge":
            log.warning("Cloudflare Managed Challenge detected (cf-mitigated header).")
            return True

    text = resp.text[:5000]
    lowered = text.lower()
    for marker in CF_CHALLENGE_MARKERS + DATADOME_MARKERS:
        if marker.lower() in lowered:
            log.warning("Block page detected: found '%s' in response.", marker)
            return True

    # Small 403 with no challenge content = direct block.
    if resp.status_code == 403 and len(resp.text) < 50:
        log.warning("Direct 403 block (short response: %d bytes).", len(resp.text))
        return True

    return False


def validate_session(session: Any) -> bool:
    """Return True if the session holds real Glassdoor cookies after bootstrap."""
    cookie_names = set(session.cookies.keys())
    missing = [c for c in SESSION_COOKIES if c not in cookie_names]
    if missing:
        log.warning(
            "Session validation failed. Missing cookies: %s. Present: %s",
            missing,
            sorted(cookie_names),
        )
        return False
    log.info("Session validated. Cookies: %s", sorted(cookie_names))
    return True


def extract_csrf_token(html: str) -> str:
    """Extract a gd-csrf-token from page HTML, trying several known patterns."""
    match = re.search(r'"token"\s*:\s*"([^"]+)"', html)
    if match:
        token = match.group(1)
        log.debug("Extracted CSRF token: %s...", token[:20])
        return token

    match = re.search(r'gdCSRFToken\s*=\s*"([^"]+)"', html)
    if match:
        return match.group(1)

    match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
    if match:
        return match.group(1)

    return ""


def _report(observer: Observer | None, outcome: RequestOutcome) -> None:
    """Deliver a request outcome to the observer, swallowing observer errors."""
    if observer is None:
        return
    try:
        observer(outcome)
    except Exception:  # pragma: no cover - observers must never break scraping
        log.debug("Request observer raised; ignoring.", exc_info=True)


def safe_request(
    session: Any,
    method: str,
    url: str,
    max_retries: int | None = None,
    backoff_base: float | None = None,
    observer: Observer | None = None,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Any:
    """Make an HTTP request with retry logic and challenge detection.

    Retries with exponential backoff plus jitter on 403/429/503 or detected
    challenge pages. Raises :class:`CloudflareBlockError` once all retries are
    exhausted. Each terminal outcome (success, block, or transport error) is
    reported to ``observer`` for proxy-health / block-rate tracking.
    """
    cfg = settings or get_settings()
    retries = max_retries if max_retries is not None else cfg.max_retries
    base = backoff_base if backoff_base is not None else cfg.backoff_base
    kwargs.setdefault("timeout", cfg.request_timeout)

    for attempt in range(1, retries + 1):
        try:
            if method.lower() == "post":
                resp = session.post(url, **kwargs)
            else:
                resp = session.get(url, **kwargs)

            if is_challenge_page(resp):
                if attempt == retries:
                    _report(
                        observer,
                        RequestOutcome(
                            url=url,
                            success=False,
                            blocked=True,
                            status_code=resp.status_code,
                            response_bytes=len(resp.content or b""),
                            attempts=attempt,
                            error="challenge_page",
                        ),
                    )
                    break
                delay = base * (2 ** (attempt - 1)) + random.uniform(0, 2)
                log.warning(
                    "Attempt %d/%d blocked (HTTP %d). Retrying in %.1fs ...",
                    attempt,
                    retries,
                    resp.status_code,
                    delay,
                )
                time.sleep(delay)
                continue

            resp.raise_for_status()
            _report(
                observer,
                RequestOutcome(
                    url=url,
                    success=True,
                    blocked=False,
                    status_code=resp.status_code,
                    response_bytes=len(resp.content or b""),
                    attempts=attempt,
                ),
            )
            return resp

        except CloudflareBlockError:
            raise
        except Exception as exc:
            if attempt == retries:
                log.error("Request failed after %d attempts: %s", retries, exc)
                _report(
                    observer,
                    RequestOutcome(
                        url=url,
                        success=False,
                        blocked=False,
                        status_code=None,
                        response_bytes=0,
                        attempts=attempt,
                        error=str(exc),
                    ),
                )
                raise
            delay = base * (2 ** (attempt - 1)) + random.uniform(0, 2)
            log.warning(
                "Attempt %d/%d error: %s. Retrying in %.1fs ...",
                attempt,
                retries,
                exc,
                delay,
            )
            time.sleep(delay)

    raise CloudflareBlockError(
        f"All {retries} attempts blocked for {url}. "
        "Check proxy quality or increase delays."
    )


def bootstrap_session(
    session: Any,
    base_url: str,
    max_retries: int | None = None,
    settings: Settings | None = None,
) -> str:
    """Bootstrap a Glassdoor session: load homepage, validate cookies, extract CSRF.

    Cloudflare treats API endpoints (``sec-fetch-mode: cors``) differently from page
    navigations. The homepage may return a challenge, but BFF API calls often pass
    regardless. So bootstrap is best-effort: if the homepage loads, great; if not,
    proceed anyway and let the API calls try on their own.

    Returns:
        CSRF token string (may be empty if not found).
    """
    cfg = settings or get_settings()
    retries = max_retries if max_retries is not None else cfg.max_retries
    log.info("Bootstrapping session on %s ...", base_url)

    for attempt in range(1, retries + 1):
        try:
            resp = session.get(base_url, timeout=15)

            if is_challenge_page(resp):
                if attempt < retries:
                    delay = 5.0 * attempt + random.uniform(1, 3)
                    log.warning(
                        "Bootstrap attempt %d/%d got challenge page. Retrying in %.1fs ...",
                        attempt,
                        retries,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                log.warning(
                    "Bootstrap got challenge page on all %d attempts. "
                    "Proceeding anyway -- API endpoints may still work.",
                    retries,
                )
                return ""

            if len(resp.text) < 1000:
                log.warning(
                    "Bootstrap response suspiciously small (%d bytes).", len(resp.text)
                )
                time.sleep(3.0 * attempt)
                continue

            csrf_token = extract_csrf_token(resp.text)

            if validate_session(session):
                log.info("Session bootstrap successful.")
                return csrf_token

            log.warning(
                "Bootstrap loaded page but session cookies missing. "
                "Continuing with available cookies."
            )
            return csrf_token

        except Exception as exc:
            log.warning("Bootstrap attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(5.0 * attempt)

    log.warning(
        "Bootstrap failed after %d attempts. Proceeding anyway -- "
        "API endpoints may still work without homepage cookies.",
        retries,
    )
    return ""


def set_api_headers(session: Any, base_url: str, csrf_token: str = "") -> None:
    """Switch session headers from navigation to XHR/fetch (CORS) mode.

    Cloudflare treats ``sec-fetch-mode: navigate`` differently from ``cors``; the API
    calls must look like same-origin fetches from the Glassdoor React app.
    """
    session.headers.update(
        {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": base_url,
            "referer": f"{base_url}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
    )
    session.headers.pop("sec-fetch-user", None)
    session.headers.pop("upgrade-insecure-requests", None)

    if csrf_token:
        session.headers["gd-csrf-token"] = csrf_token


def validate_json_response(resp: Any, context: str = "") -> dict | list | None:
    """Validate that a response contains real JSON data, not a block page.

    Returns the parsed JSON (dict or list) on success, or ``None`` when the response
    is a challenge page, unexpected HTML, or unparseable.
    """
    content_type = resp.headers.get("content-type", "")

    if "text/html" in content_type and "application/json" not in content_type:
        if is_challenge_page(resp):
            log.warning(
                "API response is a challenge page%s.",
                f" ({context})" if context else "",
            )
            return None
        log.warning(
            "Unexpected text/html response for API call%s. Size: %d bytes.",
            f" ({context})" if context else "",
            len(resp.text),
        )
        return None

    try:
        data = resp.json()
        if isinstance(data, dict) and not data:
            log.warning("Empty JSON response%s.", f" ({context})" if context else "")
        return data
    except Exception as exc:
        log.error(
            "Failed to parse JSON%s: %s", f" ({context})" if context else "", exc
        )
        return None
