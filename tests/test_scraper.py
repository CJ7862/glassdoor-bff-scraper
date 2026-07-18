"""End-to-end scraper engine tests with the network fully mocked.

Every test replaces ``create_session``/``bootstrap_session``/``safe_request`` at the
scraper module boundary, so no real HTTP request is ever made. The real parser,
cursor selection, rate limiter, circuit breaker, and fingerprint-fallback logic run.
"""

from __future__ import annotations

import pytest

from glassdoor_scraper import scraper as scr
from glassdoor_scraper.exceptions import CloudflareBlockError
from glassdoor_scraper.scraper import SearchParams, scrape_jobs

from .conftest import FakeResponse, FakeSession

EMPTY_PAGE = {"data": {"jobListings": {"jobListings": [], "paginationCursors": []}}}


@pytest.fixture
def patched_engine(monkeypatch):
    """Patch session construction and bootstrap; tests supply their own safe_request."""
    monkeypatch.setattr(
        scr, "create_session", lambda proxy_url=None, impersonate=None, **kw: FakeSession(impersonate or "chrome136")
    )
    monkeypatch.setattr(scr, "bootstrap_session", lambda *a, **kw: "")
    monkeypatch.setattr(scr, "set_api_headers", lambda *a, **kw: None)
    return monkeypatch


def test_scrape_paginates_and_stops_on_empty(patched_engine, bff_page1, test_settings):
    calls = {"post": 0}

    def fake_safe_request(session, method, url, **kwargs):
        if method == "post":
            n = calls["post"]
            calls["post"] += 1
            return FakeResponse(bff_page1 if n == 0 else EMPTY_PAGE)
        raise AssertionError("no GET expected when location_id is provided")

    patched_engine.setattr(scr, "safe_request", fake_safe_request)

    params = SearchParams(keyword="data engineer", location_id=123, max_pages=2)
    result = scrape_jobs(params, settings=test_settings)

    assert result.stats.jobs_collected == 4
    assert len(result.jobs) == 4
    assert result.stats.fingerprint_used == "chrome136"
    assert calls["post"] == 2


def test_scrape_resolves_city_location(patched_engine, bff_page1, location_response, test_settings):
    calls = {"post": 0}

    def fake_safe_request(session, method, url, **kwargs):
        if method == "get":
            return FakeResponse(location_response)
        n = calls["post"]
        calls["post"] += 1
        return FakeResponse(bff_page1 if n == 0 else EMPTY_PAGE)

    patched_engine.setattr(scr, "safe_request", fake_safe_request)

    params = SearchParams(keyword="data engineer", city="New York", max_pages=1)
    result = scrape_jobs(params, settings=test_settings)

    assert result.stats.resolved_location_id == 1132348
    assert result.stats.resolved_location_name == "New York, NY"
    assert result.stats.jobs_collected == 4


def test_fingerprint_fallback_on_persistent_block(patched_engine, bff_page1, test_settings):
    def fake_safe_request(session, method, url, **kwargs):
        if session.fingerprint == "chrome136":
            raise CloudflareBlockError("blocked on primary fingerprint")
        return FakeResponse(bff_page1)

    patched_engine.setattr(scr, "safe_request", fake_safe_request)

    params = SearchParams(keyword="data engineer", location_id=123, max_pages=1)
    result = scrape_jobs(params, settings=test_settings)

    assert result.stats.fingerprint_used == "chrome142"
    assert result.stats.fingerprint_attempts == ["chrome136", "chrome142"]
    assert result.stats.jobs_collected == 4


def test_all_fingerprints_blocked_raises(patched_engine, test_settings):
    def fake_safe_request(session, method, url, **kwargs):
        raise CloudflareBlockError("always blocked")

    patched_engine.setattr(scr, "safe_request", fake_safe_request)

    params = SearchParams(keyword="x", location_id=123, max_pages=1)
    with pytest.raises(CloudflareBlockError):
        scrape_jobs(params, settings=test_settings)


def test_circuit_breaker_stops_run(patched_engine, bff_page1, test_settings):
    calls = {"post": 0}
    challenge = FakeResponse(text="Just a moment", status_code=403, content_type="text/html")

    def fake_safe_request(session, method, url, **kwargs):
        n = calls["post"]
        calls["post"] += 1
        return FakeResponse(bff_page1) if n == 0 else challenge

    patched_engine.setattr(scr, "safe_request", fake_safe_request)

    # threshold is 2 in test_settings.
    params = SearchParams(keyword="x", location_id=123, max_pages=5)
    result = scrape_jobs(params, settings=test_settings)

    assert result.stats.circuit_broken is True
    assert result.stats.jobs_collected == 4  # from the one good page
    assert result.stats.pages_fetched == 1


def test_cancel_stops_between_pages(patched_engine, bff_page1, test_settings):
    checks = {"n": 0}

    def cancel():
        checks["n"] += 1
        return checks["n"] > 1  # allow the first page, cancel before the second

    def fake_safe_request(session, method, url, **kwargs):
        return FakeResponse(bff_page1)  # always has a next cursor

    patched_engine.setattr(scr, "safe_request", fake_safe_request)

    params = SearchParams(keyword="x", location_id=123, max_pages=3)
    result = scrape_jobs(params, settings=test_settings, cancel=cancel)

    assert result.stats.cancelled is True
    assert result.stats.pages_fetched == 1
    assert result.stats.jobs_collected == 4


def test_rate_limiter_is_invoked(patched_engine, bff_page1, test_settings):
    from glassdoor_scraper.ratelimit import TokenBucket

    calls = {"post": 0, "tokens": 0}

    class CountingBucket(TokenBucket):
        def acquire(self, tokens: float = 1.0) -> None:
            calls["tokens"] += 1
            super().acquire(tokens)

    def fake_safe_request(session, method, url, **kwargs):
        n = calls["post"]
        calls["post"] += 1
        return FakeResponse(bff_page1 if n == 0 else EMPTY_PAGE)

    patched_engine.setattr(scr, "safe_request", fake_safe_request)
    bucket = CountingBucket(rate=1000.0, capacity=1000.0)

    params = SearchParams(keyword="x", location_id=123, max_pages=2)
    scrape_jobs(params, settings=test_settings, rate_limiter=bucket)
    # A token is taken for bootstrap + each page request.
    assert calls["tokens"] >= 2
