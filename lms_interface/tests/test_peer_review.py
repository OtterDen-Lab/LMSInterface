import csv
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lms_interface.peer_review import (
    ReviewDocument,
    allocate_reviews,
    build_question_payloads,
    create_peer_reviews,
    create_peer_review_workspace,
    export_peer_review_summary,
    finalize_peer_review_workspace,
    load_peer_review_config,
    resolve_peer_review_assignment_group,
)


def test_allocate_reviews_spreads_work_and_meets_submission_minimum():
    documents = [
        ReviewDocument("a.pdf", b"a", owner_id=1),
        ReviewDocument("b.pdf", b"b", owner_id=2),
        ReviewDocument("c.pdf", b"c", owner_id=3),
    ]
    assignments, warnings = allocate_reviews(
        documents,
        [1, 2, 3, 4],
        min_reviews_per_student=1,
        max_reviews=3,
        min_reviews_per_submission=3,
        seed=4,
    )

    assert not warnings
    assert all(len(reviews) >= 1 for reviews in assignments.values())
    assert all(len(reviews) <= 3 for reviews in assignments.values())
    for document_index, document in enumerate(documents):
        assert sum(document_index in assigned for assigned in assignments.values()) >= 3
        assert document_index not in assignments[document.owner_id]


def test_allocate_reviews_rejects_insufficient_capacity():
    documents = [
        ReviewDocument(f"{index}.pdf", str(index).encode()) for index in range(2)
    ]
    with pytest.raises(ValueError, match="capacity"):
        allocate_reviews(documents, [1], max_reviews=3, seed=0)


def test_question_payloads_preserve_question_ids_and_likert_labels():
    payloads = build_question_payloads(
        {
            "rubric": {
                "likert_scale": {"labels": {1: "Bad", 2: "Good"}},
                "sections": [
                    {
                        "section": "Rating",
                        "questions": [
                            {"id": "clarity", "type": "likert", "prompt": "Clear?"},
                            {
                                "id": "feedback",
                                "type": "short_response",
                                "prompt": "Feedback?",
                            },
                        ],
                    }
                ],
            }
        }
    )
    assert "notes" not in payloads[0]["question_text"]
    assert [payload["question_name"] for payload in payloads] == [
        "Rating",
        "clarity",
        "feedback",
    ]
    assert payloads[1]["answers"] == [
        {"answer_text": "Bad", "answer_weight": 0},
        {"answer_text": "Good", "answer_weight": 0},
    ]
    assert payloads[2]["question_type"] == "essay_question"


def test_config_requires_shared_dates(tmp_path: Path):
    path = tmp_path / "reviews.yaml"
    path.write_text("rubric: {name: Reviews}\n")
    with pytest.raises(ValueError, match="unlock_at"):
        load_peer_review_config(path)


def test_dry_run_plans_local_pdfs_without_canvas_writes(tmp_path: Path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"test PDF")
    config_path = tmp_path / "reviews.yaml"
    config_path.write_text(
        """pdfs: [report.pdf]
reviews_per_student: 3
unlock_at: 2026-09-01T08:00:00-07:00
due_at: 2026-09-08T23:59:00-07:00
lock_at: 2026-09-09T00:00:00-07:00
rubric:
  name: Peer Review
  likert_scale:
    labels: {1: Bad, 2: Good}
  sections:
    - section: Rating
      questions:
        - id: clarity
          type: likert
          prompt: Is it clear?
"""
    )

    class FakeCourse:
        def get_students(self):
            return [
                SimpleNamespace(user_id=1),
                SimpleNamespace(user_id=2),
                SimpleNamespace(user_id=3),
            ]

    plan = create_peer_reviews(FakeCourse(), config_path=config_path, dry_run=True)

    assert plan.coverage == [3]
    assert all(len(assigned) == 1 for assigned in plan.assignments.values())


