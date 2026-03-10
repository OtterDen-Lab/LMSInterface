from types import SimpleNamespace
from unittest.mock import MagicMock

from lms_interface.canvas_interface import (
    CanvasAssignment,
    CanvasCourse,
    CanvasInterface,
)
from lms_interface.classes import FileSubmission__Canvas, TextSubmission__Canvas


def _build_assignment_with_text_and_attachment():
    interface = CanvasInterface(
        canvas_url="https://canvas.example.edu",
        canvas_key="token",
        privacy_mode="none",
    )

    mock_canvasapi_course = MagicMock()
    mock_canvasapi_course.id = 123
    mock_canvasapi_course.name = "Test Course"
    mock_canvasapi_course.get_user.return_value = SimpleNamespace(name="Test Student")

    course = CanvasCourse(
        canvas_interface=interface,
        canvasapi_course=mock_canvasapi_course,
    )

    mock_canvasapi_assignment = MagicMock()
    mock_canvasapi_assignment.id = 456
    mock_canvasapi_assignment.get_submissions.return_value = [
        SimpleNamespace(
            user_id=42,
            submission_history=[
                {
                    "workflow_state": "submitted",
                    "score": None,
                    "body": "Notes in text box",
                    "attachments": [
                        {
                            "filename": "main.py",
                            "url": "https://canvas.example.edu/files/main.py",
                        }
                    ],
                }
            ],
        )
    ]

    return CanvasAssignment(
        canvasapi_interface=interface,
        canvasapi_course=course,
        canvasapi_assignment=mock_canvasapi_assignment,
    )


def test_get_submissions_prefers_attachments_for_programming_assignments():
    assignment = _build_assignment_with_text_and_attachment()

    submissions = assignment.get_submissions(assignment_kind="ProgrammingAssignment")

    assert len(submissions) == 1
    assert isinstance(submissions[0], FileSubmission__Canvas)


def test_get_submissions_prefers_text_for_non_programming_assignments():
    assignment = _build_assignment_with_text_and_attachment()

    submissions = assignment.get_submissions(assignment_kind="EssayAssignment")

    assert len(submissions) == 1
    assert isinstance(submissions[0], TextSubmission__Canvas)
