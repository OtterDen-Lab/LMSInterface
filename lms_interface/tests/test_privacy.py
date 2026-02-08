from __future__ import annotations

from unittest.mock import Mock

import pytest

from lms_interface.privacy import PrivacyBackend, PseudonymousStudent


class FakeCourse:
  def __init__(self, course_id, students):
    self.id = course_id
    self.name = "Course"
    self._students = students

  def get_students(self):
    return self._students

  def get_assignment(self, assignment_id):
    return None

  def get_assignments(self, **kwargs):
    return []


class FakeBackend:
  def __init__(self, course):
    self._course = course

  def get_course(self, course_id):
    return self._course


def test_privacy_backend_requires_salt():
  backend = FakeBackend(FakeCourse(1, []))
  with pytest.raises(ValueError, match="LMS_PRIVACY_SALT"):
    PrivacyBackend(backend, salt=None)


def test_privacy_backend_hashes_student_ids():
  student = Mock()
  student.name = "Alice"
  student.user_id = 123
  course = FakeCourse(42, [student])
  backend = PrivacyBackend(FakeBackend(course), salt="secret")

  students = backend.get_course(42).get_students()

  assert len(students) == 1
  assert isinstance(students[0], PseudonymousStudent)
  assert students[0].user_id != "123"
  assert students[0].real_user_id == 123


def test_privacy_backend_id_only_mode_uses_real_id():
  student = Mock()
  student.name = "Alice"
  student.user_id = 123
  course = FakeCourse(42, [student])
  backend = PrivacyBackend(FakeBackend(course), salt="secret", mode="id_only")

  students = backend.get_course(42).get_students()

  assert len(students) == 1
  assert students[0].user_id == "123"
  assert students[0].name == "Student 123"
