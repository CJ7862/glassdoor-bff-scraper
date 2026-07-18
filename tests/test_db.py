"""Repository tests: queue claiming, TTL results, seen_jobs dedup, keys, usage."""

from __future__ import annotations

from datetime import timedelta

import pytest

from api.db import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Database,
    utcnow,
)


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _make_job(db: Database, job_id: str, max_attempts: int = 3) -> None:
    db.create_job(
        job_id=job_id,
        api_key_id="key1",
        request_body={"keyword": "x", "location_id": 1, "pages": 1},
        body_hash="hash-" + job_id,
        idempotency_key=None,
        webhook_url=None,
        max_attempts=max_attempts,
        pages_requested=1,
    )


def test_create_and_get_job(db):
    _make_job(db, "j1")
    job = db.get_job("j1")
    assert job is not None
    assert job["status"] == STATUS_QUEUED
    assert job["attempts"] == 0


def test_claim_next_job_is_atomic_and_ordered(db):
    _make_job(db, "j1")
    _make_job(db, "j2")
    first = db.claim_next_job()
    second = db.claim_next_job()
    third = db.claim_next_job()
    assert first["job_id"] == "j1"
    assert first["status"] == STATUS_RUNNING
    assert first["attempts"] == 1
    assert second["job_id"] == "j2"
    assert third is None  # queue drained


def test_requeue_and_dead_letter(db):
    _make_job(db, "j1", max_attempts=2)
    db.claim_next_job()
    db.requeue("j1", error="temporary")
    assert db.get_job("j1")["status"] == STATUS_QUEUED
    db.claim_next_job()
    db.mark_failed("j1", error="permanent")
    job = db.get_job("j1")
    assert job["status"] == STATUS_FAILED
    assert job["error"] == "permanent"


def test_mark_done_and_active_count(db):
    _make_job(db, "j1")
    _make_job(db, "j2")
    db.claim_next_job()
    assert db.count_active_for_key("key1") == 2  # one running, one queued
    db.mark_done("j1", jobs_collected=10, pages_done=1)
    assert db.count_active_for_key("key1") == 1
    assert db.get_job("j1")["status"] == STATUS_DONE


def test_reset_running_jobs(db):
    _make_job(db, "j1")
    db.claim_next_job()
    reset = db.reset_running_jobs()
    assert reset == ["j1"]
    assert db.get_job("j1")["status"] == STATUS_QUEUED


def test_results_ttl(db):
    _make_job(db, "j1")
    future = (utcnow() + timedelta(hours=1)).isoformat()
    db.save_results("j1", records=[{"job_id": "a"}], stats={}, quality={}, expires_at_iso=future)
    assert db.get_results("j1") == [{"job_id": "a"}]
    assert db.get_results_meta("j1")["record_count"] == 1

    past = (utcnow() - timedelta(hours=1)).isoformat()
    db.save_results("j1", records=[{"job_id": "a"}], stats={}, quality={}, expires_at_iso=past)
    assert db.get_results("j1") is None  # expired
    assert db.get_results_meta("j1") is None


def test_purge_expired_results(db):
    _make_job(db, "j1")
    past = (utcnow() - timedelta(hours=1)).isoformat()
    db.save_results("j1", records=[{"x": 1}], stats={}, quality={}, expires_at_iso=past)
    removed = db.purge_expired_results()
    assert removed == 1


def test_seen_jobs_dedup(db):
    first = db.record_seen(["100", "101"], keyword="k", location="NYC", site="com")
    assert first == {"new": 2, "repeat": 0}
    second = db.record_seen(["101", "102"], keyword="k", location="NYC", site="com")
    assert second == {"new": 1, "repeat": 1}
    row = db.get_seen("101")
    assert row["times_seen"] == 2
    assert db.count_seen() == 3


def test_idempotency_lookup(db):
    db.create_job(
        job_id="j1",
        api_key_id="key1",
        request_body={"keyword": "x"},
        body_hash="bh",
        idempotency_key="idem-1",
        webhook_url=None,
        max_attempts=3,
        pages_requested=1,
    )
    found = db.find_by_idempotency_key("key1", "idem-1")
    assert found["job_id"] == "j1"
    assert db.find_by_idempotency_key("key1", "missing") is None
    recent = db.find_recent_by_body_hash("key1", "bh", "2000-01-01T00:00:00+00:00")
    assert recent["job_id"] == "j1"


def test_api_keys_and_usage(db):
    db.create_api_key(
        key_id="k1",
        key_hash="hash1",
        name="acme",
        daily_quota=100,
        rate_limit_per_min=60,
        max_concurrent_jobs=5,
    )
    assert db.get_api_key_by_hash("hash1")["name"] == "acme"
    assert len(db.list_api_keys()) == 1

    day = "2026-07-14"
    assert db.increment_usage("k1", day) == 1
    assert db.increment_usage("k1", day) == 2
    assert db.get_usage("k1", day) == 2

    assert db.revoke_api_key("k1") is True
    assert db.get_api_key_by_hash("hash1") is None  # inactive keys are not returned