def test_create_peer_reviews_creates_restricted_graded_survey(tmp_path: Path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"test PDF")
    config_path = tmp_path / "reviews.yaml"
    config_path.write_text(
        """pdfs: [report.pdf]
unlock_at: "2026-09-01T08:00:00-07:00"
due_at: "2026-09-08T23:59:00-07:00"
lock_at: "2026-09-09T00:00:00-07:00"
rubric:
  name: Peer Review
  likert_scale:
    labels: {1: Bad, 2: Good}
  sections:
    - section: Rating
      questions:
        - id: clarity
          type: likert
          prompt: Is it clear?
"""
    )

    class FakeQuiz:
        id = 34
        assignment_id = 12

        def __init__(self):
            self.questions = []
            self.edits = []

        def create_question(self, *, question):
            self.questions.append(question)

        def edit(self, *, quiz):
            self.edits.append(quiz)

    class FakeAssignment:
        def __init__(self):
            self.edits = []
            self.overrides = []

        def edit(self, *, assignment):
            self.edits.append(assignment)

        def create_override(self, *, assignment_override):
            self.overrides.append(assignment_override)

    class FakeCanvasApiCourse:
        id = 99

        def __init__(self):
            self.quiz_payloads = []
            self.quiz = FakeQuiz()
            self.assignment = FakeAssignment()

        def upload(self, path, *, parent_folder_path, on_duplicate):
            assert path.name.endswith(".pdf")
            assert len(path.stem) == 12
            assert parent_folder_path == "Peer Review Files"
            assert on_duplicate == "rename"
            return True, {"id": 4}

        def create_quiz(self, *, quiz):
            self.quiz_payloads.append(quiz)
            return self.quiz

        def get_assignment(self, assignment_id):
            assert assignment_id == 12
            return self.assignment

    class FakeCourse:
        def __init__(self):
            self.course = FakeCanvasApiCourse()
            self.canvas_interface = SimpleNamespace(
                canvas_url="https://canvas.example/api/v1"
            )

        def get_students(self):
            return [
                SimpleNamespace(user_id=1),
                SimpleNamespace(user_id=2),
                SimpleNamespace(user_id=3),
            ]

        def create_assignment_group(self, *, name, delete_existing):
            assert name == "Peer Reviews"
            assert not delete_existing
            return SimpleNamespace(id=8)

    course = FakeCourse()
    create_peer_reviews(course, config_path=config_path)

    quiz_payload = course.course.quiz_payloads[0]
    assert quiz_payload["quiz_type"] == "graded_survey"
    assert quiz_payload["published"] is False
    assert quiz_payload["only_visible_to_overrides"] is True
    assert "PDF review document:" in quiz_payload["description"]
    assert "instructure_scribd_file" in quiz_payload["description"]
    assert ".pdf</a>" in quiz_payload["description"]
    assert 'data-canvas-previewable="true"' in quiz_payload["description"]
    assert "/courses/99/files/4/download?wrap=1" in quiz_payload["description"]
    assert "/api/v1/courses/99/files/4" in quiz_payload["description"]
    assert course.course.quiz.edits == [{"published": False}]
    assert course.course.assignment.edits[0]["published"] is False
    assert course.course.assignment.overrides == [
        {
            "student_ids": [1, 2, 3],
            "unlock_at": "2026-09-01T08:00:00-07:00",
            "due_at": "2026-09-08T23:59:00-07:00",
            "lock_at": "2026-09-09T00:00:00-07:00",
        }
    ]


def test_load_source_documents_accepts_canvasapi_file_objects(monkeypatch):
    from lms_interface.peer_review import load_source_documents

    attachment = SimpleNamespace(filename="report.pdf", url="https://files.example/report")
    submission = SimpleNamespace(
        user_id=7,
        submitted_at="2026-08-30T00:00:00Z",
        attachments=[attachment],
    )
    assignment = SimpleNamespace(get_submissions=lambda: [submission])
    canvas_course = SimpleNamespace(
        course=SimpleNamespace(get_assignment=lambda assignment_id: assignment),
        canvas_interface=SimpleNamespace(canvas_key="token"),
    )
    response = SimpleNamespace(content=b"PDF data", raise_for_status=lambda: None)
    monkeypatch.setattr("lms_interface.peer_review.requests.get", lambda *args, **kwargs: response)

    documents, warnings = load_source_documents(canvas_course, 10)

    assert warnings == []
    assert documents == [ReviewDocument("report.pdf", b"PDF data", owner_id=7)]


def test_allocate_reviews_assigns_one_document_to_every_reviewer_when_possible():
    documents = [ReviewDocument("only.pdf", b"only")]

    assignments, warnings = allocate_reviews(
        documents,
        [1, 2, 3, 4],
        min_reviews_per_student=1,
        max_reviews=3,
        min_reviews_per_submission=3,
        seed=1,
    )

    assert not warnings
    assert sum(0 in assigned for assigned in assignments.values()) == 4


