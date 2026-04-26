from __future__ import annotations

from dataclasses import dataclass
import shutil
import sys
import time
from typing import TextIO


@dataclass(slots=True)
class AssignmentCleanupSummary:
    index: int
    total: int
    assignment_id: int
    assignment_name: str
    unsubmitted: int
    updated_to_missing: int
    updated_to_none: int
    unchanged: int
    skipped_excused: int
    skipped_submitted: int
    skipped_existing_grade: int
    skipped_no_due_date: int
    errors: int


class CleanupMissingReporter:
    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        live: bool | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.live = self.stream.isatty() if live is None else live
        self.total_assignments = 0
        self.total_students = 0
        self.student_limit = 0
        self.rows: list[AssignmentCleanupSummary] = []
        self.started_at = time.monotonic()
        self.current_assignment: AssignmentCleanupSummary | None = None
        self._cursor_hidden = False

    def start(
        self,
        *,
        total_assignments: int,
        total_students: int,
        student_limit: int,
    ) -> None:
        self.total_assignments = total_assignments
        self.total_students = total_students
        self.student_limit = student_limit
        self.started_at = time.monotonic()
        if self.live:
            self._write("\033[?25l")
            self._cursor_hidden = True
            self._render()

    def set_current_assignment(
        self,
        *,
        index: int,
        total: int,
        assignment_id: int,
        assignment_name: str,
    ) -> None:
        self.current_assignment = AssignmentCleanupSummary(
            index=index,
            total=total,
            assignment_id=assignment_id,
            assignment_name=assignment_name,
            unsubmitted=0,
            updated_to_missing=0,
            updated_to_none=0,
            unchanged=0,
            skipped_excused=0,
            skipped_submitted=0,
            skipped_existing_grade=0,
            skipped_no_due_date=0,
            errors=0,
        )
        if self.live:
            self._render()

    def add_assignment_summary(self, summary: AssignmentCleanupSummary) -> None:
        self.rows.append(summary)
        self.current_assignment = summary
        if self.live:
            self._render()

    def finish(self, aggregate_stats: dict[str, int]) -> None:
        self._render(aggregate_stats=aggregate_stats, final=True)
        if self._cursor_hidden:
            self._write("\033[?25h")
            self._cursor_hidden = False

    def close(self) -> None:
        if self._cursor_hidden:
            self._write("\033[?25h")
            self._cursor_hidden = False

    def _write(self, text: str) -> None:
        self.stream.write(text)
        self.stream.flush()

    def _render(
        self,
        *,
        aggregate_stats: dict[str, int] | None = None,
        final: bool = False,
    ) -> None:
        if not self.live and not final:
            return
        if self.live:
            self._write("\033[2J\033[H")
        lines = self._build_lines(aggregate_stats=aggregate_stats, final=final)
        self._write("\n".join(lines) + "\n")

    def _build_lines(
        self,
        *,
        aggregate_stats: dict[str, int] | None = None,
        final: bool = False,
    ) -> list[str]:
        width = max(100, shutil.get_terminal_size(fallback=(100, 40)).columns)
        lines: list[str] = [
            "cleanup-missing",
            self._progress_line(width),
            self._status_line(),
            self._legend_line(width),
            "",
        ]
        lines.extend(self._table_lines(width, aggregate_stats=aggregate_stats))
        elapsed = time.monotonic() - self.started_at
        lines.append("")
        lines.append(f"Elapsed: {elapsed:.1f}s")
        return lines

    def _progress_line(self, width: int) -> str:
        completed = len(self.rows)
        total = self.total_assignments
        if total <= 0:
            return "Progress: [------------------------------] 0/0 (0.0%, ETA --)"
        bar_width = 30
        progress = completed / total
        filled = min(bar_width, int(progress * bar_width))
        bar = "[" + ("#" * filled) + ("-" * (bar_width - filled)) + "]"
        eta = self._estimated_remaining_seconds(completed, total)
        eta_text = self._format_duration(eta) if eta is not None else "--"
        return (
            f"Progress: {bar} {completed}/{total} "
            f"({progress * 100:.1f}%, ETA {eta_text})"
        )

    def _status_line(self) -> str:
        if self.current_assignment is None:
            return "Current: waiting for first assignment"
        current = self.current_assignment
        name = self._truncate(
            f"{current.assignment_id} {current.assignment_name}", 72
        )
        return (
            f"Current: [{current.index}/{current.total}] {name}"
        )

    def _table_lines(
        self, width: int, *, aggregate_stats: dict[str, int] | None = None
    ) -> list[str]:
        if not self.rows:
            return [
                self._format_row(
                    "Idx",
                    "Assignment",
                    "Unsub",
                    "Miss",
                    "None",
                    "Same",
                    "Exc",
                    "Sub",
                    "Grade",
                    "NoDue",
                    "Err",
                    widths=self._column_widths(width),
                ),
                self._separator_line(width),
                "No assignments completed yet.",
            ]

        widths = self._column_widths(width)
        lines = [
            self._format_row(
                "Idx",
                "Assignment",
                "Unsub",
                "Miss",
                "None",
                "Same",
                "Exc",
                "Sub",
                "Grade",
                "NoDue",
                "Err",
                widths=widths,
            ),
            self._separator_line(width),
        ]
        for row in self.rows:
            lines.append(
                self._format_row(
                    f"{row.index}/{row.total}",
                    f"{row.assignment_id} {row.assignment_name}",
                    row.unsubmitted,
                    row.updated_to_missing,
                    row.updated_to_none,
                    row.unchanged,
                    row.skipped_excused,
                    row.skipped_submitted,
                    row.skipped_existing_grade,
                    row.skipped_no_due_date,
                    row.errors,
                    widths=widths,
                )
            )
        if aggregate_stats is not None:
            lines.append(
                self._format_row(
                    "TOTAL",
                    "(all assignments)",
                    aggregate_stats.get("unsubmitted_considered", 0),
                    aggregate_stats.get("updated_to_missing", 0),
                    aggregate_stats.get("updated_to_none", 0),
                    aggregate_stats.get("unchanged", 0),
                    aggregate_stats.get("skipped_excused", 0),
                    aggregate_stats.get("skipped_submitted", 0),
                    aggregate_stats.get("skipped_existing_grade", 0),
                    aggregate_stats.get("skipped_no_due_date", 0),
                    aggregate_stats.get("errors", 0),
                    widths=widths,
                )
            )
        return lines

    def _legend_line(self, width: int) -> str:
        legend = (
            "Columns: Unsub=unsubmitted rows | Miss=set missing | None=set none | "
            "Same=no change | Exc=excused skip | Sub=submitted/content skip | "
            "Grade=existing grade skip | NoDue=no due date | Err=errors"
        )
        if len(legend) <= width:
            return legend
        short = (
            "Columns: Unsub=unsubmitted | Miss=missing | None=none | Same=no change | "
            "Exc=excused | Sub=submitted | Grade=grade exists | NoDue=no due date | Err=errors"
        )
        return short if len(short) <= width else "Columns: see table abbreviations below"

    @staticmethod
    def _column_widths(width: int) -> dict[str, int]:
        assignment_width = max(32, min(52, width - 68))
        return {
            "idx": 8,
            "assignment": assignment_width,
            "unsub": 7,
            "miss": 6,
            "none": 6,
            "same": 6,
            "exc": 5,
            "sub": 5,
            "grade": 6,
            "nodue": 6,
            "err": 5,
        }

    @staticmethod
    def _separator_line(width: int) -> str:
        return "-" * min(width, 110)

    @staticmethod
    def _format_row(
        *values: object,
        widths: dict[str, int],
    ) -> str:
        idx, assignment, unsub, miss, none, same, exc, sub, grade, nodue, err = values
        assignment_text = CleanupMissingReporter._truncate(
            str(assignment), widths["assignment"]
        )
        return (
            f"{str(idx):<{widths['idx']}} "
            f"{assignment_text:<{widths['assignment']}} "
            f"{str(unsub):>{widths['unsub']}} "
            f"{str(miss):>{widths['miss']}} "
            f"{str(none):>{widths['none']}} "
            f"{str(same):>{widths['same']}} "
            f"{str(exc):>{widths['exc']}} "
            f"{str(sub):>{widths['sub']}} "
            f"{str(grade):>{widths['grade']}} "
            f"{str(nodue):>{widths['nodue']}} "
            f"{str(err):>{widths['err']}}"
        )

    @staticmethod
    def _truncate(value: str, width: int) -> str:
        if width <= 0 or len(value) <= width:
            return value
        if width <= 3:
            return value[:width]
        return value[: width - 3] + "..."

    def _estimated_remaining_seconds(self, completed: int, total: int) -> float | None:
        if completed <= 0 or total <= completed:
            return 0.0 if total <= completed else None
        elapsed = time.monotonic() - self.started_at
        average_per_assignment = elapsed / completed
        return average_per_assignment * (total - completed)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h{minutes:02d}m"
        if minutes > 0:
            return f"{minutes}m{seconds:02d}s"
        return f"{seconds}s"
