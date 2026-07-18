"""Parser tests against recorded BFF fixtures."""

from __future__ import annotations

from datetime import datetime

from glassdoor_scraper.models import (
    PAY_PERIOD_ANNUAL,
    PAY_PERIOD_HOURLY,
    infer_pay_period_from_amount,
    normalize_pay_period,
)
from glassdoor_scraper.parser import (
    extract_listings,
    parse_jobs,
    parse_location_response,
    select_next_cursor,
)

REFERENCE = datetime(2026, 7, 14)


def test_parse_jobs_basic_fields(bff_page1):
    jobs = parse_jobs(bff_page1, reference_date=REFERENCE)
    # Five listings, but one is a duplicate id -> four unique jobs.
    assert len(jobs) == 4
    first = jobs[0]
    assert first.job_id == "1010179702382"
    assert first.title == "Data Engineer"
    assert first.company == "DCI Solutions"
    assert first.location == "New York, NY"
    assert first.salary_min == "175000"
    assert first.salary_max == "260000"
    assert first.salary_currency == "USD"
    assert first.company_rating == "3.9"
    assert first.easy_apply is True
    assert first.posted_date == "2026-07-09"  # 5 days before the reference date
    assert "Computer Science" in first.description_snippet


def test_parse_jobs_deduplicates_within_response(bff_page1):
    jobs = parse_jobs(bff_page1, reference_date=REFERENCE)
    ids = [j.job_id for j in jobs]
    assert len(ids) == len(set(ids))


def test_pay_period_explicit_annual_vs_hourly(bff_page1):
    jobs = {j.job_id: j for j in parse_jobs(bff_page1, reference_date=REFERENCE)}
    assert jobs["1010179702382"].pay_period == PAY_PERIOD_ANNUAL
    assert jobs["1010191916488"].pay_period == PAY_PERIOD_HOURLY


def test_pay_period_inferred_from_magnitude(bff_page1):
    jobs = {j.job_id: j for j in parse_jobs(bff_page1, reference_date=REFERENCE)}
    # These two listings have no explicit payPeriod, so it is inferred.
    assert jobs["2000000000001"].pay_period == PAY_PERIOD_ANNUAL  # 150000
    assert jobs["2000000000002"].pay_period == PAY_PERIOD_HOURLY  # 75


def test_zero_rating_is_blank(bff_variant):
    jobs = {j.job_id: j for j in parse_jobs(bff_variant, reference_date=REFERENCE)}
    # overallRating of 0 should not be reported as a rating.
    assert jobs["3000000000001"].company_rating == ""
    assert jobs["3000000000002"].company_rating == "4.5"


def test_parse_jobs_tolerates_variant_shape(bff_variant):
    jobs = parse_jobs(bff_variant, reference_date=REFERENCE)
    assert len(jobs) == 2
    assert jobs[0].pay_period == PAY_PERIOD_HOURLY
    assert jobs[1].pay_period == PAY_PERIOD_ANNUAL


def test_extract_listings_empty_on_garbage():
    assert extract_listings({"nonsense": True}) == []
    assert extract_listings(None) == []
    assert extract_listings([]) == []


def test_select_next_cursor_matches_next_page(bff_page1):
    # Current page 0 (1-based page 1) -> next page is 2 -> cursor_page_2.
    assert select_next_cursor(bff_page1, 0) == "cursor_page_2"
    # Current page 1 -> next page is 3 -> cursor_page_3.
    assert select_next_cursor(bff_page1, 1) == "cursor_page_3"


def test_select_next_cursor_prefers_pagenumber_over_last():
    data = {
        "data": {
            "jobListings": {
                "paginationCursors": [
                    {"pageNumber": 2, "cursor": "correct"},
                    {"pageNumber": 7, "cursor": "decoy_last"},
                ]
            }
        }
    }
    # page_num 0 -> target page 2 -> must pick "correct", not the last entry.
    assert select_next_cursor(data, 0) == "correct"


def test_select_next_cursor_falls_back_to_last_when_no_match():
    data = {"data": {"jobListings": {"paginationCursors": [{"pageNumber": 9, "cursor": "only"}]}}}
    assert select_next_cursor(data, 0) == "only"


def test_select_next_cursor_empty_when_absent():
    assert select_next_cursor({"data": {"jobListings": {}}}, 0) == ""


def test_parse_location_response(location_response):
    loc_id, loc_type, loc_name = parse_location_response(location_response, "New York")
    assert loc_id == 1132348
    assert loc_type == "CITY"
    # Parenthetical country suffix is stripped.
    assert loc_name == "New York, NY"


def test_parse_location_response_raises_on_empty():
    import pytest

    with pytest.raises(ValueError):
        parse_location_response([], "Nowhere")


def test_normalize_pay_period_tokens():
    assert normalize_pay_period("ANNUAL") == PAY_PERIOD_ANNUAL
    assert normalize_pay_period("hourly") == PAY_PERIOD_HOURLY
    assert normalize_pay_period("PERIOD_ANNUAL") == PAY_PERIOD_ANNUAL
    assert normalize_pay_period(None) == "UNKNOWN"
    assert normalize_pay_period("") == "UNKNOWN"


def test_infer_pay_period_from_amount():
    assert infer_pay_period_from_amount(75) == PAY_PERIOD_HOURLY
    assert infer_pay_period_from_amount(150000) == PAY_PERIOD_ANNUAL
    assert infer_pay_period_from_amount(0) == "UNKNOWN"
    assert infer_pay_period_from_amount(None) == "UNKNOWN"