def test_resolve_peer_review_group_reports_all_duplicate_names():
    group_one = SimpleNamespace(id=12, name="Peer Reviews", position=1)
    group_two = SimpleNamespace(id=24, name="Peer Reviews", position=4)
    course = SimpleNamespace(
        course=SimpleNamespace(
            get_assignment_groups=lambda: [group_one, group_two],
            get_assignments_for_group=lambda group_id: [
                SimpleNamespace(id=group_id * 10, name=f"Review {group_id}")
            ],
        )
    )

    with pytest.raises(ValueError, match="group_id=12") as exc_info:
        resolve_peer_review_assignment_group(
            course, assignment_group_name="Peer Reviews"
        )

    message = str(exc_info.value)
    assert "group_id=24" in message
    assert "--assignment-id" in message


def test_resolve_peer_review_group_accepts_quiz_id_in_assignment_id_slot():
    from canvasapi.exceptions import ResourceDoesNotExist

    group = SimpleNamespace(id=8, name="Peer Reviews")

    def get_assignment(assignment_id):
        raise ResourceDoesNotExist("Not Found")

    course = SimpleNamespace(
        course=SimpleNamespace(
            get_assignment=get_assignment,
            get_quiz=lambda quiz_id: SimpleNamespace(
                id=quiz_id, assignment_group_id=8
            ),
            get_assignment_group=lambda group_id: group,
        )
    )

    assert resolve_peer_review_assignment_group(course, assignment_id=159325) is group


def test_export_peer_review_summary_uses_assignment_to_find_group_and_source_owner(
    tmp_path: Path, monkeypatch
):
    from lms_interface.peer_review import ReviewDocument

    group = SimpleNamespace(id=8, name="Peer Reviews")
    assignment = SimpleNamespace(id=12, name="Review A", assignment_group_id=8)
    quiz = SimpleNamespace(
        id=34,
        assignment_id=12,
        quiz_type="graded_survey",
        title="Peer Review [a1b2c3d4e5f6]",
    )
    requested_report = SimpleNamespace(id=55)
    quiz.create_report = lambda report_type: requested_report
    course = SimpleNamespace(
        course=SimpleNamespace(
            get_assignment=lambda assignment_id: assignment,
            get_assignment_group=lambda group_id: group,
            get_assignments_for_group=lambda group_id: [assignment],
        ),
        get_quizzes=lambda: [quiz],
    )
    monkeypatch.setattr(
        "lms_interface.peer_review.load_source_documents",
        lambda canvas_course, source_assignment_id: (
            [ReviewDocument("report.pdf", b"ignored", owner_id=7)],
            [],
        ),
    )
    # The source document's digest is normally calculated from its PDF bytes.
    monkeypatch.setattr(
        ReviewDocument,
        "short_digest",
        property(lambda self: "a1b2c3d4e5f6"),
    )
    monkeypatch.setattr(
        "lms_interface.peer_review.wait_for_quiz_report",
        lambda raw_quiz, report: report,
    )
    monkeypatch.setattr(
        "lms_interface.peer_review.download_quiz_report_csv",
        lambda canvas_course, report: (
            "Student Name,Student ID,Attempt,Completed At,Score,"
            "2784991: Rating,2784992: Is it clear?,2784993: Comments,"
            "Login ID,n correct\n"
            "Reviewer,99,1,2026-10-09,1,,4,Great work,reviewer99,9\n"
        ),
    )
    monkeypatch.setattr("lms_interface.peer_review.quiz_likert_values", lambda quiz: {})

    output_path = tmp_path / "results.csv"
    rubric_path = tmp_path / "peer-review.yaml"
    rubric_path.write_text(
        """unlock_at: "2026-09-01T08:00:00-07:00"
due_at: "2026-09-08T23:59:00-07:00"
lock_at: "2026-09-09T00:00:00-07:00"
rubric:
  name: Peer Review
  likert_scale:
    labels: {1: Poor, 2: Good}
  sections:
    - section: Rating
      questions:
        - id: clarity
          type: likert
          prompt: Is it clear?
        - id: comments
          type: short_response
          prompt: Comments
"""
    )
    count = export_peer_review_summary(
        course,
        output_path=output_path,
        assignment_id=12,
        source_assignment_id=101,
        rubric_config_path=rubric_path,
    )

    assert count == 1
    with output_path.open(encoding="utf-8-sig", newline="") as output_file:
        rows = list(csv.DictReader(output_file))
    assert rows == [
        {
            "pdf_id": "a1b2c3d4e5f6",
            "submitter_id": "7",
            "quiz_id": "34",
            "quiz_submission_id": "",
            "reviewer_id": "99",
            "attempt": "1",
            "workflow_state": "complete",
            "submitted_at": "2026-10-09",
            "clarity": "4",
            "comments": "Great work",
        }
    ]


