"""Checkpoint/resume run-state tests."""

from __future__ import annotations

from glassdoor_scraper.runstate import RunState, row_key


def test_row_key_is_stable_and_case_insensitive():
    a = row_key("Data Engineer", "New York", "com", "us", 2)
    b = row_key("data engineer", "new york", "com", "us", 2)
    assert a == b
    assert a != row_key("data engineer", "new york", "com", "us", 3)


def test_mark_done_persists_and_reloads(tmp_path):
    path = str(tmp_path / "state.json")
    state = RunState.load(path)
    key = row_key("k", "c", "com", "us", 2)
    assert state.is_done(key) is False
    state.mark_done(key, jobs=12, meta={"pages_fetched": 2})

    reloaded = RunState.load(path)
    assert reloaded.is_done(key) is True
    assert reloaded.rows[key]["jobs"] == 12
    assert reloaded.rows[key]["pages_fetched"] == 2


def test_mark_failed_is_not_done(tmp_path):
    path = str(tmp_path / "state.json")
    state = RunState.load(path)
    key = row_key("k", "c", "com", "us", 2)
    state.mark_failed(key, "blocked")
    assert state.is_done(key) is False
    assert RunState.load(path).rows[key]["status"] == "failed"


def test_load_missing_file_is_empty(tmp_path):
    state = RunState.load(str(tmp_path / "nope.json"))
    assert state.rows == {}
