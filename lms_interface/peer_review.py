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
    if source_assignment_id is not None:
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
