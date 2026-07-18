"""Pydantic request/response models for the API.

These are the schema boundary: every inbound field is validated here (types,
enums, ranges, the location-XOR rule, and the hard per-request pages cap sourced
from settings) so malformed or abusive input is rejected before it can reach the
queue or the scraper.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from glassdoor_scraper.config import SITES, get_settings
from glassdoor_scraper.parser import POSTED_MAP, RATING_MAP, SORT_MAP, WORK_TYPE_MAP
from glassdoor_scraper.scraper import SearchParams


class SearchRequest(BaseModel):
    """A job-search submission."""

    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=200)
    location_id: int | None = Field(default=None, ge=1)
    location_name: str = Field(default="", max_length=200)
    site: str = Field(default="com")
    country: str = Field(default="us", min_length=2, max_length=2)
    pages: int = Field(default=2, ge=1)
    sort: str = Field(default="relevant")
    work_type: str | None = Field(default=None)
    easy_apply: bool = False
    rating: str = Field(default="any")
    min_salary: int | None = Field(default=None, ge=0)
    max_salary: int | None = Field(default=None, ge=0)
    posted: str = Field(default="any")
    webhook_url: str | None = Field(default=None, max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("site")
    @classmethod
    def _valid_site(cls, value: str) -> str:
        if value not in SITES:
            raise ValueError(f"site must be one of: {', '.join(SITES)}")
        return value

    @field_validator("sort")
    @classmethod
    def _valid_sort(cls, value: str) -> str:
        if value not in SORT_MAP:
            raise ValueError(f"sort must be one of: {', '.join(SORT_MAP)}")
        return value

    @field_validator("rating")
    @classmethod
    def _valid_rating(cls, value: str) -> str:
        if value not in RATING_MAP:
            raise ValueError(f"rating must be one of: {', '.join(RATING_MAP)}")
        return value

    @field_validator("posted")
    @classmethod
    def _valid_posted(cls, value: str) -> str:
        if value not in POSTED_MAP:
            raise ValueError(f"posted must be one of: {', '.join(POSTED_MAP)}")
        return value

    @field_validator("work_type")
    @classmethod
    def _valid_work_type(cls, value: str | None) -> str | None:
        if value is not None and value not in WORK_TYPE_MAP:
            raise ValueError(f"work_type must be one of: {', '.join(WORK_TYPE_MAP)}")
        return value

    @field_validator("webhook_url")
    @classmethod
    def _valid_webhook(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("webhook_url must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _validate_location_and_pages(self) -> SearchRequest:
        if not self.city and not self.location_id:
            raise ValueError("Provide either 'city' or 'location_id'.")
        cap = get_settings().max_pages_per_request
        if self.pages > cap:
            raise ValueError(f"pages must not exceed the configured maximum of {cap}.")
        if (
            self.min_salary is not None
            and self.max_salary is not None
            and self.min_salary > self.max_salary
        ):
            raise ValueError("min_salary must not exceed max_salary.")
        return self

    def to_search_params(self) -> SearchParams:
        """Translate the validated request into engine :class:`SearchParams`."""
        return SearchParams(
            keyword=self.keyword,
            city=self.city or "",
            location_id=self.location_id or 0,
            location_name=self.location_name,
            site=self.site,
            max_pages=self.pages,
            sort=SORT_MAP[self.sort],
            country=self.country,
            min_rating=RATING_MAP[self.rating],
            min_salary=self.min_salary,
            max_salary=self.max_salary,
            posted_days=POSTED_MAP[self.posted],
            easy_apply_only=self.easy_apply,
            work_type=WORK_TYPE_MAP.get(self.work_type) if self.work_type else None,
        )


class SubmitResponse(BaseModel):
    job_id: str
    status: str
    idempotent: bool = False
    created_at: str | None = None


class JobProgress(BaseModel):
    pages_requested: int
    pages_done: int
    jobs_collected: int


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: JobProgress
    error: str | None = None
    attempts: int = 0
    max_attempts: int = 0
    webhook_status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    results_available: bool = False
    results_expire_at: str | None = None


class ResultsResponse(BaseModel):
    job_id: str
    total: int
    page: int
    page_size: int
    results: list[dict[str, Any]]


class ApiKeyInfo(BaseModel):
    id: str
    name: str
    daily_quota: int
    rate_limit_per_min: int
    max_concurrent_jobs: int
    active: bool
    created_at: str
