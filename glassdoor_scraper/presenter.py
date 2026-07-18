"""Human-facing output for the CLI.

Readability is the hard requirement here. Every value is shown with a full
plain-language label ("Pages fetched: 3/3", "Blocked by Cloudflare: 0"), never a
cryptic abbreviation, and color is only ever used to *reinforce* text that already
stands on its own (green = ok, yellow = warning, red = error) -- so the output is
identical in meaning with color stripped.

When stdout is a real terminal the presenter uses ``rich`` progress bars and tables.
When output is piped or redirected (not a TTY), or ``--no-color`` selects plain
mode, it falls back to plain aligned text with no animations or box art, so logs and
scripts stay parseable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.table import Table

from .reporting import FLAG_GHOST, FLAG_OK, FLAG_SPARSE, QualityReport, format_report_plaintext
from .scraper import ProgressEvent

# A progress callback consumes ProgressEvents emitted by ``scrape_jobs``.
ProgressSink = Callable[[ProgressEvent], None]

_FLAG_STYLE = {FLAG_OK: "green", FLAG_SPARSE: "yellow", FLAG_GHOST: "red"}
_FLAG_LABEL = {FLAG_OK: "OK", FLAG_SPARSE: "Sparse", FLAG_GHOST: "Ghost field"}


class Presenter:
    """Renders progress, summaries, and the data-quality report."""

    def __init__(self, no_color: bool = False) -> None:
        # ``force_terminal=False`` lets rich auto-detect a real TTY; when output is
        # piped, ``is_terminal`` becomes False and we switch to plain rendering.
        self.console = Console(no_color=no_color)
        self.color = not no_color
        self.interactive = self.console.is_terminal and not no_color

    # -- generic messages ---------------------------------------------------
    def info(self, message: str) -> None:
        if self.interactive:
            self.console.print(message)
        else:
            print(message)

    def warn(self, message: str) -> None:
        text = f"Warning: {message}"
        if self.interactive:
            self.console.print(text, style="yellow")
        else:
            print(text)

    def error(self, message: str) -> None:
        text = f"Error: {message}"
        if self.interactive:
            self.console.print(text, style="red")
        else:
            print(text)

    def success(self, message: str) -> None:
        if self.interactive:
            self.console.print(message, style="green")
        else:
            print(message)

    # -- per-search progress ------------------------------------------------
    @contextmanager
    def search_progress(self, title: str, total_pages: int) -> Iterator[ProgressSink]:
        """Yield a progress sink to pass as ``scrape_jobs(progress=...)``.

        In interactive mode this drives a live progress bar; otherwise it is a no-op
        because the INFO log stream already reports page-by-page progress, and we do
        not want to print it twice.
        """
        if not self.interactive:
            def noop(_: ProgressEvent) -> None:
                return None

            yield noop
            return

        # Imported lazily so plain/non-TTY runs never pay the rich progress import.
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TextColumn("Pages fetched: {task.completed}/{task.total}"),
            TextColumn("| Jobs collected: {task.fields[jobs]}"),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        )
        task = progress.add_task(title, total=max(total_pages, 1), jobs=0)

        def sink(event: ProgressEvent) -> None:
            if event.phase == "bootstrap":
                progress.update(task, description=f"{title}: establishing session")
            elif event.phase == "location":
                progress.update(task, description=f"{title}: resolving location")
            elif event.phase == "page":
                progress.update(
                    task,
                    description=f"{title}: fetching results",
                    completed=event.page,
                    jobs=event.jobs_collected,
                )
            elif event.phase == "done":
                progress.update(
                    task,
                    description=f"{title}: complete",
                    completed=max(event.page, total_pages),
                    jobs=event.jobs_collected,
                )

        with progress:
            yield sink

    # -- results summary table ---------------------------------------------
    def summary_table(self, rows: list[dict]) -> None:
        """Render the per-search results summary.

        Each row dict has: keyword, location, pages_fetched, pages_requested, jobs,
        blocks, retries, status.
        """
        if not rows:
            return

        if self.interactive:
            table = Table(title="Search results summary", header_style="bold")
            table.add_column("Keyword")
            table.add_column("Location")
            table.add_column("Pages fetched", justify="right")
            table.add_column("Jobs collected", justify="right")
            table.add_column("Blocked by Cloudflare", justify="right")
            table.add_column("Retries", justify="right")
            table.add_column("Status")
            for row in rows:
                status = row["status"]
                style = {
                    "OK": "green",
                    "Partial": "yellow",
                    "Failed": "red",
                    "Skipped (resumed)": "green",
                }.get(status, "")
                table.add_row(
                    row["keyword"],
                    row["location"],
                    f"{row['pages_fetched']}/{row['pages_requested']}",
                    str(row["jobs"]),
                    str(row["blocks"]),
                    str(row["retries"]),
                    f"[{style}]{status}[/{style}]" if style else status,
                )
            self.console.print(table)
            return

        # Plain text fallback.
        print("\nSearch results summary")
        print("-" * 100)
        header = (
            f"{'Keyword':<24} {'Location':<22} {'Pages fetched':>13} "
            f"{'Jobs collected':>14} {'Blocked':>8} {'Retries':>8}  Status"
        )
        print(header)
        print("-" * 100)
        for row in rows:
            print(
                f"{row['keyword'][:24]:<24} {row['location'][:22]:<22} "
                f"{str(row['pages_fetched']) + '/' + str(row['pages_requested']):>13} "
                f"{row['jobs']:>14} {row['blocks']:>8} {row['retries']:>8}  {row['status']}"
            )
        print("-" * 100)

    # -- data-quality report ------------------------------------------------
    def quality_report(self, report: QualityReport) -> None:
        """Render the data-quality report (rich table or the original plain block)."""
        if report.total == 0:
            return

        if not self.interactive:
            print("\n" + format_report_plaintext(report))
            return

        table = Table(
            title=f"Data quality report ({report.total} {report.label})",
            header_style="bold",
        )
        table.add_column("Field")
        table.add_column("Populated")
        table.add_column("Percent", justify="right")
        table.add_column("Count", justify="right")
        table.add_column("Flag")

        for fq in report.fields:
            bar_filled = round(fq.pct / 10)
            bar = "\u2588" * bar_filled + "\u2591" * (10 - bar_filled)
            style = _FLAG_STYLE.get(fq.flag, "")
            label = _FLAG_LABEL.get(fq.flag, fq.flag)
            table.add_row(
                fq.name,
                bar,
                f"{fq.pct:.0f}%",
                f"{fq.populated}/{fq.total}",
                f"[{style}]{label}[/{style}]" if style else label,
            )

        self.console.print(table)

        if report.ghost_fields:
            self.console.print(
                "Ghost fields (0% populated - the payload shape may have changed):",
                style="red",
            )
            for gf in report.ghost_fields:
                self.console.print(f"  - {gf}")
        if report.sparse_fields:
            self.console.print(
                "Sparse fields (these may just be user-optional):", style="yellow"
            )
            for name, pct in report.sparse_fields:
                self.console.print(f"  - {name}: {pct:.0f}%")
