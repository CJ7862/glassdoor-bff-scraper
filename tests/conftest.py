"""Shared pytest fixtures and network-free test doubles.

Nothing here touches the real network. HTTP is simulated with :class:`FakeResponse`
and the scraper engine is monkeypatched at its module boundary in the tests that
exercise the CLI/API so no request ever leaves the process.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from glassdoor_scraper.config import Settings, reset_settings_cache

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture from the tests/fixtures directory."""
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


class FakeResponse:
    """A minimal stand-in for a curl_cffi response used in unit tests."""

    def __init__(
        self,
        data: Any = None,
        *,
        text: str | None = None,
        status_code: int = 200,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._data = data
        if text is not None:
            self.text = text
        elif data is not None:
            self.text = json.dumps(data)
        else:
            self.text = ""
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if headers:
            self.headers.update(headers)
        self.content = self.text.encode("utf-8")

    def json(self) -> Any:
        if self._data is None:
            raise ValueError("no json body")
        return self._data

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    """A stand-in curl_cffi session: a headers dict, cookies, and a proxies slot."""

    def __init__(self, fingerprint: str = "chrome136") -> None:
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.proxies: dict[str, str] | None = None
        self.fingerprint = fingerprint

    def get(self, url: str, **kwargs: Any) -> FakeResponse:  # pragma: no cover
        return FakeResponse({}, content_type="application/json")

    def post(self, url: str, **kwargs: Any) -> FakeResponse:  # pragma: no cover
        return FakeResponse({}, content_type="application/json")


@pytest.fixture
def bff_page1() -> Any:
    return load_fixture("bff_page1.json")


@pytest.fixture
def bff_variant() -> Any:
    return load_fixture("bff_variant.json")


@pytest.fixture
def location_response() -> Any:
    return load_fixture("location.json")


@pytest.fixture
def test_settings() -> Settings:
    """A deterministic Settings instance for engine-level tests (no proxy, no delay)."""
    return Settings(
        impersonate="chrome136",
        impersonate_fallbacks=["chrome142"],
        proxy_user="",
        proxy_pass="",
        delay_min=0.0,
        delay_max=0.0,
        max_retries=2,
        backoff_base=0.0,
        rate_limit_per_sec=1000.0,
        rate_limit_burst=1000.0,
        circuit_breaker_threshold=2,
    )


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Ensure each test starts and ends with a clean settings cache."""
    reset_settings_cache()
    yield
    reset_settings_cache()
