"""Canvas Classic Quiz student-analysis report helpers."""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

REPORT_POLL_INTERVAL_SECONDS = 2
REPORT_TIMEOUT_SECONDS = 120


def value_from_canvas_object(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def normalise_likert_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def quiz_likert_values(quiz: Any) -> dict[str, int]:
    """Map each multiple-choice label in a quiz to its one-based scale value."""
    values: dict[str, int] = {}
    for question in quiz.get_questions():
        if getattr(question, "question_type", None) != "multiple_choice_question":
            continue
        for position, answer in enumerate(getattr(question, "answers", []) or [], start=1):
            label = value_from_canvas_object(answer, "answer_text")
            if label is None:
                label = value_from_canvas_object(answer, "text")
            if label is not None:
                values[normalise_likert_label(label)] = position
    return values


def convert_likert_values(csv_text: str, likert_values: dict[str, int]) -> str:
    """Replace known Likert labels in a student-analysis CSV with numbers."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ValueError("Canvas returned a student-analysis report with no CSV header.")

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in reader:
        writer.writerow(
            {
                column: likert_values.get(normalise_likert_label(value), value)
                for column, value in row.items()
            }
        )
    return output.getvalue()


def wait_for_quiz_report(quiz: Any, report: Any) -> Any:
    """Refresh a report until Canvas makes its generated CSV available."""
    deadline = time.monotonic() + REPORT_TIMEOUT_SECONDS
    while True:
        current_report = quiz.get_quiz_report(report.id)
        if value_from_canvas_object(current_report, "file"):
            return current_report

        progress = value_from_canvas_object(current_report, "progress")
        workflow_state = value_from_canvas_object(progress, "workflow_state")
        if workflow_state in {"failed", "cancelled"}:
            raise ValueError(f"Canvas failed to generate the quiz report ({workflow_state}).")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Canvas did not finish generating quiz report {report.id} within "
                f"{REPORT_TIMEOUT_SECONDS} seconds."
            )
        completion = value_from_canvas_object(progress, "completion")
        status = workflow_state or "queued"
        if completion is not None:
            status = f"{status} ({completion}% complete)"
        log.info("Quiz report %s is %s; waiting…", report.id, status)
        time.sleep(REPORT_POLL_INTERVAL_SECONDS)


def download_quiz_report_csv(canvas_course: Any, report: Any) -> str:
    """Download the CSV exposed by a completed Canvas report's File object."""
    report_file = value_from_canvas_object(report, "file")
    url = value_from_canvas_object(report_file, "url")
    if not isinstance(url, str) or not url:
        raise ValueError("Canvas generated the quiz report without a downloadable file URL.")
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {canvas_course.canvas_interface.canvas_key}"},
            timeout=60,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ValueError(f"Could not download the generated quiz report: {exc}") from exc
    return response.content.decode("utf-8-sig")
