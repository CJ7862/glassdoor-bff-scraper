"""CSV/JSON export round-trip and atomic-write tests."""

from __future__ import annotations

import csv
import json
import os

from glassdoor_scraper.export import (
    export_csv,
    export_json,
    jobs_to_csv_str,
    jobs_to_json_str,
)
from glassdoor_scraper.models import Job

SAMPLE = [
    Job(
        job_id="1",
        title="Data Engineer",
        company="Acme",
        location="NYC",
        salary_min="100000",
        salary_max="150000",
        salary_currency="USD",
        pay_period="ANNUAL",
        posted_date="2026-07-01",
        easy_apply=True,
        company_rating="4.1",
        job_url="https://example.test/1",
        description_snippet="Build pipelines.",
    ),
    Job(job_id="2", title="Analyst", company="Beta", location="Remote", pay_period="HOURLY"),
]


def test_json_round_trip():
    text = jobs_to_json_str(SAMPLE)
    data = json.loads(text)
    assert len(data) == 2
    assert data[0]["pay_period"] == "ANNUAL"
    rebuilt = [Job.from_dict(d) for d in data]
    assert rebuilt == SAMPLE


def test_csv_round_trip():
    text = jobs_to_csv_str(SAMPLE)
    rows = list(csv.DictReader(text.splitlines()))
    assert len(rows) == 2
    assert rows[0]["job_id"] == "1"
    assert rows[0]["pay_period"] == "ANNUAL"
    # Booleans serialize as their str() form and the column set is stable.
    assert set(rows[0].keys()) == set(Job.field_names())


def test_export_files_are_written_atomically(tmp_path):
    json_path = str(tmp_path / "out.json")
    csv_path = str(tmp_path / "out.csv")
    export_json(SAMPLE, json_path)
    export_csv(SAMPLE, csv_path)
    assert os.path.isfile(json_path)
    assert os.path.isfile(csv_path)
    # No leftover temp files from the atomic write.
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == []
    with open(json_path, encoding="utf-8") as handle:
        assert len(json.load(handle)) == 2


def test_from_dict_ignores_unknown_and_missing_fields():
    job = Job.from_dict({"job_id": "9", "unknown": "x"})
    assert job.job_id == "9"
    assert job.pay_period == ""
