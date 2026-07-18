"""Tests for proxy URL building, challenge detection, and safe_request retries."""

from __future__ import annotations

import pytest

from glassdoor_scraper.config import Settings
from glassdoor_scraper.exceptions import CloudflareBlockError
from glassdoor_scraper.session import (
    build_proxy_url,
    is_challenge_page,
    safe_request,
    validate_json_response,
)

from .conftest import FakeResponse, FakeSession

SETTINGS = Settings(proxy_host="gw.dataimpulse.com", proxy_rotating_port=823, proxy_sticky_port=10000)


def test_build_proxy_url_none_without_credentials():
    assert build_proxy_url("", "", settings=SETTINGS) is None
    assert build_proxy_url("user", "", settings=SETTINGS) is None


def test_build_proxy_url_rotating():
    url = build_proxy_url("user", "pass", sticky=False, country="us", settings=SETTINGS)
    assert url == "http://user__cr.us:pass@gw.dataimpulse.com:823"


def test_build_proxy_url_sticky_uses_sticky_port_and_sessid():
    url = build_proxy_url(
        "user", "pass", sticky=True, country="de", session_id="fixed1", settings=SETTINGS
    )
    assert url == "http://user__cr.de;sessid.fixed1:pass@gw.dataimpulse.com:10000"


def test_build_proxy_url_sticky_generates_unique_sessid():
    a = build_proxy_url("user", "pass", sticky=True, settings=SETTINGS)
    b = build_proxy_url("user", "pass", sticky=True, settings=SETTINGS)
    assert a is not None and b is not None
    # Two bootstraps must not collide on the same sessid label.
    assert a != b
    assert "sessid." in a


def test_is_challenge_page_by_marker():
    resp = FakeResponse(text="Just a moment...", status_code=403, content_type="text/html")
    assert is_challenge_page(resp) is True


def test_is_challenge_page_by_cf_header():
    resp = FakeResponse(text="", status_code=403, content_type="text/html", headers={"cf-mitigated": "challenge"})
    assert is_challenge_page(resp) is True


def test_is_challenge_page_short_403():
    resp = FakeResponse(text="no", status_code=403, content_type="text/html")
    assert is_challenge_page(resp) is True


def test_is_challenge_page_clean_response():
    resp = FakeResponse({"ok": True})
    assert is_challenge_page(resp) is False


def test_validate_json_response_ok():
    resp = FakeResponse({"data": 1})
    assert validate_json_response(resp) == {"data": 1}


def test_validate_json_response_challenge_returns_none():
    resp = FakeResponse(text="Just a moment", status_code=403, content_type="text/html")
    assert validate_json_response(resp, context="unit") is None


def test_safe_request_returns_on_success():
    session = FakeSession()
    good = FakeResponse({"hello": "world"})
    session.get = lambda url, **kw: good  # type: ignore[method-assign]
    resp = safe_request(session, "get", "https://example.test", settings=SETTINGS)
    assert resp is good


def test_safe_request_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("glassdoor_scraper.session.time.sleep", lambda *_: None)
    session = FakeSession()
    calls = {"n": 0}
    block = FakeResponse(text="Just a moment", status_code=403, content_type="text/html")
    good = FakeResponse({"ok": 1})

    def flaky_post(url, **kw):
        calls["n"] += 1
        return block if calls["n"] == 1 else good

    session.post = flaky_post  # type: ignore[method-assign]
    cfg = Settings(max_retries=3, backoff_base=0.0)
    outcomes = []
    resp = safe_request(session, "post", "https://x.test", observer=outcomes.append, settings=cfg)
    assert resp is good
    assert calls["n"] == 2
    # The observer records exactly one terminal success with attempts=2.
    assert outcomes[-1].success and outcomes[-1].attempts == 2


def test_safe_request_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr("glassdoor_scraper.session.time.sleep", lambda *_: None)
    session = FakeSession()
    block = FakeResponse(text="Sorry, you have been blocked", status_code=403, content_type="text/html")
    session.get = lambda url, **kw: block  # type: ignore[method-assign]
    cfg = Settings(max_retries=2, backoff_base=0.0)
    outcomes = []
    with pytest.raises(CloudflareBlockError):
        safe_request(session, "get", "https://x.test", observer=outcomes.append, settings=cfg)
    assert outcomes[-1].blocked is True
