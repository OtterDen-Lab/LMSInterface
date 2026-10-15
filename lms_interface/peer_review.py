"""Create Canvas graded-survey peer reviews from PDFs and a YAML rubric."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import logging
import random
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml
from canvasapi.exceptions import ResourceDoesNotExist

from lms_interface.canvas_interface import CanvasCourse
from lms_interface.quiz_reports import (
    convert_likert_values,
    download_quiz_report_csv,
    quiz_likert_values,
    wait_for_quiz_report,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewDocument:
    """A PDF which will receive its own peer-review survey."""

    filename: str
    content: bytes
    owner_id: int | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def short_digest(self) -> str:
        return self.digest[:12]


@dataclass
class PeerReviewPlan:
    config: dict[str, Any]
    documents: list[ReviewDocument]
    reviewer_ids: list[int]
    assignments: dict[int, list[int]]
    coverage: list[int]
    warnings: list[str]


def load_peer_review_config(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise ValueError(f"Could not read peer-review YAML '{path}': {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid peer-review YAML '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Peer-review YAML must contain a mapping.")

    for key in ("unlock_at", "due_at", "lock_at", "rubric"):
        if not data.get(key):
            raise ValueError(f"Peer-review YAML requires '{key}'.")
    if not isinstance(data["rubric"], dict) or not data["rubric"].get("name"):
        raise ValueError("Peer-review YAML requires rubric.name.")
    max_reviews = data.get("reviews_per_student", 3)
    if not isinstance(max_reviews, int) or max_reviews < 1:
        raise ValueError("reviews_per_student must be a positive integer.")
    min_reviews_per_student = data.get("min_reviews_per_student", 1)
    if not isinstance(min_reviews_per_student, int) or min_reviews_per_student < 1:
        raise ValueError("min_reviews_per_student must be a positive integer.")
    if min_reviews_per_student > max_reviews:
        raise ValueError("min_reviews_per_student cannot exceed reviews_per_student.")
    min_reviews_per_submission = data.get("min_reviews_per_submission", 3)
    if not isinstance(min_reviews_per_submission, int) or min_reviews_per_submission < 1:
        raise ValueError("min_reviews_per_submission must be a positive integer.")
    points = data.get("points_possible", 1)
    if not isinstance(points, (int, float)) or points < 0:
        raise ValueError("points_possible must be a non-negative number.")
    published = data.get("published", False)
    if not isinstance(published, bool):
        raise ValueError("published must be a boolean when provided.")
    return data


def load_local_documents(config_path: Path, config: dict[str, Any]) -> list[ReviewDocument]:
    pdfs = config.get("pdfs")
    if not isinstance(pdfs, list) or not pdfs:
        raise ValueError("Local-PDF mode requires a non-empty 'pdfs' list.")
    documents: list[ReviewDocument] = []
    for raw_path in pdfs:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("Each pdfs entry must be a non-empty path string.")
        path = (config_path.parent / raw_path).resolve()
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"Peer-review document is not a PDF: {path}")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Could not read peer-review PDF '{path}': {exc}") from exc
        if not content:
            raise ValueError(f"Peer-review PDF is empty: {path}")
        documents.append(ReviewDocument(filename=path.name, content=content))
    return documents


def load_source_documents(canvas_course: CanvasCourse, source_assignment_id: int) -> tuple[list[ReviewDocument], list[str]]:
    assignment = canvas_course.course.get_assignment(source_assignment_id)
    documents: list[ReviewDocument] = []
    warnings: list[str] = []
    def attachment_value(attachment: Any, key: str) -> Any:
        if isinstance(attachment, dict):
            return attachment.get(key)
        return getattr(attachment, key.replace("-", "_"), None)

    for submission in assignment.get_submissions():
        user_id = getattr(submission, "user_id", None)
        attachments = getattr(submission, "attachments", None) or []
        pdf = next(
            (
                attachment
                for attachment in attachments
                if str(attachment_value(attachment, "filename") or "")
                .lower()
                .endswith(".pdf")
            ),
            None,
        )
        if user_id is None or pdf is None:
            if getattr(submission, "submitted_at", None) is not None:
                warnings.append(
                    f"Skipping source submission for user {user_id}: no PDF attachment."
                )
            continue
        url = attachment_value(pdf, "url")
        if not isinstance(url, str) or not url:
            warnings.append(f"Skipping source submission for user {user_id}: PDF has no URL.")
            continue
        try:
            response = requests.get(
                url,
                headers={
                    "Authorization": (
                        f"Bearer {canvas_course.canvas_interface.canvas_key}"
                    )
                },
                timeout=60,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ValueError(f"Could not download PDF for user {user_id}: {exc}") from exc
        content = response.content
        if not content:
            warnings.append(f"Skipping source submission for user {user_id}: PDF is empty.")
            continue
        documents.append(
            ReviewDocument(
                filename=str(
                    attachment_value(pdf, "filename") or f"submission-{user_id}.pdf"
                ),
                content=content,
                owner_id=int(user_id),
            )
        )
    if not documents:
        raise ValueError("No usable PDF attachments found in the source assignment.")
    return documents, warnings


def allocate_reviews(
    documents: list[ReviewDocument],
    reviewer_ids: list[int],
    *,
    min_reviews_per_student: int = 1,
    max_reviews: int = 3,
    min_reviews_per_submission: int = 3,
    seed: int = 0,
) -> tuple[dict[int, list[int]], list[str]]:
    """Allocate peer reviews while spreading work across all eligible students."""
    if min_reviews_per_student < 1 or max_reviews < min_reviews_per_student:
        raise ValueError("Reviewer minimum/maximum review counts are invalid.")
    if min_reviews_per_submission < 1:
        raise ValueError("min_reviews_per_submission must be positive.")
    if len(documents) * min_reviews_per_submission > len(reviewer_ids) * max_reviews:
        raise ValueError(
            "Peer-review allocation is infeasible: required reviews per document "
            "exceed the total reviewer capacity."
        )
    for document in documents:
        eligible_count = sum(
            reviewer_id != document.owner_id for reviewer_id in reviewer_ids
        )
        if eligible_count < min_reviews_per_submission:
            raise ValueError(
                "Peer-review allocation is infeasible: document "
                f"'{document.filename}' has only {eligible_count} eligible reviewers, "
                f"but requires {min_reviews_per_submission}."
            )

    rng = random.Random(seed)
    assignments: dict[int, set[int]] = {reviewer_id: set() for reviewer_id in reviewer_ids}
    reviewer_order = list(reviewer_ids)
    rng.shuffle(reviewer_order)

    def eligible_documents(reviewer_id: int) -> list[int]:
        return [
            index
            for index, document in enumerate(documents)
            if document.owner_id != reviewer_id and index not in assignments[reviewer_id]
        ]

    def coverage(document_index: int) -> int:
        return sum(document_index in assigned for assigned in assignments.values())

    warnings: list[str] = []
    reviewers_below_minimum: list[int] = []
    # First spread reviews to every student who can receive the requested minimum.
    for reviewer_id in reviewer_order:
        while len(assignments[reviewer_id]) < min_reviews_per_student:
            candidates = eligible_documents(reviewer_id)
            if len(assignments[reviewer_id]) + len(candidates) < min_reviews_per_student:
                reviewers_below_minimum.append(reviewer_id)
                break
            lowest_coverage = min(coverage(index) for index in candidates)
            balanced = [
                index for index in candidates if coverage(index) == lowest_coverage
            ]
            assignments[reviewer_id].add(rng.choice(balanced))

    # Then ensure every source document receives the configured minimum coverage.
    document_order = list(range(len(documents)))
    rng.shuffle(document_order)
    for document_index in document_order:
        while coverage(document_index) < min_reviews_per_submission:
            candidates = [
                reviewer_id
                for reviewer_id in reviewer_ids
                if reviewer_id != documents[document_index].owner_id
                and document_index not in assignments[reviewer_id]
                and len(assignments[reviewer_id]) < max_reviews
            ]
            if not candidates:
                raise ValueError(
                    "Peer-review allocation is infeasible while assigning "
                    f"document '{documents[document_index].filename}'."
                )
            lowest_load = min(len(assignments[reviewer_id]) for reviewer_id in candidates)
            least_loaded = [
                reviewer_id
                for reviewer_id in candidates
                if len(assignments[reviewer_id]) == lowest_load
            ]
            assignments[rng.choice(least_loaded)].add(document_index)

    if reviewers_below_minimum:
        warnings.append(
            f"{len(reviewers_below_minimum)} reviewers could not receive the "
            f"requested minimum of {min_reviews_per_student} distinct reviews "
            "without reviewing their own submission."
        )
    return {reviewer_id: sorted(assigned) for reviewer_id, assigned in assignments.items()}, warnings


def build_question_payloads(config: dict[str, Any]) -> list[dict[str, Any]]:
    rubric = config["rubric"]
    scale = rubric.get("likert_scale") or {}
    labels = scale.get("labels") or {}
    payloads: list[dict[str, Any]] = []
    for section in rubric.get("sections") or []:
        if not isinstance(section, dict):
            raise ValueError("Each rubric section must be a mapping.")
        title = str(section.get("section") or "Peer Review")
        instructions = str(section.get("instructions") or "")
        payloads.append(
            {
                "question_name": title,
                "question_text": (
                    f"<h3>{title}</h3>"
                    f"{f'<p>{instructions}</p>' if instructions else ''}"
                ),
                "question_type": "text_only_question",
                "points_possible": 0,
            }
        )
        for question in section.get("questions") or []:
            if not isinstance(question, dict):
                raise ValueError("Each rubric question must be a mapping.")
            question_id = str(question.get("id") or "").strip()
            prompt = str(question.get("prompt") or "").strip()
            question_type = str(question.get("type") or "").strip()
            if not question_id or not prompt:
                raise ValueError("Each rubric question requires id and prompt.")
            payload: dict[str, Any] = {
                "question_name": question_id,
                "question_text": f"<p>{prompt}</p>",
                "points_possible": 0,
            }
            if question_type == "likert":
                if not labels:
                    raise ValueError("Likert questions require rubric.likert_scale.labels.")
                payload["question_type"] = "multiple_choice_question"
                payload["answers"] = [
                    {"answer_text": str(label), "answer_weight": 0}
                    for _, label in sorted(labels.items(), key=lambda item: int(item[0]))
                ]
            elif question_type == "short_response":
                payload["question_type"] = "essay_question"
            else:
                raise ValueError(
                    f"Question '{question_id}' has unsupported type '{question_type}'."
                )
            payloads.append(payload)
    return payloads


PEER_REVIEW_FILES_FOLDER = "Peer Review Files"
PEER_REVIEW_QUIZ_ID_PATTERN = re.compile(r"\[([0-9a-f]{12})\]\s*$", re.IGNORECASE)


class PeerReviewSummaryError(ValueError):
    """Raised when a peer-review batch cannot be selected unambiguously."""


def _assignment_group_assignments(canvas_course: CanvasCourse, group: Any) -> list[Any]:
    """Return assignments in a group, including when the group was not expanded."""
    assignments = getattr(group, "assignments", None)
    if assignments is not None:
        return list(assignments)
    return list(canvas_course.course.get_assignments_for_group(group.id))


def _format_assignment_group_options(
    canvas_course: CanvasCourse, groups: list[Any]
) -> str:
    lines = ["Multiple assignment groups with that name were found:", ""]
    for group in groups:
        assignments = _assignment_group_assignments(canvas_course, group)
        lines.append(
            f"  group_id={group.id}  position={getattr(group, 'position', '?')}  "
            f"assignments={len(assignments)}"
        )
        for assignment in assignments:
            lines.append(
                f"    - assignment_id={assignment.id}  "
                f"{getattr(assignment, 'name', '(unnamed assignment)')}"
            )
        lines.append("")
    lines.extend(
        [
            "Re-run with --assignment-id <an assignment from the intended group>",
            "or --assignment-group-id <group_id>.",
        ]
    )
    return "\n".join(lines)


def resolve_peer_review_assignment_group(
    canvas_course: CanvasCourse,
    *,
    assignment_id: int | None = None,
    quiz_id: int | None = None,
    assignment_group_id: int | None = None,
    assignment_group_name: str | None = None,
) -> Any:
    """Resolve a peer-review assignment group from a convenient user selector."""
    if not any(
        (
            assignment_id is not None,
            quiz_id is not None,
            assignment_group_id is not None,
            assignment_group_name,
        )
    ):
        raise PeerReviewSummaryError(
            "Specify --assignment-id, --quiz-id, --assignment-group-id, or "
            "--assignment-group-name."
        )

    resolved_group_id = assignment_group_id

    def use_group_id(candidate_group_id: Any, selector: str) -> None:
        nonlocal resolved_group_id
        if candidate_group_id is None:
            raise PeerReviewSummaryError(
                f"{selector} does not belong to an assignment group."
            )
        candidate_group_id = int(candidate_group_id)
        if (
            resolved_group_id is not None
            and candidate_group_id != resolved_group_id
        ):
            raise PeerReviewSummaryError(
                f"{selector} belongs to group {candidate_group_id}, not "
                f"group {resolved_group_id}."
            )
        resolved_group_id = candidate_group_id

    def use_quiz_id(candidate_quiz_id: int) -> None:
        quiz = canvas_course.course.get_quiz(candidate_quiz_id)
        quiz_group_id = getattr(quiz, "assignment_group_id", None)
        if quiz_group_id is not None:
            use_group_id(quiz_group_id, f"Quiz {candidate_quiz_id}")
            return
        linked_assignment_id = getattr(quiz, "assignment_id", None)
        if linked_assignment_id is None:
            raise PeerReviewSummaryError(
                f"Quiz {candidate_quiz_id} has no linked assignment or assignment group."
            )
        linked_assignment = canvas_course.course.get_assignment(linked_assignment_id)
        use_group_id(
            getattr(linked_assignment, "assignment_group_id", None),
            f"Quiz {candidate_quiz_id}",
        )

    if assignment_id is not None:
        try:
            assignment = canvas_course.course.get_assignment(assignment_id)
        except ResourceDoesNotExist:
            log.info(
                "Assignment %s was not found; treating it as a Classic Quiz ID.",
                assignment_id,
            )
            use_quiz_id(assignment_id)
        else:
            use_group_id(
                getattr(assignment, "assignment_group_id", None),
                f"Assignment {assignment_id}",
            )

    if quiz_id is not None:
        use_quiz_id(quiz_id)

    matching_groups: list[Any] = []
    if assignment_group_name:
        matching_groups = [
            group
            for group in canvas_course.course.get_assignment_groups()
            if getattr(group, "name", None) == assignment_group_name
        ]
        if not matching_groups:
            raise PeerReviewSummaryError(
                f"No assignment group named '{assignment_group_name}' was found."
            )
        if len(matching_groups) > 1:
            raise PeerReviewSummaryError(
                _format_assignment_group_options(canvas_course, matching_groups)
            )
        named_group_id = int(matching_groups[0].id)
        if resolved_group_id is not None and named_group_id != resolved_group_id:
            raise PeerReviewSummaryError(
                f"The supplied assignment/group selectors disagree: group "
                f"'{assignment_group_name}' is {named_group_id}, not {resolved_group_id}."
            )
        resolved_group_id = named_group_id

    if matching_groups:
        return matching_groups[0]
    assert resolved_group_id is not None
    return canvas_course.course.get_assignment_group(resolved_group_id)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value if value is not None else ""


def _normalise_question_prompt(value: str) -> str:
    plain_text = re.sub(r"<[^>]+>", " ", html.unescape(value))
    return re.sub(r"\s+", " ", plain_text).strip().casefold()


def _rubric_questions(config_path: Path | None) -> tuple[list[str], dict[str, str]]:
    if config_path is None:
        return [], {}
    config = load_peer_review_config(config_path)
    questions = [
        payload
        for payload in build_question_payloads(config)
        if payload["question_type"] != "text_only_question"
    ]
    return (
        [str(question["question_name"]) for question in questions],
        {
            _normalise_question_prompt(str(question["question_text"])): str(
                question["question_name"]
            )
            for question in questions
        },
    )


_STUDENT_ANALYSIS_METADATA_COLUMNS = {
    "student",
    "student name",
    "id",
    "student id",
    "student id (canvas)",
    "user id",
    "sis user id",
    "sis id",
    "section",
    "section id",
    "attempt",
    "started at",
    "start time",
    "completed at",
    "completion time",
    "time",
    "time spent",
    "score",
    "submission id",
}


def _student_analysis_value(row: dict[str, str], *names: str) -> str:
    normalised_names = {name.strip().casefold() for name in names}
    for column, value in row.items():
        if column.strip().casefold() in normalised_names:
            return value or ""
    return ""


_STUDENT_ANALYSIS_QUESTION_COLUMN = re.compile(r"^\s*\d+\s*:\s*(.+?)\s*$")


def _student_analysis_question_columns(fieldnames: list[str]) -> list[tuple[str, str]]:
    """Return `(column, prompt)` pairs for Canvas's numbered question columns."""
    columns: list[tuple[str, str]] = []
    for column in fieldnames:
        match = _STUDENT_ANALYSIS_QUESTION_COLUMN.match(column)
        if match:
            columns.append((column, match.group(1)))
    return columns


