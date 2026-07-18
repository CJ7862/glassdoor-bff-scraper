"""Core data models for scraped jobs.

The ``Job`` dataclass is deliberately connector-agnostic: it holds the normalized
shape a downstream consumer cares about, not anything Glassdoor-specific. A future
Monster/HiringCafe connector would populate the same model, so nothing here should
reference Glassdoor payload internals (that lives in ``parser.py``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

# Recognized pay-period values. ``UNKNOWN`` is used when the payload does not make
# the period explicit; ``normalize_pay_period`` maps raw BFF strings onto these.
PAY_PERIOD_ANNUAL = "ANNUAL"
PAY_PERIOD_HOURLY = "HOURLY"
PAY_PERIOD_MONTHLY = "MONTHLY"
PAY_PERIOD_DAILY = "DAILY"
PAY_PERIOD_WEEKLY = "WEEKLY"
PAY_PERIOD_UNKNOWN = "UNKNOWN"


@dataclass
class Job:
    """A single normalized job listing.

    ``pay_period`` distinguishes hourly figures (e.g. 40-60) from annual ones
    (e.g. 110000) so a consumer never has to guess from the magnitude of the number.
    """

    job_id: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    salary_min: str = ""
    salary_max: str = ""
    salary_currency: str = ""
    pay_period: str = ""
    posted_date: str = ""
    easy_apply: bool = False
    company_rating: str = ""
    job_url: str = ""
    description_snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict (used for JSON/CSV export and API responses)."""
        return asdict(self)

    @classmethod
    def field_names(cls) -> list[str]:
        """Return the ordered field names (stable CSV column order)."""
        return [f.name for f in fields(cls)]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        """Build a ``Job`` from a dict, ignoring unknown keys.

        Tolerates payloads that predate the ``pay_period`` field so old exports and
        stored results still round-trip cleanly.
        """
        known = set(cls.field_names())
        return cls(**{k: v for k, v in data.items() if k in known})


# Raw BFF pay-period tokens -> normalized constant. Glassdoor has used several
# spellings across payload versions; we tolerate all of them.
_PAY_PERIOD_TOKENS: dict[str, str] = {
    "ANNUAL": PAY_PERIOD_ANNUAL,
    "ANNUALLY": PAY_PERIOD_ANNUAL,
    "YEARLY": PAY_PERIOD_ANNUAL,
    "YEAR": PAY_PERIOD_ANNUAL,
    "HOURLY": PAY_PERIOD_HOURLY,
    "HOUR": PAY_PERIOD_HOURLY,
    "MONTHLY": PAY_PERIOD_MONTHLY,
    "MONTH": PAY_PERIOD_MONTHLY,
    "DAILY": PAY_PERIOD_DAILY,
    "DAY": PAY_PERIOD_DAILY,
    "WEEKLY": PAY_PERIOD_WEEKLY,
    "WEEK": PAY_PERIOD_WEEKLY,
}


def normalize_pay_period(raw: object) -> str:
    """Map a raw BFF pay-period token onto a normalized constant.

    Returns ``PAY_PERIOD_UNKNOWN`` for missing/unrecognized values so the caller
    can still fall back to an amount-magnitude heuristic if it wants to.
    """
    if not isinstance(raw, str):
        return PAY_PERIOD_UNKNOWN
    token = raw.strip().upper()
    if not token:
        return PAY_PERIOD_UNKNOWN
    # Strip a common prefix Glassdoor sometimes uses, e.g. "PERIOD_ANNUAL".
    token = token.removeprefix("PERIOD_").removeprefix("PAY_PERIOD_")
    return _PAY_PERIOD_TOKENS.get(token, PAY_PERIOD_UNKNOWN)


def infer_pay_period_from_amount(amount: float | int | None) -> str:
    """Best-effort pay-period guess from the magnitude of a salary figure.

    Only used when the payload omits an explicit period. Glassdoor hourly rates are
    small (tens), annual figures are large (tens of thousands), so a threshold of
    2000 cleanly separates them for every real-world currency this scraper targets.
    """
    if amount is None:
        return PAY_PERIOD_UNKNOWN
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return PAY_PERIOD_UNKNOWN
    if value <= 0:
        return PAY_PERIOD_UNKNOWN
    if value < 2000:
        return PAY_PERIOD_HOURLY
    return PAY_PERIOD_ANNUAL
