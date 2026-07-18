"""Data-quality report tests (ghost/sparse flag logic and plain-text rendering)."""

from __future__ import annotations

from glassdoor_scraper.models import Job
from glassdoor_scraper.reporting import (
    FLAG_GHOST,
    FLAG_OK,
    FLAG_SPARSE,
    compute_quality_report,
    format_report_plaintext,
)


def _jobs(n: int, **overrides) -> list[Job]:
    return [Job(job_id=str(i), title="t", company="c", **overrides) for i in range(n)]


def test_ghost_field_flagged():
    # description_snippet is never populated across >= 3 records -> ghost.
    report = compute_quality_report(_jobs(5), "jobs")
    by_name = {f.name: f for f in report.fields}
    assert by_name["description_snippet"].flag == FLAG_GHOST
    assert "description_snippet" in report.ghost_fields


def test_fully_populated_field_ok():
    report = compute_quality_report(_jobs(5), "jobs")
    by_name = {f.name: f for f in report.fields}
    assert by_name["job_id"].flag == FLAG_OK
    assert by_name["title"].flag == FLAG_OK


def test_sparse_field_flagged():
    jobs = _jobs(10)
    # Populate salary on only 2 of 10 -> 20% -> sparse.
    for j in jobs[:2]:
        j.salary_min = "100000"
    report = compute_quality_report(jobs, "jobs")
    by_name = {f.name: f for f in report.fields}
    assert by_name["salary_min"].flag == FLAG_SPARSE


def test_boolean_false_counts_as_populated():
    # easy_apply defaults to False, which is a meaningful value, so it is populated.
    report = compute_quality_report(_jobs(4), "jobs")
    by_name = {f.name: f for f in report.fields}
    assert by_name["easy_apply"].populated == 4


def test_empty_report_is_safe():
    report = compute_quality_report([], "jobs")
    assert report.total == 0
    assert format_report_plaintext(report) == ""


def test_plaintext_contains_headers_and_flags():
    text = format_report_plaintext(compute_quality_report(_jobs(5), "jobs"))
    assert "DATA QUALITY REPORT (5 jobs)" in text
    assert "GHOST FIELD" in text
