from pathlib import Path
from types import SimpleNamespace

import pytest

from lms_interface.peer_review import (
    ReviewDocument,
    allocate_reviews,
    build_question_payloads,
    create_peer_reviews,
    load_peer_review_config,
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