def test_export_peer_review_summary_requests_every_report_before_collecting(
    tmp_path: Path, monkeypatch
):
    group = SimpleNamespace(id=8, name="Peer Reviews")
    assignments = [
        SimpleNamespace(id=12, assignment_group_id=8),
        SimpleNamespace(id=13, assignment_group_id=8),
    ]
    events: list[tuple[str, int]] = []

    def make_quiz(quiz_id: int, assignment_id: int, digest: str):
        quiz = SimpleNamespace(
            id=quiz_id,
            assignment_id=assignment_id,
            quiz_type="graded_survey",
            title=f"Peer Review [{digest}]",
        )
        quiz.create_report = lambda report_type: (
            events.append(("request", quiz_id)) or SimpleNamespace(id=quiz_id)
        )
        return quiz

    first_quiz = make_quiz(34, 12, "a1b2c3d4e5f6")
    second_quiz = make_quiz(35, 13, "b1c2d3e4f5a6")
    course = SimpleNamespace(
        course=SimpleNamespace(
            get_assignment=lambda assignment_id: assignments[0],
            get_assignment_group=lambda group_id: group,
            get_assignments_for_group=lambda group_id: assignments,
        ),
        get_quizzes=lambda: [first_quiz, second_quiz],
    )
    monkeypatch.setattr(
        "lms_interface.peer_review.wait_for_quiz_report",
        lambda raw_quiz, report: events.append(("collect", report.id)) or report,
    )
    monkeypatch.setattr(
        "lms_interface.peer_review.download_quiz_report_csv",
        lambda canvas_course, report: (
            "Student,ID,2784992: Question 1\nReviewer,99,Agree / Good\n"
        ),
    )
    monkeypatch.setattr(
        "lms_interface.peer_review.quiz_likert_values",
        lambda quiz: {"agree / good": 3},
    )

    output_path = tmp_path / "results.csv"
    assert export_peer_review_summary(
        course, output_path=output_path, assignment_id=12
    ) == 2
    assert events == [
        ("request", 34),
        ("request", 35),
        ("collect", 34),
        ("collect", 35),
    ]
    with output_path.open(encoding="utf-8-sig", newline="") as output_file:
        rows = list(csv.DictReader(output_file))
    assert [row["Question 1"] for row in rows] == ["3", "3"]


def test_create_peer_review_workspace_writes_shuffled_template_and_selected_pdfs(
    tmp_path: Path, monkeypatch
):
    documents = [
        ReviewDocument("low.pdf", b"low", owner_id=1),
        ReviewDocument("lower-middle.pdf", b"lower-middle", owner_id=2),
        ReviewDocument("middle.pdf", b"middle", owner_id=3),
        ReviewDocument("upper-middle.pdf", b"upper-middle", owner_id=4),
        ReviewDocument("high.pdf", b"high", owner_id=5),
    ]

    def fake_export(canvas_course, *, output_path, source_documents, **kwargs):
        with output_path.open("w", encoding="utf-8-sig", newline="") as output_file:
            writer = csv.DictWriter(
                output_file,
                fieldnames=["pdf_id", "submitter_id", "likert_clarity"],
            )
            writer.writeheader()
            writer.writerows(
                [
                    {
                        "pdf_id": document.short_digest,
                        "submitter_id": str(document.owner_id),
                        "likert_clarity": str([1, 2, 2, 3, 4][index - 1]),
                    }
                    for index, document in enumerate(documents, start=1)
                ]
            )
        assert source_documents == documents
        return 5

    monkeypatch.setattr(
        "lms_interface.peer_review.load_source_documents",
        lambda canvas_course, assignment_id: (documents, []),
    )
    monkeypatch.setattr("lms_interface.peer_review.export_peer_review_summary", fake_export)
    rubric_path = tmp_path / "rubric.yaml"
    rubric_path.write_text(
        """unlock_at: 2026-09-01T08:00:00-07:00
due_at: 2026-09-08T23:59:00-07:00
lock_at: 2026-09-09T00:00:00-07:00
rubric:
  name: Peer Review
  likert_scale:
    labels: {1: Poor, 2: Fair, 3: Good, 4: Excellent}
  sections:
    - section: Rating
      questions:
        - id: likert_clarity
          type: likert
          prompt: Is it clear?
        - id: comments
          type: short_response
          prompt: Comments
"""
    )
    work_dir = tmp_path / "workspace"

    assert create_peer_review_workspace(
        object(),
        work_dir=work_dir,
        course_id=123,
        quiz_id=456,
        assignment_id=None,
        assignment_group_id=None,
        assignment_group_name=None,
        source_assignment_id=789,
        rubric_config_path=rubric_path,
        seed=3,
    ) == work_dir

    with (work_dir / "professor-review.csv").open(
        encoding="utf-8-sig", newline=""
    ) as review_file:
        rows = list(csv.DictReader(review_file))
    assert len(rows) == 5
    assert set(rows[0]) == {
        "pdf_id",
        "submitter_id",
        "pdf_path",
        "likert_clarity",
        "comments",
        "instructor_notes",
    }
    assert all(row["likert_clarity"] == "" for row in rows)
    assert all((work_dir / row["pdf_path"]).is_file() for row in rows)

    config = yaml.safe_load((work_dir / "peer-review-workspace.yaml").read_text())
    assert config["course_id"] == 123
    assert config["canvas_push"]["paper_assignment_id"] == 789
    assert config["selection"]["selected_pdf_ids"]
    assert config["selection"]["random_count"] == 2