def export_peer_review_summary(
    canvas_course: CanvasCourse,
    *,
    output_path: Path,
    assignment_id: int | None = None,
    quiz_id: int | None = None,
    assignment_group_id: int | None = None,
    assignment_group_name: str | None = None,
    source_assignment_id: int | None = None,
    rubric_config_path: Path | None = None,
    source_documents: list[ReviewDocument] | None = None,
) -> int:
    """Export completed peer-review survey responses to a CSV file.

    The PDF identifier is read from the generated quiz title.  Supplying the
    original source assignment additionally resolves that identifier to the
    submitting student's Canvas user ID.
    """
    group = resolve_peer_review_assignment_group(
        canvas_course,
        assignment_id=assignment_id,
        quiz_id=quiz_id,
        assignment_group_id=assignment_group_id,
        assignment_group_name=assignment_group_name,
    )
    assignments = _assignment_group_assignments(canvas_course, group)
    assignments_by_id = {int(assignment.id): assignment for assignment in assignments}
    quizzes = [
        quiz
        for quiz in canvas_course.get_quizzes()
        if getattr(quiz, "assignment_id", None) in assignments_by_id
        and getattr(quiz, "quiz_type", None) == "graded_survey"
        and PEER_REVIEW_QUIZ_ID_PATTERN.search(str(getattr(quiz, "title", "")))
    ]
    if not quizzes:
        raise PeerReviewSummaryError(
            f"Assignment group {group.id} contains no peer-review graded surveys."
        )

    submitter_ids: dict[str, int] = {}
    if source_documents is not None or source_assignment_id is not None:
        documents = source_documents
        if documents is None:
            documents, _ = load_source_documents(canvas_course, source_assignment_id)
        submitter_ids = {
            document.short_digest: document.owner_id
            for document in documents
            if document.owner_id is not None
        }

    rubric_question_names, rubric_questions_by_prompt = _rubric_questions(
        rubric_config_path
    )
    question_names: set[str] = set(rubric_question_names)
    rows: list[dict[str, Any]] = []

    # Ask Canvas to generate every report before waiting for any one of them.
    # This lets its background workers generate the whole peer-review batch in
    # parallel rather than serializing report creation and polling.
    requested_reports: list[tuple[Any, Any]] = []
    for quiz in quizzes:
        raw_quiz = getattr(quiz, "quiz", quiz)
        try:
            requested_reports.append((quiz, raw_quiz.create_report("student_analysis")))
        except Exception as exc:
            raise PeerReviewSummaryError(
                f"Could not request a student-analysis report for quiz {quiz.id}: {exc}"
            ) from exc

    for quiz, requested_report in requested_reports:
        document_id = PEER_REVIEW_QUIZ_ID_PATTERN.search(quiz.title).group(1).lower()
        raw_quiz = getattr(quiz, "quiz", quiz)
        try:
            completed_report = wait_for_quiz_report(raw_quiz, requested_report)
            report_csv = download_quiz_report_csv(canvas_course, completed_report)
            normalised_csv = convert_likert_values(
                report_csv, quiz_likert_values(raw_quiz)
            )
        except (TimeoutError, ValueError) as exc:
            raise PeerReviewSummaryError(
                f"Could not retrieve the student-analysis report for quiz {quiz.id}: {exc}"
            ) from exc

        reader = csv.DictReader(io.StringIO(normalised_csv))
        if not reader.fieldnames:
            raise PeerReviewSummaryError(
                f"Student-analysis report for quiz {quiz.id} has no CSV header."
            )
        report_question_columns = _student_analysis_question_columns(reader.fieldnames)
        for report_row in reader:
            reviewer_id = _student_analysis_value(
                report_row, "ID", "Student ID", "Student ID (Canvas)", "User ID"
            )
            if not reviewer_id:
                log.warning(
                    "Skipping a row in quiz %s's student-analysis report because it has no student ID.",
                    quiz.id,
                )
                continue
            row: dict[str, Any] = {
                "pdf_id": document_id,
                "submitter_id": submitter_ids.get(document_id, ""),
                "quiz_id": quiz.id,
                "quiz_submission_id": _student_analysis_value(
                    report_row, "Submission ID"
                ),
                "reviewer_id": reviewer_id,
                "attempt": _student_analysis_value(report_row, "Attempt"),
                "workflow_state": "complete",
                "submitted_at": _student_analysis_value(
                    report_row, "Completed At", "Completion Time"
                ),
            }
            for column, question_prompt in report_question_columns:
                question_name = rubric_questions_by_prompt.get(
                    _normalise_question_prompt(question_prompt)
                )
                # Section headings are emitted by Canvas as numbered report
                # columns too.  With the rubric config, only questions whose
                # prompt is known are responses we should export.
                if question_name is None and rubric_questions_by_prompt:
                    continue
                if question_name is None:
                    question_name = question_prompt
                question_names.add(question_name)
                row[question_name] = _csv_value(report_row.get(column, ""))
            rows.append(row)

    fieldnames = [
        "pdf_id",
        "submitter_id",
        "quiz_id",
        "quiz_submission_id",
        "reviewer_id",
        "attempt",
        "workflow_state",
        "submitted_at",
        *sorted(question_names),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as output_file:
        writer = csv.DictWriter(
            output_file, fieldnames=fieldnames, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _rubric_question_ids_by_type(config_path: Path) -> tuple[list[str], list[str]]:
    config = load_peer_review_config(config_path)
    payloads = [
        payload
        for payload in build_question_payloads(config)
        if payload["question_type"] != "text_only_question"
    ]
    question_ids = [str(payload["question_name"]) for payload in payloads]
    likert_ids = [
        str(payload["question_name"])
        for payload in payloads
        if payload["question_type"] == "multiple_choice_question"
    ]
    return question_ids, likert_ids


def _peer_score(value: str) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if 1 <= score <= 4 else None


def _select_instructor_review_pdfs(
    rows: list[dict[str, str]], likert_question_ids: list[str], *, seed: int
) -> list[tuple[str, str, str]]:
    """Choose low, middle, high, and two random papers for instructor review."""
    scores_by_pdf: dict[str, list[float]] = {}
    review_counts: dict[str, int] = {}
    submitter_ids: dict[str, str] = {}
    for row in rows:
        pdf_id = row.get("pdf_id", "")
        if not pdf_id:
            continue
        scores = [
            score
            for question_id in likert_question_ids
            if (score := _peer_score(row.get(question_id, ""))) is not None
        ]
        if not scores:
            continue
        scores_by_pdf.setdefault(pdf_id, []).append(sum(scores) / len(scores))
        review_counts[pdf_id] = review_counts.get(pdf_id, 0) + 1
        submitter_ids[pdf_id] = row.get("submitter_id", "")

    aggregated = sorted(
        (
            (
                sum(scores) / len(scores),
                pdf_id,
                submitter_ids.get(pdf_id, ""),
                review_counts[pdf_id],
            )
            for pdf_id, scores in scores_by_pdf.items()
        ),
        key=lambda item: (item[0], item[1]),
    )
    if len(aggregated) < 3:
        raise PeerReviewSummaryError(
            "Need at least three peer-reviewed PDFs with Likert responses to create "
            "an instructor-review workspace."
        )

    selected: list[tuple[float, str, str, int, str]] = [
        (*aggregated[0], "low"),
        (*aggregated[-1], "high"),
    ]
    selected_pdf_ids = {aggregated[0][1], aggregated[-1][1]}
    overall_median = aggregated[len(aggregated) // 2][0]
    middle_candidates = [
        candidate
        for candidate in aggregated
        if candidate[1] not in selected_pdf_ids and candidate[3] >= 2
    ]
    if not middle_candidates:
        middle_candidates = [
            candidate for candidate in aggregated if candidate[1] not in selected_pdf_ids
        ]
    middle = min(middle_candidates, key=lambda item: (abs(item[0] - overall_median), item[1]))
    selected.append((*middle, "middle"))
    selected_pdf_ids.add(middle[1])

    remaining = [candidate for candidate in aggregated if candidate[1] not in selected_pdf_ids]
    random_count = min(2, len(remaining))
    for candidate in random.Random(seed).sample(remaining, random_count):
        selected.append((*candidate, "random"))
    return [(pdf_id, submitter_id, bucket) for _, pdf_id, submitter_id, _, bucket in selected]


def create_peer_review_workspace(
    canvas_course: CanvasCourse,
    *,
    work_dir: Path,
    course_id: int,
    quiz_id: int | None,
    assignment_id: int | None,
    assignment_group_id: int | None,
    assignment_group_name: str | None,
    source_assignment_id: int,
    rubric_config_path: Path,
    seed: int = 0,
) -> Path:
    """Create a self-contained, manually editable peer-review grading workspace."""
    workspace_config_path = work_dir / "peer-review-workspace.yaml"
    instructor_review_path = work_dir / "professor-review.csv"
    if workspace_config_path.exists() or instructor_review_path.exists():
        raise PeerReviewSummaryError(
            f"Workspace '{work_dir}' already contains grading files. Choose a new "
            "--work-dir to avoid overwriting manual instructor feedback."
        )

    documents, warnings = load_source_documents(canvas_course, source_assignment_id)
    for warning in warnings:
        log.warning(warning)

    work_dir.mkdir(parents=True, exist_ok=True)
    summary_path = work_dir / "peer-review-results.csv"
    export_peer_review_summary(
        canvas_course,
        output_path=summary_path,
        assignment_id=assignment_id,
        quiz_id=quiz_id,
        assignment_group_id=assignment_group_id,
        assignment_group_name=assignment_group_name,
        source_assignment_id=source_assignment_id,
        rubric_config_path=rubric_config_path,
        source_documents=documents,
    )

    with summary_path.open(encoding="utf-8-sig", newline="") as summary_file:
        summary_rows = list(csv.DictReader(summary_file))
    question_ids, likert_question_ids = _rubric_question_ids_by_type(rubric_config_path)
    selected = _select_instructor_review_pdfs(
        summary_rows, likert_question_ids, seed=seed
    )
    documents_by_id = {document.short_digest: document for document in documents}

    pdf_dir = work_dir / "selected-pdfs"
    pdf_dir.mkdir(exist_ok=True)
    template_rows: list[dict[str, str]] = []
    for pdf_id, submitter_id, _ in selected:
        document = documents_by_id.get(pdf_id)
        if document is None:
            raise PeerReviewSummaryError(
                f"Could not find the source PDF for selected review '{pdf_id}'."
            )
        pdf_path = pdf_dir / f"{pdf_id}.pdf"
        pdf_path.write_bytes(document.content)
        template_rows.append(
            {
                "pdf_id": pdf_id,
                "submitter_id": submitter_id,
                "pdf_path": str(pdf_path.relative_to(work_dir)),
                **{question_id: "" for question_id in question_ids},
                "instructor_notes": "",
            }
        )
    random.Random(seed).shuffle(template_rows)
    template_fieldnames = [
        "pdf_id",
        "submitter_id",
        "pdf_path",
        *question_ids,
        "instructor_notes",
    ]
    with instructor_review_path.open("w", newline="", encoding="utf-8-sig") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=template_fieldnames)
        writer.writeheader()
        writer.writerows(template_rows)

    workspace_config = {
        "course_id": course_id,
        "quiz_id": quiz_id,
        "assignment_id": assignment_id,
        "assignment_group_id": assignment_group_id,
        "assignment_group_name": assignment_group_name,
        "source_assignment_id": source_assignment_id,
        "rubric_config_path": str(rubric_config_path.resolve()),
        "selection": {
            "low_count": 1,
            "middle_count": 1,
            "high_count": 1,
            "random_count": sum(bucket == "random" for _, _, bucket in selected),
            "minimum_reviews_for_middle": 2,
            "selected_pdf_ids": [pdf_id for pdf_id, _, _ in selected],
            "shuffle_seed": seed,
        },
        "scoring": {
            "paper": {
                "completion_floor_percent": 40,
                "quality_weight_percent": 60,
            },
            "peer_review_participation": {
                "completion_weight_percent": 60,
                "written_feedback_weight_percent": 40,
            },
        },
        "canvas_push": {
            "paper_assignment_id": source_assignment_id,
            "peer_review_participation_assignment_id": None,
            "post_student_feedback": False,
        },
        "files": {
            "peer_review_results": summary_path.name,
            "professor_review": instructor_review_path.name,
            "selected_pdfs": pdf_dir.name,
        },
    }
    workspace_config_path.write_text(
        yaml.safe_dump(workspace_config, sort_keys=False), encoding="utf-8"
    )
    return work_dir


def _rubric_review_fields(
    config_path: Path,
) -> tuple[list[str], dict[str, str], list[str], dict[str, str]]:
    """Return Likert IDs/prompts and text IDs/student-visible prompts."""
    config = load_peer_review_config(config_path)
    likert_ids: list[str] = []
    likert_prompts: dict[str, str] = {}
    text_ids: list[str] = []
    visible_text_prompts: dict[str, str] = {}
    for section in config["rubric"].get("sections") or []:
        for question in section.get("questions") or []:
            question_id = str(question["id"])
            if question.get("type") == "likert":
                likert_ids.append(question_id)
                likert_prompts[question_id] = str(question["prompt"])
            elif question.get("type") == "short_response":
                text_ids.append(question_id)
                if question.get("visible_to_author"):
                    visible_text_prompts[question_id] = str(question["prompt"])
    return likert_ids, likert_prompts, text_ids, visible_text_prompts


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _format_score(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def finalize_peer_review_workspace(work_dir: Path) -> dict[str, Path]:
    """Create local proposed grades, participation checks, and feedback drafts.

    This operation deliberately has no Canvas side effects. It uses the
    instructor-scored sample to apply a bounded, per-criterion additive
    calibration to peer aggregates, but leaves score posting for a later,
    explicit workflow.
    """
    config_path = work_dir / "peer-review-workspace.yaml"
    if not config_path.is_file():
        raise PeerReviewSummaryError(
            f"No peer-review workspace configuration found at '{config_path}'."
        )
    config = yaml.safe_load(config_path.read_text())
    if not isinstance(config, dict):
        raise PeerReviewSummaryError("Workspace configuration must be a YAML mapping.")
    files = config.get("files") or {}
    results_path = work_dir / str(files.get("peer_review_results", "peer-review-results.csv"))
    professor_path = work_dir / str(files.get("professor_review", "professor-review.csv"))
    rubric_path = Path(str(config.get("rubric_config_path", "")))
    if not results_path.is_file() or not professor_path.is_file() or not rubric_path.is_file():
        raise PeerReviewSummaryError(
            "Workspace is missing its peer results, professor review, or rubric YAML file."
        )

    with results_path.open(encoding="utf-8-sig", newline="") as input_file:
        peer_rows = list(csv.DictReader(input_file))
    with professor_path.open(encoding="utf-8-sig", newline="") as input_file:
        professor_rows = list(csv.DictReader(input_file))
    likert_ids, likert_prompts, text_ids, visible_text_prompts = (
        _rubric_review_fields(rubric_path)
    )

    instructor_by_pdf = {row.get("pdf_id", ""): row for row in professor_rows}
    missing_instructor_scores = [
        (pdf_id, question_id)
        for pdf_id, row in instructor_by_pdf.items()
        for question_id in likert_ids
        if _peer_score(row.get(question_id, "")) is None
    ]
    if missing_instructor_scores:
        missing = ", ".join(
            f"{pdf_id}:{question_id}" for pdf_id, question_id in missing_instructor_scores
        )
        raise PeerReviewSummaryError(
            f"Professor review has missing or invalid 1–4 Likert scores: {missing}."
        )

    rows_by_pdf: dict[str, list[dict[str, str]]] = {}
    for row in peer_rows:
        pdf_id = row.get("pdf_id", "")
        if pdf_id:
            rows_by_pdf.setdefault(pdf_id, []).append(row)

    # A calibration score is the mean instructor-minus-peer difference for a
    # criterion across the independently reviewed sample.  This is intentionally
    # a simple correction: three reviewed papers are enough to reveal a useful
    # systematic bias, but not enough to responsibly fit a more complex model.
    calibration_deltas: dict[str, float] = {}
    calibration_summary_rows: list[dict[str, str]] = []
    for question_id in likert_ids:
        peer_scores: list[float] = []
        instructor_scores: list[float] = []
        for pdf_id, instructor_row in instructor_by_pdf.items():
            criterion_scores = [
                score
                for row in rows_by_pdf.get(pdf_id, [])
                if (score := _peer_score(row.get(question_id, ""))) is not None
            ]
            instructor_score = _peer_score(instructor_row.get(question_id, ""))
            peer_score = _mean(criterion_scores)
            if peer_score is not None and instructor_score is not None:
                peer_scores.append(peer_score)
                instructor_scores.append(instructor_score)
        peer_mean = _mean(peer_scores)
        instructor_mean = _mean(instructor_scores)
        delta = (
            instructor_mean - peer_mean
            if peer_mean is not None and instructor_mean is not None
            else 0.0
        )
        calibration_deltas[question_id] = delta
        calibration_summary_rows.append(
            {
                "criterion_id": question_id,
                "calibration_sample_count": str(len(peer_scores)),
                "calibration_peer_mean": _format_score(peer_mean),
                "calibration_instructor_mean": _format_score(instructor_mean),
                "instructor_minus_peer_delta": _format_score(delta),
            }
        )

    scoring = config.get("scoring") or {}
    paper_scoring = scoring.get("paper") or {}
    completion_floor = float(paper_scoring.get("completion_floor_percent", 40))
    quality_weight = float(paper_scoring.get("quality_weight_percent", 60))
    participation_scoring = scoring.get("peer_review_participation") or {}
    completion_weight = float(participation_scoring.get("completion_weight_percent", 60))
    written_weight = float(
        participation_scoring.get("written_feedback_weight_percent", 40)
    )

    grade_rows: list[dict[str, str]] = []
    feedback_dir = work_dir / "feedback"
    feedback_dir.mkdir(exist_ok=True)
    for pdf_id, pdf_rows in sorted(rows_by_pdf.items()):
        submitter_id = pdf_rows[0].get("submitter_id", "")
        criterion_means: dict[str, float | None] = {
            question_id: _mean(
                [
                    score
                    for row in pdf_rows
                    if (score := _peer_score(row.get(question_id, ""))) is not None
                ]
            )
            for question_id in likert_ids
        }
        peer_mean = _mean(
            [score for score in criterion_means.values() if score is not None]
        )
        calibrated_criterion_means = {
            question_id: (
                None
                if criterion_mean is None
                else max(1.0, min(4.0, criterion_mean + calibration_deltas[question_id]))
            )
            for question_id, criterion_mean in criterion_means.items()
        }
        calibrated_peer_mean = _mean(
            [
                score
                for score in calibrated_criterion_means.values()
                if score is not None
            ]
        )
        instructor_row = instructor_by_pdf.get(pdf_id)
        instructor_mean = None
        if instructor_row is not None:
            instructor_scores = [
                _peer_score(instructor_row[question_id]) for question_id in likert_ids
            ]
            instructor_mean = _mean([score for score in instructor_scores if score is not None])
        provisional_grade = None
        if peer_mean is not None:
            provisional_grade = completion_floor + quality_weight * ((peer_mean - 1) / 3)
        calibrated_grade = None
        if calibrated_peer_mean is not None:
            calibrated_grade = completion_floor + quality_weight * (
                (calibrated_peer_mean - 1) / 3
            )
        grade_rows.append(
            {
                "pdf_id": pdf_id,
                "submitter_id": submitter_id,
                "completed_peer_reviews": str(len(pdf_rows)),
                "peer_likert_mean": _format_score(peer_mean),
                "raw_peer_grade_percent": _format_score(provisional_grade),
                "calibrated_likert_mean": _format_score(calibrated_peer_mean),
                "calibration_likert_adjustment": _format_score(
                    None
                    if peer_mean is None or calibrated_peer_mean is None
                    else calibrated_peer_mean - peer_mean
                ),
                "proposed_grade_percent": _format_score(calibrated_grade),
                "calibration_grade_adjustment_percent": _format_score(
                    None
                    if provisional_grade is None or calibrated_grade is None
                    else calibrated_grade - provisional_grade
                ),
                "instructor_likert_mean": _format_score(instructor_mean),
                "instructor_minus_peer": _format_score(
                    None
                    if peer_mean is None or instructor_mean is None
                    else instructor_mean - peer_mean
                ),
                "status": "instructor_calibration" if instructor_row else "peer_only",
            }
        )

        feedback_lines = ["# Peer-review feedback", "", "## Rubric summary", ""]
        if instructor_row is not None:
            feedback_lines.extend(
                [
                    "| Criterion | Peer mean | Instructor score | Responses |",
                    "| --- | ---: | ---: | ---: |",
                ]
            )
        else:
            feedback_lines.extend(
                ["| Criterion | Peer mean | Responses |", "| --- | ---: | ---: |"]
            )
        for question_id in likert_ids:
            response_count = sum(
                _peer_score(row.get(question_id, "")) is not None for row in pdf_rows
            )
            prompt = likert_prompts.get(question_id, question_id).replace("\n", " ")
            if instructor_row is not None:
                feedback_lines.append(
                    "| "
                    f"{prompt} | {_format_score(criterion_means[question_id])} | "
                    f"{_format_score(_peer_score(instructor_row.get(question_id, '')))} | "
                    f"{response_count} |"
                )
            else:
                feedback_lines.append(
                    f"| {prompt} | {_format_score(criterion_means[question_id])} | "
                    f"{response_count} |"
                )
        feedback_lines.extend(
            [
                "",
                "These are the original peer-review ratings. Your course grade may "
                "also reflect instructor calibration of peer-review scores.",
            ]
        )
        for question_id, prompt in visible_text_prompts.items():
            comments = [
                row.get(question_id, "").strip()
                for row in pdf_rows
                if row.get(question_id, "").strip()
            ]
            if comments:
                feedback_lines.extend(["", f"## {prompt}", ""])
                feedback_lines.extend(f"- {comment}" for comment in comments)
        if instructor_row and instructor_row.get("instructor_notes", "").strip():
            feedback_lines.extend(
                ["", "## Instructor feedback", "", instructor_row["instructor_notes"].strip()]
            )
        (feedback_dir / f"{pdf_id}.md").write_text(
            "\n".join(feedback_lines) + "\n", encoding="utf-8"
        )

    participation_by_reviewer: dict[str, list[dict[str, str]]] = {}
    for row in peer_rows:
        reviewer_id = row.get("reviewer_id", "")
        if reviewer_id:
            participation_by_reviewer.setdefault(reviewer_id, []).append(row)
    participation_rows: list[dict[str, str]] = []
    for reviewer_id, reviewer_rows in sorted(participation_by_reviewer.items()):
        per_review_scores: list[float] = []
        missing_likert_cells = 0
        missing_written_cells = 0
        for row in reviewer_rows:
            likert_answered = sum(
                _peer_score(row.get(question_id, "")) is not None
                for question_id in likert_ids
            )
            written_answered = sum(
                bool(row.get(question_id, "").strip()) for question_id in text_ids
            )
            missing_likert_cells += len(likert_ids) - likert_answered
            missing_written_cells += len(text_ids) - written_answered
            per_review_scores.append(
                completion_weight * likert_answered / len(likert_ids)
                + written_weight * written_answered / len(text_ids)
            )
        flags: list[str] = []
        if missing_likert_cells:
            flags.append("missing_likert")
        if missing_written_cells:
            flags.append("missing_written_feedback")
        participation_rows.append(
            {
                "reviewer_id": reviewer_id,
                "completed_reviews": str(len(reviewer_rows)),
                "average_review_participation_percent": _format_score(
                    _mean(per_review_scores)
                ),
                "missing_likert_cells": str(missing_likert_cells),
                "missing_written_cells": str(missing_written_cells),
                "flags": ";".join(flags),
                "manual_override_percent": "",
            }
        )

    output_paths = {
        "proposed_paper_grades": work_dir / "proposed-paper-grades.csv",
        "calibration": work_dir / "instructor-peer-calibration.csv",
        "participation": work_dir / "reviewer-participation.csv",
        "feedback": feedback_dir,
    }
    for name, output_rows, fieldnames in (
        (
            "proposed_paper_grades",
            grade_rows,
            [
                "pdf_id",
                "submitter_id",
                "completed_peer_reviews",
                "peer_likert_mean",
                "raw_peer_grade_percent",
                "calibrated_likert_mean",
                "calibration_likert_adjustment",
                "proposed_grade_percent",
                "calibration_grade_adjustment_percent",
                "instructor_likert_mean",
                "instructor_minus_peer",
                "status",
            ],
        ),
        (
            "calibration",
            calibration_summary_rows,
            [
                "criterion_id",
                "calibration_sample_count",
                "calibration_peer_mean",
                "calibration_instructor_mean",
                "instructor_minus_peer_delta",
            ],
        ),
        (
            "participation",
            participation_rows,
            [
                "reviewer_id",
                "completed_reviews",
                "average_review_participation_percent",
                "missing_likert_cells",
                "missing_written_cells",
                "flags",
                "manual_override_percent",
            ],
        ),
    ):
        with output_paths[name].open("w", newline="", encoding="utf-8-sig") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)
    return output_paths


def push_peer_review_workspace(
    canvas_course: CanvasCourse,
    work_dir: Path,
    *,
    push: bool = False,
    include_comments: bool = True,
) -> dict[str, int]:
    """Dry-run or push finalized paper feedback and participation grades.

    Paper grades and their feedback are deliberately sent in the same Canvas
    submission update.  The caller must pass ``push=True`` to make any write.
    """
    config_path = work_dir / "peer-review-workspace.yaml"
    if not config_path.is_file():
        raise PeerReviewSummaryError(
            f"No peer-review workspace configuration found at '{config_path}'."
        )
    config = yaml.safe_load(config_path.read_text())
    if not isinstance(config, dict):
        raise PeerReviewSummaryError("Workspace configuration must be a YAML mapping.")
    files = config.get("files") or {}
    grades_path = work_dir / "proposed-paper-grades.csv"
    participation_path = work_dir / "reviewer-participation.csv"
    feedback_dir = work_dir / "feedback"
    if not grades_path.is_file() or not participation_path.is_file() or not feedback_dir.is_dir():
        raise PeerReviewSummaryError(
            "Run peer-review-finalize before preparing a Canvas grade push."
        )
    canvas_push = config.get("canvas_push") or {}
    paper_assignment_id = canvas_push.get("paper_assignment_id")
    participation_assignment_id = canvas_push.get("peer_review_participation_assignment_id")
    if not paper_assignment_id or not participation_assignment_id:
        raise PeerReviewSummaryError(
            "Workspace canvas_push must specify both paper_assignment_id and "
            "peer_review_participation_assignment_id."
        )

    with grades_path.open(encoding="utf-8-sig", newline="") as input_file:
        paper_rows = list(csv.DictReader(input_file))
    with participation_path.open(encoding="utf-8-sig", newline="") as input_file:
        participation_rows = list(csv.DictReader(input_file))

    planned_papers: list[tuple[int, float, str]] = []
    for row in paper_rows:
        pdf_id = row.get("pdf_id", "")
        feedback_path = feedback_dir / f"{pdf_id}.md"
        try:
            user_id = int(row["submitter_id"])
            score = float(row["proposed_grade_percent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerReviewSummaryError(
                f"Invalid proposed paper grade row for PDF '{pdf_id}'."
            ) from exc
        if not feedback_path.is_file():
            raise PeerReviewSummaryError(f"Missing feedback file '{feedback_path}'.")
        planned_papers.append((user_id, score, feedback_path.read_text(encoding="utf-8")))

    planned_participation: list[tuple[int, float]] = []
    for row in participation_rows:
        try:
            user_id = int(row["reviewer_id"])
            score = float(
                row.get("manual_override_percent", "").strip()
                or row["average_review_participation_percent"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerReviewSummaryError(
                "Invalid reviewer participation row; use a numeric manual override "
                "or regenerate the workspace."
            ) from exc
        planned_participation.append((user_id, score))

    result = {
        "paper_grades_and_comments": len(planned_papers),
        "participation_grades": len(planned_participation),
        "written": 0,
    }
    if not push:
        return result
    paper_assignment = canvas_course.course.get_assignment(int(paper_assignment_id))
    participation_assignment = canvas_course.course.get_assignment(
        int(participation_assignment_id)
    )
    for user_id, score, feedback in planned_papers:
        payload: dict[str, Any] = {"posted_grade": f"{score:g}%"}
        kwargs: dict[str, Any] = {"submission": payload}
        if include_comments:
            kwargs["comment"] = {"text_comment": feedback}
        paper_assignment.get_submission(user_id).edit(**kwargs)
        result["written"] += 1
    for user_id, score in planned_participation:
        participation_assignment.get_submission(user_id).edit(
            submission={"posted_grade": f"{score:g}%"}
        )
        result["written"] += 1
    return result


def _upload_course_pdf(canvas_course: CanvasCourse, document: ReviewDocument) -> int:
    # CanvasAPI derives the uploaded filename from the temporary path, so create
    # that path with a meaningful basename instead of passing a ignored `name=`.
    upload_name = f"{document.short_digest}.pdf"
    with tempfile.TemporaryDirectory(prefix="peer_review_pdf_") as temp_dir:
        upload_path = Path(temp_dir) / upload_name
        upload_path.write_bytes(document.content)
        success, uploaded = canvas_course.course.upload(
            upload_path,
            parent_folder_path=PEER_REVIEW_FILES_FOLDER,
            on_duplicate="rename",
        )
    if not success or not isinstance(uploaded, dict) or uploaded.get("id") is None:
        raise ValueError(f"Canvas failed to upload peer-review PDF '{document.filename}'.")
    return int(uploaded["id"])


def _canvas_base_url(canvas_course: CanvasCourse) -> str:
    base_url = canvas_course.canvas_interface.canvas_url.rstrip("/")
    if base_url.endswith("/api/v1"):
        return base_url[: -len("/api/v1")]
    return base_url


def _pdf_file_link(canvas_course: CanvasCourse, file_id: int, filename: str) -> str:
    """Return Canvas RCE markup for its native, inline document viewer."""
    base_url = _canvas_base_url(canvas_course)
    course_id = canvas_course.course.id
    file_url = f"{base_url}/courses/{course_id}/files/{file_id}/download?wrap=1"
    api_url = f"{base_url}/api/v1/courses/{course_id}/files/{file_id}"
    return (
        '<p><a class="instructure_file_link instructure_scribd_file" '
        'data-canvas-previewable="true" '
        f'data-api-endpoint="{api_url}" data-api-returntype="File" '
        f'href="{file_url}" title="{filename}">{filename}</a></p>'
    )


def _quiz_url(canvas_course: CanvasCourse, quiz: Any) -> str:
    html_url = getattr(quiz, "html_url", None)
    if isinstance(html_url, str) and html_url:
        return html_url
    return (
        f"{_canvas_base_url(canvas_course)}/courses/{canvas_course.course.id}/quizzes/"
        f"{quiz.id}"
    )


def create_peer_reviews(
    canvas_course: CanvasCourse,
    *,
    config_path: Path,
    source_assignment_id: int | None = None,
    seed: int = 0,
    dry_run: bool = False,
) -> PeerReviewPlan:
    """Validate and create one restricted graded survey for each review PDF."""
    config = load_peer_review_config(config_path)
    if source_assignment_id is None:
        documents = load_local_documents(config_path, config)
        source_warnings: list[str] = []
    else:
        documents, source_warnings = load_source_documents(canvas_course, source_assignment_id)
    document_hashes = [document.digest for document in documents]
    if len(document_hashes) != len(set(document_hashes)):
        raise ValueError("Peer-review PDFs must have distinct content hashes.")
    question_payloads = build_question_payloads(config)
    reviewer_ids = sorted({int(student.user_id) for student in canvas_course.get_students()})
    if not reviewer_ids:
        raise ValueError("No active students are enrolled in this Canvas course.")
    assignments, allocation_warnings = allocate_reviews(
        documents,
        reviewer_ids,
        min_reviews_per_student=int(config.get("min_reviews_per_student", 1)),
        max_reviews=int(config.get("reviews_per_student", 3)),
        min_reviews_per_submission=int(config.get("min_reviews_per_submission", 3)),
        seed=seed,
    )
    coverage = [sum(index in assigned for assigned in assignments.values()) for index in range(len(documents))]
    plan = PeerReviewPlan(
        config=config,
        documents=documents,
        reviewer_ids=reviewer_ids,
        assignments=assignments,
        coverage=coverage,
        warnings=[*source_warnings, *allocation_warnings],
    )
    _log_plan(plan, dry_run=dry_run)
    if dry_run:
        return plan

    log.warning("Creating a new peer-review batch; rerunning this command creates duplicates.")
    published = bool(config.get("published", False))
    if not published:
        log.info("Peer-review surveys will be left unpublished for instructor review.")
    group = canvas_course.create_assignment_group(name="Peer Reviews", delete_existing=False)
    quiz_prefix = str(config["rubric"]["name"]).strip()
    for document_index, document in enumerate(documents):
        file_id = _upload_course_pdf(canvas_course, document)
        title = f"{quiz_prefix} [{document.short_digest}]"
        instructions = str(config["rubric"].get("instructions") or "")
        preview_instruction = (
            "<p><strong>PDF review document:</strong> Click the filename below to "
            "preview or download the PDF.</p>"
        )
        description = (
            f"<p>{instructions}</p>" if instructions else ""
        ) + preview_instruction + _pdf_file_link(
            canvas_course, file_id, f"{document.short_digest}.pdf"
        )
        quiz = canvas_course.course.create_quiz(
            quiz={
                "title": title,
                "description": description,
                "quiz_type": "graded_survey",
                "published": published,
                "points_possible": config.get("points_possible", 1),
                "allowed_attempts": 1,
                "assignment_group_id": group.id,
                "only_visible_to_overrides": True,
            }
        )
        for payload in question_payloads:
            quiz.create_question(question=payload)
        # Classic Quiz marks questions added through its API as unsaved until a
        # final quiz update persists the question set for students.
        quiz.edit(quiz={"published": published})
        log.info("Created peer-review survey: %s", _quiz_url(canvas_course, quiz))
        assignment = canvas_course.course.get_assignment(int(quiz.assignment_id))
        assignment.edit(
            assignment={
                "published": published,
                "points_possible": config.get("points_possible", 1),
                "only_visible_to_overrides": True,
            }
        )
        assigned_reviewers = [
            reviewer_id
            for reviewer_id, assigned_documents in assignments.items()
            if document_index in assigned_documents
        ]
        assignment.create_override(
            assignment_override={
                "student_ids": assigned_reviewers,
                "unlock_at": config["unlock_at"],
                "due_at": config["due_at"],
                "lock_at": config["lock_at"],
            }
        )
    return plan


def _log_plan(plan: PeerReviewPlan, *, dry_run: bool) -> None:
    log.info(
        "Peer-review plan (%s): %d PDFs, %d reviewers.",
        "dry run" if dry_run else "creating",
        len(plan.documents),
        len(plan.reviewer_ids),
    )
    for index, document in enumerate(plan.documents):
        reviewers = [
            reviewer_id
            for reviewer_id, assigned in plan.assignments.items()
            if index in assigned
        ]
        log.info(
            "  %s [%s]: %d reviewers (%s)",
            document.filename,
            document.short_digest,
            len(reviewers),
            ", ".join(map(str, reviewers)),
        )
    for warning in plan.warnings:
        log.warning(warning)
