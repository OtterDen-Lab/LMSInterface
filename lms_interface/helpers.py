#!env python

import argparse
import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List
from urllib.parse import urlparse

import canvasapi
import requests

from lms_interface.cleanup_missing_ui import (
    AssignmentCleanupSummary,
    CleanupMissingReporter,
)
from lms_interface.canvas_interface import (
    CanvasAssignment,
    CanvasCourse,
    CanvasInterface,
)
from lms_interface.quiz_reports import (
    convert_likert_values,
    download_quiz_report_csv,
    quiz_likert_values,
    wait_for_quiz_report,
)

# Configure logging to actually output
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
# Keep Canvas HTTP request/response chatter off unless explicitly debugging.
logging.getLogger("canvasapi.requester").setLevel(logging.WARNING)
logging.getLogger("canvasapi").setLevel(logging.WARNING)


def delete_empty_folders(canvas_course: CanvasCourse):
    log.info("delete_empty_folders")
    num_processed = 0
    for f in canvas_course.get_folders():
        try:
            f.delete()
            log.info(f'({num_processed}) : "{f}" deleted')
        except canvasapi.exceptions.BadRequest:
            log.info(
                f'({num_processed}) : "{f}" (not deleted) ({len(list(f.get_files()))})'
            )
        num_processed += 1


def get_quiz_results(
    canvas_course: CanvasCourse,
    quiz_id: int,
    output_path: Path | None = None,
) -> int:
    """Generate, download, and print a normalized Canvas student-analysis report."""
    quiz = canvas_course.course.get_quiz(quiz_id)
    report = quiz.create_report("student_analysis")
    report = wait_for_quiz_report(quiz, report)
    csv_text = download_quiz_report_csv(canvas_course, report)
    normalized_csv = convert_likert_values(csv_text, quiz_likert_values(quiz))

    if output_path is None:
        print(normalized_csv, end="")
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(normalized_csv)
    return sum(1 for _ in csv.DictReader(io.StringIO(normalized_csv)))


def _safe_attachment_filename(filename: Any) -> str:
    """Return a filename that cannot escape a submission's output directory."""
    name = Path(str(filename or "submission_file")).name.replace("\x00", "").strip()
    return name if name not in {"", ".", ".."} else "submission_file"


def _attachment_download_url_for_log(url: str) -> str:
    """Describe an attachment URL without logging its potentially signed query string."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _submission_attachments(submission: Any) -> list[dict[str, Any]]:
    """Get the latest attempt's attachments from a Canvas submission response."""
    history = getattr(submission, "submission_history", None)
    if isinstance(history, list) and history:
        latest = history[-1]
        if isinstance(latest, dict):
            attachments = latest.get("attachments")
            if isinstance(attachments, list):
                return [item for item in attachments if isinstance(item, dict)]

    attachments = getattr(submission, "attachments", None)
    if isinstance(attachments, list):
        return [item for item in attachments if isinstance(item, dict)]
    return []


