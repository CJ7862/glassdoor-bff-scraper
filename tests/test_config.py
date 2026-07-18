"""Settings/config behavior tests."""

from __future__ import annotations

from glassdoor_scraper.config import Settings


def test_default_fingerprint_chain():
    s = Settings(impersonate="chrome136", impersonate_fallbacks=["chrome142", "chrome136"])
    # Primary is tried first and duplicates are removed while preserving order.
    assert s.ordered_fingerprints() == ["chrome136", "chrome142"]


def test_comma_separated_fallbacks_from_env(monkeypatch):
    monkeypatch.setenv("GLASSDOOR_IMPERSONATE_FALLBACKS", "chrome142, chrome145 ,chrome124")
    s = Settings()
    assert s.impersonate_fallbacks == ["chrome142", "chrome145", "chrome124"]


def test_legacy_env_aliases(monkeypatch):
    monkeypatch.setenv("DATAIMPULSE_USER", "legacy_user")
    monkeypatch.setenv("DATAIMPULSE_PASS", "legacy_pass")
    monkeypatch.setenv("GLASSDOOR_IMPERSONATE", "chrome145")
    s = Settings()
    assert s.proxy_user == "legacy_user"
    assert s.proxy_pass == "legacy_pass"
    assert s.impersonate == "chrome145"


def test_delay_pair_is_normalized():
    s = Settings(delay_min=5.0, delay_max=3.0)
    assert s.delay == (3.0, 5.0)