def test_finalize_peer_review_workspace_writes_local_grades_participation_and_feedback(
    tmp_path: Path,
):
    rubric_path = tmp_path / "rubric.yaml"
    rubric_path.write_text(
        """unlock_at: 2026-09-01T08:00:00-07:00
due_at: 2026-09-08T23:59:00-07:00
lock_at: 2026-09-09T00:00:00-07:00
rubric:
  name: Peer Review
  likert_scale:
    labels: {1: Poor, 2: Fair, 3: Good, 4: Excellent}
  sections:
    - section: Rating
      questions:
        - id: clarity
          type: likert
          prompt: Is it clear?
        - id: public_comment
          type: short_response
          prompt: What worked well?
          visible_to_author: true
        - id: private_check
          type: short_response
          prompt: Summarize the report.
          visible_to_author: false
"""
    )
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    (work_dir / "peer-review-workspace.yaml").write_text(
        yaml.safe_dump(
            {
                "rubric_config_path": str(rubric_path),
                "files": {
                    "peer_review_results": "peer-review-results.csv",
                    "professor_review": "professor-review.csv",
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
            }
        )
    )
    (work_dir / "peer-review-results.csv").write_text(
        """pdf_id,submitter_id,reviewer_id,clarity,public_comment,private_check
paper-a,10,20,4,Useful plot,Correct summary
paper-a,10,21,2,More explanation needed,Another summary
paper-b,11,20,3,,Summary
"""
    )
    (work_dir / "professor-review.csv").write_text(
        """pdf_id,clarity,instructor_notes
paper-a,4,Instructor note
"""
    )

    paths = finalize_peer_review_workspace(work_dir)

    with paths["proposed_paper_grades"].open(encoding="utf-8-sig", newline="") as file:
        grades = {row["pdf_id"]: row for row in csv.DictReader(file)}
    # The instructor gave paper-a a 4 while peers averaged 3. The +1 correction
    # is bounded at 4 and inherited by paper-b.
    assert grades["paper-a"]["raw_peer_grade_percent"] == "80.00"
    assert grades["paper-a"]["proposed_grade_percent"] == "100.00"
    assert grades["paper-a"]["calibration_likert_adjustment"] == "1.00"
    assert grades["paper-a"]["calibration_grade_adjustment_percent"] == "20.00"
    assert grades["paper-b"]["proposed_grade_percent"] == "100.00"
    assert grades["paper-a"]["status"] == "instructor_calibration"
    assert grades["paper-b"]["status"] == "peer_only"

    with paths["calibration"].open(encoding="utf-8-sig", newline="") as file:
        calibration = list(csv.DictReader(file))
    assert calibration == [
        {
            "criterion_id": "clarity",
            "calibration_sample_count": "1",
            "calibration_peer_mean": "3.00",
            "calibration_instructor_mean": "4.00",
            "instructor_minus_peer_delta": "1.00",
        }
    ]

    feedback = (paths["feedback"] / "paper-a.md").read_text()
    assert "Is it clear?" in feedback
    assert "Instructor score" in feedback
    assert "Useful plot" in feedback
    assert "Correct summary" not in feedback
    assert "Instructor note" in feedback
    assert feedback.rfind("## Instructor feedback") > feedback.rfind("## What worked well?")
    assert "instructor calibration of peer-review scores" in feedback

    with paths["participation"].open(encoding="utf-8-sig", newline="") as file:
        participation = {row["reviewer_id"]: row for row in csv.DictReader(file)}
    assert participation["20"]["average_review_participation_percent"] == "90.00"
    assert participation["20"]["flags"] == "missing_written_feedback"