def download_assignment_submissions(
    canvas_course: CanvasCourse,
    assignment_id: int,
    output_dir: Path,
    *,
    overwrite: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Download latest file attachments for an assignment, grouped by Canvas user ID.

    A successful course and assignment lookup confirms that the configured key can
    use the Canvas API. Individual attachment failures are recorded separately,
    which makes expired URLs or file-specific authorization problems visible.
    """
    try:
        assignment = canvas_course.get_assignment(assignment_id)
    except (canvasapi.exceptions.CanvasException, requests.RequestException) as exc:
        raise RuntimeError(
            "Could not retrieve the assignment from the Canvas API before downloading "
            "files. Verify the course ID and that the configured API key has access."
        ) from exc
    if assignment is None:
        raise ValueError(f"Assignment {assignment_id} was not found or is unavailable.")

    output_dir.mkdir(parents=True, exist_ok=True)
    stats = {
        "submissions_checked": 0,
        "attachments_found": 0,
        "downloaded": 0,
        "skipped_existing": 0,
        "failed": 0,
    }
    headers = {"Authorization": f"Bearer {canvas_course.canvas_interface.canvas_key}"}
    try:
        submissions = assignment.assignment.get_submissions(include=["submission_history"])
    except (canvasapi.exceptions.CanvasException, requests.RequestException) as exc:
        raise RuntimeError(
            "Could not list assignment submissions from the Canvas API. "
            "The API key may not have permission to view student submissions."
        ) from exc

    for submission in submissions:
        if limit is not None and stats["submissions_checked"] >= limit:
            break
        stats["submissions_checked"] += 1
        user_id = getattr(submission, "user_id", None)
        if user_id is None:
            log.warning("Skipping a submission without a Canvas user ID.")
            continue

        attachments = _submission_attachments(submission)
        stats["attachments_found"] += len(attachments)
        for attachment in attachments:
            filename = _safe_attachment_filename(attachment.get("filename"))
            url = attachment.get("url")
            if not isinstance(url, str) or not url:
                stats["failed"] += 1
                log.error("user=%s file=%s has no downloadable Canvas URL", user_id, filename)
                continue

            user_dir = output_dir / str(user_id)
            destination = user_dir / filename
            if destination.exists() and not overwrite:
                stats["skipped_existing"] += 1
                log.info("Skipping existing file user=%s path=%s", user_id, destination)
                continue
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=30,
                    allow_redirects=True,
                )
                response.raise_for_status()
                user_dir.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as file_handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            file_handle.write(chunk)
            except requests.RequestException as exc:
                stats["failed"] += 1
                status = getattr(getattr(exc, "response", None), "status_code", None)
                status_detail = f" status={status}" if status is not None else ""
                log.error(
                    "Download failed user=%s file=%s%s url=%s error=%s",
                    user_id,
                    filename,
                    status_detail,
                    _attachment_download_url_for_log(url),
                    exc,
                )
                continue
            except OSError as exc:
                stats["failed"] += 1
                log.error("Could not save user=%s file=%s: %s", user_id, filename, exc)
                continue

            stats["downloaded"] += 1
            log.info("Downloaded user=%s file=%s -> %s", user_id, filename, destination)

    return stats


def get_closed_assignments(
    interface: CanvasCourse,
) -> List[canvasapi.assignment.Assignment]:
    closed_assignments: List[canvasapi.assignment.Assignment] = []
    for assignment in interface.get_assignments(include=["all_dates"], order_by="name"):
        if not assignment.published:
            continue
        if assignment.lock_at is not None:
            # Then it's the easy case because there's no overrides
            if datetime.fromisoformat(assignment.lock_at) < datetime.now(timezone.utc):
                # Then the assignment is past due
                closed_assignments.append(assignment)
                continue
        elif assignment.all_dates is not None:

            # First we need to figure out what the latest time this assignment could be available is
            # todo: This could be done on a per-student basis
            last_lock_datetime = None
            for dates_dict in assignment.all_dates:
                if dates_dict["lock_at"] is not None:
                    lock_datetime = datetime.fromisoformat(dates_dict["lock_at"])
                    if (last_lock_datetime is None) or (
                        lock_datetime >= last_lock_datetime
                    ):
                        last_lock_datetime = lock_datetime

            # If we have found a valid lock time, and it's in the past then we lock
            if last_lock_datetime is not None and last_lock_datetime <= datetime.now(
                timezone.utc
            ):
                closed_assignments.append(assignment)
                continue

        else:
            log.warning(f"Cannot find any lock dates for assignment {assignment.name}!")

    return closed_assignments


def get_unsubmitted_submissions(
    interface: CanvasCourse, assignment: canvasapi.assignment.Assignment
) -> List[canvasapi.submission.Submission]:
    submissions: List[canvasapi.submission.Submission] = list(
        filter(
            lambda s: s.submitted_at is None
            and s.percentage_score is None
            and not s.excused,
            assignment.get_submissions(),
        )
    )
    return submissions


def clear_out_missing(interface: CanvasCourse):
    assignments = get_closed_assignments(interface)
    for assignment in assignments:
        missing_submissions = get_unsubmitted_submissions(interface, assignment)
        if not missing_submissions:
            continue
        log.info(
            f"Assignment: ({assignment.quiz_id if hasattr(assignment, 'quiz_id') else assignment.id}) {assignment.name} {assignment.published}"
        )
        for submission in missing_submissions:
            log.info(
                f"{submission.user_id} ({interface.get_username(submission.user_id)}) : {submission.workflow_state} : {submission.missing} : {submission.score} : {submission.grader_id} : {submission.graded_at}"
            )
            submission.edit(submission={"late_policy_status": "missing"})
        log.info("")


def _parse_canvas_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_submission_due_at(
    assignment: canvasapi.assignment.Assignment,
    submission: canvasapi.submission.Submission,
) -> datetime | None:
    # Most specific: submission-specific cached due date from Canvas.
    for value in (
        getattr(submission, "cached_due_date", None),
        getattr(submission, "due_at", None),
    ):
        parsed = _parse_canvas_datetime(value)
        if parsed is not None:
            return parsed

    # Sometimes the submission includes nested assignment metadata.
    submission_assignment = getattr(submission, "assignment", None)
    if isinstance(submission_assignment, dict):
        parsed = _parse_canvas_datetime(submission_assignment.get("due_at"))
        if parsed is not None:
            return parsed
    elif submission_assignment is not None:
        parsed = _parse_canvas_datetime(getattr(submission_assignment, "due_at", None))
        if parsed is not None:
            return parsed

    # Fallback to assignment-level due date.
    return _parse_canvas_datetime(getattr(assignment, "due_at", None))


def _submission_content_signals(
    submission: canvasapi.submission.Submission,
) -> list[str]:
    signals: list[str] = []

    submission_type = (
        str(getattr(submission, "submission_type", "") or "").strip().lower()
    )
    if submission_type and submission_type not in {"none", "on_paper", "not_graded"}:
        signals.append(f"submission_type={submission_type}")

    attachments = getattr(submission, "attachments", None)
    if isinstance(attachments, list) and len(attachments) > 0:
        signals.append(f"attachments={len(attachments)}")

    body = getattr(submission, "body", None)
    if isinstance(body, str) and body.strip():
        signals.append("body")

    url = getattr(submission, "url", None)
    if isinstance(url, str) and url.strip():
        signals.append("url")

    media_comment_id = getattr(submission, "media_comment_id", None)
    if media_comment_id not in (None, "", 0):
        signals.append("media_comment")

    return signals


def _normalize_late_policy_status(value: Any) -> str:
    if value is None:
        return "none"
    return str(value).strip().lower()


def _normalize_grade(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _placeholder_grade_needs_clear(submission: canvasapi.submission.Submission) -> bool:
    grade = _normalize_grade(getattr(submission, "grade", None))
    posted_grade = _normalize_grade(getattr(submission, "posted_grade", None))
    entered_grade = _normalize_grade(getattr(submission, "entered_grade", None))
    score = getattr(submission, "score", None)

    grade_tokens = {grade, posted_grade, entered_grade}
    if any(token in {"incomplete", "fail", "f"} for token in grade_tokens):
        return True
    if any(token in {"0", "0.0", "0.00"} for token in grade_tokens):
        return True

    try:
        if score is not None and float(score) == 0.0:
            return True
    except Exception:
        pass

    return False


def _submission_has_non_placeholder_grade(
    submission: canvasapi.submission.Submission,
) -> bool:
    grade = _normalize_grade(getattr(submission, "grade", None))
    posted_grade = _normalize_grade(getattr(submission, "posted_grade", None))
    entered_grade = _normalize_grade(getattr(submission, "entered_grade", None))
    score = getattr(submission, "score", None)

    grade_tokens = {grade, posted_grade, entered_grade}
    if any(
        token
        and token not in {"incomplete", "fail", "f", "0", "0.0", "0.00"}
        for token in grade_tokens
    ):
        return True

    try:
        if score is not None:
            return float(score) != 0.0
    except Exception:
        pass

    return False


def cleanup_missing_by_due_date(
    canvas_course: CanvasCourse,
    *,
    dry_run: bool = False,
    include_unpublished: bool = False,
    assignment_id: int | None = None,
    limit: int | None = None,
    force_clear_stale_missing: bool = True,
    clear_placeholder_grade: bool = False,
    now: datetime | None = None,
    reporter: CleanupMissingReporter | None = None,
) -> dict[str, int]:
    """
    Normalize late policy status for *unsubmitted* work:
      - Due date passed  -> `missing`
      - Due date future  -> `none`

    Uses Assignment.get_submission(user_id) for each student/assignment pair.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    stats = {
        "assignments_considered": 0,
        "students_available": 0,
        "student_limit": limit if limit is not None else 0,
        "students_considered": 0,
        "submissions_checked": 0,
        "unsubmitted_considered": 0,
        "desired_missing": 0,
        "desired_none": 0,
        "current_missing": 0,
        "current_none": 0,
        "current_other": 0,
        "missing_flag_true": 0,
        "missing_flag_false": 0,
        "submitted_with_content": 0,
        "skipped_existing_grade": 0,
        "placeholder_without_content": 0,
        "placeholder_grade_needs_clear": 0,
        "placeholder_grade_clear_attempted": 0,
        "placeholder_grade_clear_succeeded": 0,
        "placeholder_grade_clear_failed": 0,
        "updated_to_missing": 0,
        "updated_to_none": 0,
        "forced_none_due_stale_missing_flag": 0,
        "unchanged": 0,
        "unchanged_missing": 0,
        "unchanged_none": 0,
        "unchanged_other": 0,
        "skipped_excused": 0,
        "skipped_submitted": 0,
        "skipped_no_due_date": 0,
        "errors": 0,
    }

    students = list(canvas_course.get_students(include_names=False))
    stats["students_available"] = len(students)
    if limit is not None:
        students = students[:limit]
    assignments = [
        assignment
        for assignment in canvas_course.get_assignments(
            include=["all_dates"], order_by="name"
        )
        if (assignment_id is None or assignment.id == assignment_id)
        and (include_unpublished or getattr(assignment, "published", True))
    ]

    total_assignments = len(assignments)
    if reporter is not None:
        reporter.start(
            total_assignments=total_assignments,
            total_students=stats["students_available"],
            student_limit=stats["student_limit"],
        )

    for assignment_index, assignment in enumerate(assignments, start=1):
        stats["assignments_considered"] += 1
        assignment_updates_to_missing = 0
        assignment_updates_to_none = 0
        assignment_unchanged = 0
        assignment_skipped_excused = 0
        assignment_skipped_submitted = 0
        assignment_skipped_existing_grade = 0
        assignment_skipped_no_due_date = 0
        assignment_errors = 0
        assignment_unsubmitted = 0

        assignment_name = getattr(assignment, "name", f"assignment_{assignment.id}")
        if reporter is not None:
            reporter.set_current_assignment(
                index=assignment_index,
                total=total_assignments,
                assignment_id=assignment.id,
                assignment_name=assignment_name,
            )
        else:
            log.info(
                f"[{assignment_index}/{total_assignments}] Checking assignment "
                f"{assignment.id} ({assignment_name})"
            )

        for student in students:
            user_id = student.user_id
            stats["students_considered"] += 1
            try:
                submission = assignment.get_submission(user_id)
            except Exception as e:
                stats["errors"] += 1
                assignment_errors += 1
                log.warning(
                    f"Skipping assignment={assignment.id} user={user_id}: could not fetch submission ({e})"
                )
                continue

            stats["submissions_checked"] += 1

            if bool(getattr(submission, "excused", False)):
                stats["skipped_excused"] += 1
                assignment_skipped_excused += 1
                continue

            content_signals = _submission_content_signals(submission)
            if content_signals:
                stats["skipped_submitted"] += 1
                stats["submitted_with_content"] += 1
                assignment_skipped_submitted += 1
                continue

            if _submission_has_non_placeholder_grade(submission):
                stats["skipped_existing_grade"] += 1
                assignment_skipped_existing_grade += 1
                log.debug(
                    f"Skipping assignment={assignment.id} user={user_id}: "
                    "submission already has a non-placeholder grade"
                )
                continue

            workflow_state = (
                str(getattr(submission, "workflow_state", "") or "").strip().lower()
            )
            submitted_at = getattr(submission, "submitted_at", None)
            if (
                workflow_state in {"submitted", "graded", "pending_review"}
                or submitted_at is not None
            ):
                stats["placeholder_without_content"] += 1
                log.debug(
                    f"Treating placeholder as unsubmitted for assignment={assignment.id} user={user_id}: "
                    f"workflow_state={workflow_state!r}, submitted_at={submitted_at!r}, "
                    f"submission_type={getattr(submission, 'submission_type', None)!r}"
                )

            stats["unsubmitted_considered"] += 1
            assignment_unsubmitted += 1

            due_at = _resolve_submission_due_at(assignment, submission)
            if due_at is None:
                stats["skipped_no_due_date"] += 1
                assignment_skipped_no_due_date += 1
                continue

            desired_status = "missing" if due_at <= now else "none"
            if desired_status == "missing":
                stats["desired_missing"] += 1
            else:
                stats["desired_none"] += 1

            current_status = _normalize_late_policy_status(
                getattr(submission, "late_policy_status", None)
            )
            missing_flag = bool(getattr(submission, "missing", False))
            if missing_flag:
                stats["missing_flag_true"] += 1
            else:
                stats["missing_flag_false"] += 1
            if current_status == "missing":
                stats["current_missing"] += 1
            elif current_status == "none":
                stats["current_none"] += 1
            else:
                stats["current_other"] += 1

            # Canvas can report stale `missing=True` even when late_policy_status is already
            # `none`; writing `none` again is a cheap nudge to recompute server-side state.
            if (
                force_clear_stale_missing
                and desired_status == "none"
                and current_status == "none"
                and missing_flag
            ):
                log.debug(
                    f"assignment={assignment.id} user={user_id} "
                    f"forcing late_policy_status none (stale missing flag true)"
                )
                if not dry_run:
                    submission.edit(submission={"late_policy_status": "none"})
                stats["updated_to_none"] += 1
                stats["forced_none_due_stale_missing_flag"] += 1
                assignment_updates_to_none += 1
                continue

            placeholder_grade_needs_clear = (
                desired_status == "none"
                and _placeholder_grade_needs_clear(submission)
            )
            if placeholder_grade_needs_clear:
                stats["placeholder_grade_needs_clear"] += 1

            if clear_placeholder_grade and placeholder_grade_needs_clear:
                stats["placeholder_grade_clear_attempted"] += 1
                log.debug(
                    f"assignment={assignment.id} user={user_id} "
                    f"clearing placeholder grade "
                    f"(grade={getattr(submission, 'grade', None)!r}, "
                    f"posted_grade={getattr(submission, 'posted_grade', None)!r}, "
                    f"entered_grade={getattr(submission, 'entered_grade', None)!r}, "
                    f"score={getattr(submission, 'score', None)!r})"
                )
                if not dry_run:
                    cleared = False
                    for payload in ("", None):
                        try:
                            submission.edit(
                                submission={
                                    "posted_grade": payload,
                                    "late_policy_status": "none",
                                }
                            )
                            cleared = True
                            break
                        except Exception as e:
                            log.warning(
                                f"assignment={assignment.id} user={user_id} "
                                f"failed clearing placeholder grade with payload={payload!r}: {e}"
                            )
                    if cleared:
                        stats["placeholder_grade_clear_succeeded"] += 1
                    else:
                        stats["placeholder_grade_clear_failed"] += 1
                        stats["errors"] += 1
                else:
                    stats["placeholder_grade_clear_succeeded"] += 1
                continue

            if current_status == desired_status:
                stats["unchanged"] += 1
                assignment_unchanged += 1
                if current_status == "missing":
                    stats["unchanged_missing"] += 1
                elif current_status == "none":
                    stats["unchanged_none"] += 1
                else:
                    stats["unchanged_other"] += 1
                continue

            log.debug(
                f"assignment={assignment.id} user={user_id} "
                f"due_at={due_at.isoformat()} now={now.isoformat()} "
                f"late_policy_status: {current_status} -> {desired_status}"
            )
            if not dry_run:
                submission.edit(submission={"late_policy_status": desired_status})

            if desired_status == "missing":
                stats["updated_to_missing"] += 1
                assignment_updates_to_missing += 1
            else:
                stats["updated_to_none"] += 1
                assignment_updates_to_none += 1

        summary = AssignmentCleanupSummary(
            index=assignment_index,
            total=total_assignments,
            assignment_id=assignment.id,
            assignment_name=assignment_name,
            unsubmitted=assignment_unsubmitted,
            updated_to_missing=assignment_updates_to_missing,
            updated_to_none=assignment_updates_to_none,
            unchanged=assignment_unchanged,
            skipped_excused=assignment_skipped_excused,
            skipped_submitted=assignment_skipped_submitted,
            skipped_existing_grade=assignment_skipped_existing_grade,
            skipped_no_due_date=assignment_skipped_no_due_date,
            errors=assignment_errors,
        )
        if reporter is not None:
            reporter.add_assignment_summary(summary)
        else:
            log.info(
                f"[{assignment_index}/{total_assignments}] assignment={assignment.id} summary: "
                f"unsubmitted={assignment_unsubmitted}, "
                f"updated_to_missing={assignment_updates_to_missing}, "
                f"updated_to_none={assignment_updates_to_none}, "
                f"unchanged={assignment_unchanged}, "
                f"skipped_excused={assignment_skipped_excused}, "
                f"skipped_submitted={assignment_skipped_submitted}, "
                f"skipped_existing_grade={assignment_skipped_existing_grade}, "
                f"skipped_no_due_date={assignment_skipped_no_due_date}, "
                f"errors={assignment_errors}"
            )

    if stats["placeholder_grade_needs_clear"] > 0 and not clear_placeholder_grade:
        log.warning(
            f"Detected {stats['placeholder_grade_needs_clear']} future-due "
            "placeholder submissions with stale grade values (e.g., 0/Incomplete). "
            "Re-run with --clear-placeholder-grade to clear those grades."
        )

    if reporter is not None:
        reporter.finish(stats)

    return stats


def deprecate_assignment(
    canvas_course: CanvasCourse, assignment_id
) -> List[canvasapi.assignment.Assignment]:

    log.debug(canvas_course.__dict__)

    # for assignment in canvas_course.get_assignments():
    #   print(assignment)

    canvas_assignment: CanvasAssignment = canvas_course.get_assignment(
        assignment_id=assignment_id
    )

    canvas_assignment.assignment.edit(
        assignment={
            "name": f"{canvas_assignment.assignment.name} (deprecated)",
            "due_at": f"{datetime.now(timezone.utc).isoformat()}",
            "lock_at": f"{datetime.now(timezone.utc).isoformat()}",
        }
    )


def mark_future_assignments_as_ungraded(canvas_course: CanvasCourse):

    for assignment in canvas_course.get_assignments(
        include=["all_dates"], order_by="name"
    ):
        if assignment.unlock_at is not None:
            if datetime.fromisoformat(assignment.unlock_at) > datetime.now(
                timezone.utc
            ):
                log.debug(assignment)
                for submission in assignment.get_submissions():
                    submission.mark_unread()


def main():

    # Mapping of short CLI names to helper functions
    HELPERS = {
        "clean-folders": ("delete_empty_folders", False),
        "closed": ("get_closed_assignments", False),
        "unsubmitted": ("get_unsubmitted_submissions", True),
        "missing": ("clear_out_missing", False),
        "cleanup-missing": ("cleanup_missing_by_due_date", False),
        "deprecate": ("deprecate_assignment", True),
        "ungraded": ("mark_future_assignments_as_ungraded", False),
        "plan-course": ("plan_course", False),
        "peer-review": ("peer_review", False),
        "peer-review-summary": ("peer_review_summary", False),
        "peer-review-workspace": ("peer_review_workspace", False),
        "peer-review-finalize": ("peer_review_finalize", False),
        "peer-review-setup": ("peer_review_setup", False),
        "peer-review-complete": ("peer_review_complete", False),
        "peer-review-push": ("peer_review_push", False),
        "get-quiz-results": ("get_quiz_results", False),
        "download-submissions": ("download_assignment_submissions", True),
    }

    parser = argparse.ArgumentParser(
        description="Canvas helper utilities for common course management tasks"
    )

    parser.add_argument("helper", choices=HELPERS.keys(), help="Helper function to run")

    parser.add_argument("--course-id", type=int, help="Canvas course ID")

    parser.add_argument(
        "--assignment-id",
        type=int,
        help="Canvas assignment ID (or a Classic Quiz ID for peer-review-summary)",
    )
    parser.add_argument(
        "--quiz-id",
        type=int,
        help="Classic Canvas quiz ID that selects a peer-review-summary batch",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit processing to first N students (cleanup-missing or download-submissions)",
    )

    parser.add_argument(
        "--prod",
        action="store_true",
        help="Use production Canvas instance instead of development",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report updates without writing changes to Canvas",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Write peer-review grades/comments to Canvas (peer-review-push only)",
    )
    parser.add_argument(
        "--grades-only",
        action="store_true",
        help="Update peer-review grades without adding duplicate feedback comments",
    )
    parser.add_argument(
        "--include-unpublished",
        action="store_true",
        help="Also process unpublished assignments",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable verbose per-student debug logging"
    )
    parser.add_argument(
        "--no-force-clear-stale-missing",
        action="store_true",
        help="Do not force-write late_policy_status=none when Canvas reports missing=True with future due date",
    )
    parser.add_argument(
        "--clear-placeholder-grade",
        action="store_true",
        help="Attempt to clear stale placeholder grades (e.g., Incomplete/0) on future-due, no-content submissions",
    )
    parser.add_argument(
        "--plain-output",
        action="store_true",
        help="Disable the live terminal UI and print plain text summaries instead",
    )
    parser.add_argument(
        "--yaml-path",
        help="Path to YAML file (required for plan-course, peer-review, and workspace summaries)",
    )
    parser.add_argument(
        "--source-assignment-id",
        type=int,
        help="Use PDF attachments from this Canvas assignment (peer-review commands)",
    )
    parser.add_argument(
        "--assignment-group-id",
        type=int,
        help="Canvas assignment group ID (peer-review-summary)",
    )
    parser.add_argument(
        "--assignment-group-name",
        help="Exact Canvas assignment group name (peer-review-summary)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path (peer-review-summary or get-quiz-results)",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        help="Directory for downloaded submission attachments (download-submissions)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace matching files in --download-dir (download-submissions only)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Create or finalize a peer-review grading workspace in this directory",
    )
    parser.add_argument("--google-client-secret", type=Path)
    parser.add_argument(
        "--google-token-path",
        type=Path,
        default=Path.home() / ".tokens" / "lms-interface-google-token.json",
    )
    parser.add_argument("--google-oauth-port", type=int, default=8765)
    parser.add_argument(
        "--replace-google-forms",
        action="store_true",
        help="Trash the prior Google Forms/PDF folder and create a fresh batch",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="Deterministic allocation seed (peer-review)"
    )
    parser.add_argument(
        "--output-dir",
        default="build/course_plan",
        help="Directory for generated calendar outputs (plan-course)",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish generated schedule page to Canvas (plan-course)",
    )
    parser.add_argument(
        "--page-title",
        help="Override Canvas schedule page title when publishing (plan-course)",
    )
    parser.add_argument(
        "--module-name",
        default="Course Schedule",
        help="Canvas module name for schedule page link when publishing (plan-course)",
    )
    parser.add_argument(
        "--publish-weekly-slides",
        action="store_true",
        help="Create/reuse Week modules and publish weekly slide URL links (plan-course)",
    )
    parser.add_argument(
        "--publish-attendance",
        action="store_true",
        help="Create/update attendance check-in quizzes with per-section assign-to windows (plan-course)",
    )
    parser.add_argument(
        "--publish-assignments",
        action="store_true",
        help="Create/update generated course assignments (study notes/programming) from YAML rules (plan-course)",
    )
    parser.add_argument(
        "--weekly-module-template",
        help="Template for weekly module names, e.g. 'Week {week_number}' (plan-course)",
    )

    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)
        # Re-enable low-level Canvas request logs when debugging.
        logging.getLogger("canvasapi.requester").setLevel(logging.INFO)
        logging.getLogger("canvasapi").setLevel(logging.INFO)

    # Get helper function name and whether it requires assignment_id
    helper_func_name, requires_assignment = HELPERS[args.helper]

    # Validate assignment_id requirement
    if requires_assignment and not args.assignment_id:
        parser.error(f"--assignment-id is required for '{args.helper}'")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.limit is not None and args.helper not in {
        "cleanup-missing",
        "download-submissions",
    }:
        parser.error("--limit is only supported by cleanup-missing and download-submissions")
    if helper_func_name == "download_assignment_submissions" and args.download_dir is None:
        parser.error("--download-dir is required for 'download-submissions'")
    if args.download_dir is not None and helper_func_name != "download_assignment_submissions":
        parser.error("--download-dir is only supported by download-submissions")
    if args.overwrite and helper_func_name != "download_assignment_submissions":
        parser.error("--overwrite is only supported by download-submissions")
    workspace_summary = (
        helper_func_name in {"peer_review_summary", "peer_review_workspace", "peer_review_setup"}
        and args.work_dir is not None
    )
    if helper_func_name in {"plan_course", "peer_review"} and not args.yaml_path:
        parser.error(f"--yaml-path is required for '{args.helper}'")
    if workspace_summary:
        if not args.yaml_path:
            parser.error(f"--yaml-path is required when using --work-dir")
        if args.source_assignment_id is None:
            parser.error("--source-assignment-id is required when using --work-dir")
        if args.output is not None:
            parser.error("--output cannot be combined with --work-dir")
    if helper_func_name == "peer_review_workspace" and args.work_dir is None:
        parser.error(
            "'peer-review-workspace' is an alias for 'peer-review-summary --work-dir'; "
            "specify --work-dir."
        )
    if (
        helper_func_name in {"peer_review_finalize", "peer_review_push", "peer_review_setup", "peer_review_complete"}
        and args.work_dir is None
    ):
        parser.error(f"--work-dir is required for '{args.helper}'")
    if args.work_dir is not None and helper_func_name not in {
        "peer_review_summary",
        "peer_review_workspace",
        "peer_review_finalize",
        "peer_review_push",
        "peer_review_setup",
        "peer_review_complete",
    }:
        parser.error("--work-dir is only supported by peer-review workspace commands")
    if helper_func_name == "get_quiz_results" and args.quiz_id is None:
        parser.error("--quiz-id is required for 'get-quiz-results'")
    if helper_func_name == "peer_review_push" and args.dry_run and args.push:
        parser.error("--dry-run cannot be combined with --push")
    if helper_func_name in {"peer_review_setup", "peer_review_complete"} and args.google_client_secret is None:
        parser.error("--google-client-secret is required for peer-review Google workflow")
    if args.replace_google_forms and helper_func_name != "peer_review_setup":
        parser.error("--replace-google-forms is only supported by peer-review-setup")
    if args.grades_only and helper_func_name != "peer_review_push":
        parser.error("--grades-only is only supported by peer-review-push")
    if args.grades_only and not args.push:
        parser.error("--grades-only requires --push")
    if (
        helper_func_name == "plan_course"
        and (
            args.publish_weekly_slides
            or args.publish_attendance
            or args.publish_assignments
        )
        and not args.publish
    ):
        parser.error(
            "--publish is required when using --publish-weekly-slides, --publish-attendance, or --publish-assignments"
        )

    needs_canvas_course = helper_func_name not in {
        "plan_course",
        "peer_review_finalize",
        "peer_review_complete",
    } or args.publish
    canvas_course = None
    if needs_canvas_course:
        if args.course_id is None:
            parser.error("--course-id is required for this command")
        canvas_interface = CanvasInterface(prod=args.prod)
        canvas_course = canvas_interface.get_course(args.course_id)

    # Run the requested helper
    if helper_func_name == "delete_empty_folders":
        delete_empty_folders(canvas_course)

    elif helper_func_name == "get_closed_assignments":
        assignments = get_closed_assignments(canvas_course)
        log.info(f"Found {len(assignments)} closed assignments:")
        for assignment in assignments:
            log.info(f"  - {assignment.name} (ID: {assignment.id})")

    elif helper_func_name == "get_unsubmitted_submissions":
        assignment = canvas_course.get_assignment(args.assignment_id)
        submissions = get_unsubmitted_submissions(canvas_course, assignment.assignment)
        log.info(f"Found {len(submissions)} unsubmitted submissions:")
        for submission in submissions:
            log.info(f"  - User {submission.user_id}")

    elif helper_func_name == "clear_out_missing":
        clear_out_missing(canvas_course)

    elif helper_func_name == "cleanup_missing_by_due_date":
        reporter = CleanupMissingReporter(
            live=False if args.plain_output else None,
        )
        try:
            cleanup_missing_by_due_date(
                canvas_course,
                dry_run=args.dry_run,
                include_unpublished=args.include_unpublished,
                assignment_id=args.assignment_id,
                limit=args.limit,
                force_clear_stale_missing=not args.no_force_clear_stale_missing,
                clear_placeholder_grade=args.clear_placeholder_grade,
                reporter=reporter,
            )
        finally:
            reporter.close()

    elif helper_func_name == "deprecate_assignment":
        deprecate_assignment(canvas_course, args.assignment_id)
        log.info(f"Assignment {args.assignment_id} has been deprecated")

    elif helper_func_name == "mark_future_assignments_as_ungraded":
        mark_future_assignments_as_ungraded(canvas_course)

    elif helper_func_name == "peer_review":
        from lms_interface.peer_review import create_peer_reviews

        create_peer_reviews(
            canvas_course,
            config_path=Path(args.yaml_path),
            source_assignment_id=args.source_assignment_id,
            seed=args.seed,
            dry_run=args.dry_run,
        )

    elif helper_func_name in {"peer_review_summary", "peer_review_workspace"}:
        from lms_interface.peer_review import (
            PeerReviewSummaryError,
            create_peer_review_workspace,
            export_peer_review_summary,
        )

        try:
            if args.work_dir is not None:
                work_dir = create_peer_review_workspace(
                    canvas_course,
                    work_dir=args.work_dir,
                    course_id=args.course_id,
                    assignment_id=args.assignment_id,
                    quiz_id=args.quiz_id,
                    assignment_group_id=args.assignment_group_id,
                    assignment_group_name=args.assignment_group_name,
                    source_assignment_id=args.source_assignment_id,
                    rubric_config_path=Path(args.yaml_path),
                    seed=args.seed,
                )
                log.info("Created peer-review grading workspace at %s", work_dir)
            else:
                row_count = export_peer_review_summary(
                    canvas_course,
                    output_path=args.output or Path("peer-review-results.csv"),
                    assignment_id=args.assignment_id,
                    quiz_id=args.quiz_id,
                    assignment_group_id=args.assignment_group_id,
                    assignment_group_name=args.assignment_group_name,
                    source_assignment_id=args.source_assignment_id,
                    rubric_config_path=Path(args.yaml_path) if args.yaml_path else None,
                )
                log.info(
                    "Exported %d peer-review response rows to %s",
                    row_count,
                    args.output or Path("peer-review-results.csv"),
                )
        except PeerReviewSummaryError as exc:
            parser.error(str(exc))

    elif helper_func_name == "peer_review_finalize":
        from lms_interface.peer_review import (
            PeerReviewSummaryError,
            finalize_peer_review_workspace,
        )

        try:
            output_paths = finalize_peer_review_workspace(args.work_dir)
        except PeerReviewSummaryError as exc:
            parser.error(str(exc))
        log.info("Finalized peer-review workspace at %s", args.work_dir)
        for name, path in output_paths.items():
            log.info("  %s: %s", name, path)

    elif helper_func_name == "peer_review_setup":
        import json

        from lms_interface.google_peer_review import create_forms
        from lms_interface.peer_review import create_peer_review_workspace
        workspace_config = args.work_dir / "peer-review-workspace.yaml"
        if workspace_config.exists():
            work_dir = args.work_dir
            log.info("Reusing existing peer-review workspace at %s", work_dir)
        else:
            work_dir = create_peer_review_workspace(canvas_course, work_dir=args.work_dir, course_id=args.course_id, assignment_id=args.assignment_id, quiz_id=args.quiz_id, assignment_group_id=args.assignment_group_id, assignment_group_name=args.assignment_group_name, source_assignment_id=args.source_assignment_id, rubric_config_path=Path(args.yaml_path), seed=args.seed)
        manifest = create_forms(
            work_dir,
            args.google_client_secret,
            args.google_token_path,
            args.google_oauth_port,
            replace=args.replace_google_forms,
        )
        manifest_data = json.loads(manifest.read_text())
        log.info("Peer-review setup complete; Drive folder: %s", manifest_data["folder"].get("webViewLink", ""))
        for entry in manifest_data["forms"]:
            log.info("  %s: %s", entry["pdf_id"], entry.get("responder_uri", ""))

    elif helper_func_name == "peer_review_complete":
        from lms_interface.google_peer_review import import_forms
        from lms_interface.peer_review import finalize_peer_review_workspace
        imported = import_forms(args.work_dir, args.google_client_secret, args.google_token_path, args.google_oauth_port)
        finalize_peer_review_workspace(args.work_dir)
        log.info("Imported %d Forms and finalized peer-review workspace", imported)

    elif helper_func_name == "peer_review_push":
        from lms_interface.peer_review import PeerReviewSummaryError, push_peer_review_workspace

        try:
            result = push_peer_review_workspace(
                canvas_course,
                args.work_dir,
                push=args.push,
                include_comments=not args.grades_only,
            )
        except PeerReviewSummaryError as exc:
            parser.error(str(exc))
        action = "Pushed" if args.push else "Dry run: would push"
        log.info(
            "%s %d paper grades/comments and %d participation grades%s.",
            action,
            result["paper_grades_and_comments"],
            result["participation_grades"],
            f" ({result['written']} Canvas updates written)" if args.push else "",
        )

    elif helper_func_name == "get_quiz_results":
        count = get_quiz_results(
            canvas_course,
            args.quiz_id,
            args.output,
        )
        if args.output:
            log.info("Exported %d student-analysis response rows to %s", count, args.output)
        else:
            log.info("Printed %d student-analysis response rows", count)

    elif helper_func_name == "download_assignment_submissions":
        stats = download_assignment_submissions(
            canvas_course,
            args.assignment_id,
            args.download_dir,
            overwrite=args.overwrite,
            limit=args.limit,
        )
        log.info(
            "Download summary: checked=%(submissions_checked)d attachments=%(attachments_found)d "
            "downloaded=%(downloaded)d skipped_existing=%(skipped_existing)d failed=%(failed)d",
            stats,
        )

    elif helper_func_name == "plan_course":
        from lms_interface.course_plan import build_course_calendar

        result = build_course_calendar(
            plan_path=Path(args.yaml_path),
            output_dir=Path(args.output_dir),
            publish=args.publish,
            publish_weekly_slides=args.publish_weekly_slides,
            publish_attendance_quizzes=args.publish_attendance,
            publish_assignments=args.publish_assignments,
            dry_run=args.dry_run,
            canvas_course=canvas_course,
            page_title_override=args.page_title,
            module_name=args.module_name,
            weekly_module_template_override=args.weekly_module_template,
        )
        log.info("plan-course generated outputs:")
        for name, path in result.output_paths.items():
            log.info(f"  {name}: {path}")
        if result.warnings:
            log.warning("plan-course warnings:")
            for warning in result.warnings:
                log.warning(f"  - {warning}")
        if result.publish_result is not None:
            log.info(
                f"plan-course publish actions (dry_run={result.publish_result['dry_run']}):"
            )
            for action in result.publish_result["actions"]:
                log.info(f"  - {action}")


if __name__ == "__main__":
    main()
