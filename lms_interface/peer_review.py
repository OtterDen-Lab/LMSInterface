"""Create Canvas graded-survey peer reviews from PDFs and a YAML rubric."""

from __future__ import annotations

import hashlib
import logging
import random
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

from lms_interface.canvas_interface import CanvasCourse

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
