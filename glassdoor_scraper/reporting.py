"""Data-quality reporting over parsed :class:`Job` records.

The report is computed once into a structured :class:`QualityReport` and then
rendered either as plain text (byte-for-byte close to the original single-file
output) or as a rich table. Keeping the computation separate from rendering lets the
API service reuse it for schema-drift alerting (a field that suddenly becomes a
GHOST field usually means Glassdoor changed its payload shape).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import fields as dc_fields
from typing import Any

# Field is flagged GHOST when 0% populated (payload likely drifted).
FLAG_GHOST = "GHOST"
# Field is flagged SPARSE when populated but well below expectation.
FLAG_SPARSE = "SPARSE"
FLAG_OK = "OK"


@dataclass
class FieldQuality:
    """Population statistics for a single field across a record set."""

    name: str
    populated: int
    total: int
    flag: str = FLAG_OK

    @property
    def pct(self) -> float:
        return (self.populated / self.total) * 100 if self.total else 0.0


@dataclass
class QualityReport:
    """Aggregate data-quality report over a set of records."""

    total: int
    label: str
    fields: list[FieldQuality] = field(default_factory=list)

    @property
    def ghost_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.flag == FLAG_GHOST]

    @property
    def sparse_fields(self) -> list[tuple[str, float]]:
        return [(f.name, f.pct) for f in self.fields if f.flag == FLAG_SPARSE]


def _is_populated(value: Any) -> bool:
    """Return True if a field value counts as populated.

    Booleans always count (both True and False are meaningful); everything else must
    be non-None and non-blank once stringified.
    """
    if isinstance(value, bool):
        return True
    return value is not None and bool(str(value).strip())


def compute_quality_report(records: list[Any], label: str = "records") -> QualityReport:
    """Compute a :class:`QualityReport` from a list of dataclass records.

    Flag thresholds preserve the original tool's rules:
      * GHOST  -- 0% populated (needs >= 3 records to be meaningful)
      * SPARSE -- 0<pct<=30% (>=3 records), or 30<pct<70% (>=10 records)
    """
    report = QualityReport(total=len(records), label=label)
    if not records:
        return report

    total = len(records)
    field_names = [f.name for f in dc_fields(records[0])]

    for fname in field_names:
        populated = sum(1 for r in records if _is_populated(getattr(r, fname, "")))
        pct = (populated / total) * 100
        flag = FLAG_OK
        if pct == 0 and total >= 3:
            flag = FLAG_GHOST
        elif 0 < pct <= 30 and total >= 3:
            flag = FLAG_SPARSE
        elif 30 < pct < 70 and total >= 10:
            flag = FLAG_SPARSE
        report.fields.append(
            FieldQuality(name=fname, populated=populated, total=total, flag=flag)
        )

    return report


def format_report_plaintext(report: QualityReport) -> str:
    """Render a :class:`QualityReport` as the original plain-text block."""
    if report.total == 0:
        return ""

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f" DATA QUALITY REPORT ({report.total} {report.label})")
    lines.append("=" * 60)

    for fq in report.fields:
        bar_filled = round(fq.pct / 10)
        bar = "\u2588" * bar_filled + "\u2591" * (10 - bar_filled)
        suffix = ""
        if fq.flag == FLAG_GHOST:
            suffix = " <- GHOST FIELD"
        elif fq.flag == FLAG_SPARSE:
            suffix = " <- SPARSE" if fq.pct > 30 else " <- VERY SPARSE"
        lines.append(
            f" {fq.name:<35} {bar} {fq.pct:5.0f}% ({fq.populated}/{fq.total}){suffix}"
        )

    if report.ghost_fields:
        lines.append("")
        lines.append(f" Ghost fields (0% populated across {report.total} records):")
        for gf in report.ghost_fields:
            lines.append(f"   - {gf}")
    if report.sparse_fields:
        lines.append("")
        lines.append(" Sparse fields (these may just be user-optional):")
        for sf, sp in report.sparse_fields:
            lines.append(f"   - {sf}: {sp:.0f}%")

    lines.append("=" * 60)
    return "\n".join(lines)
