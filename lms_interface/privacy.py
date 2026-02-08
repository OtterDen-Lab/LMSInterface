from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from .classes import Submission
from .interfaces import LMSAssignment, LMSBackend, LMSCourse, LMSUser


def _hash_id(value: str, salt: str) -> str:
  digest = hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()
  return digest


@dataclass(frozen=True)
class PseudonymousStudent(LMSUser):
  name: str
  user_id: str
  real_user_id: str | int | None = None


class PrivacyBackend(LMSBackend):
  def __init__(self, backend: LMSBackend, *, salt: str | None = None, mode: str = "pseudonymous"):
    self._backend = backend
    self._mode = mode
    self._salt = salt or os.environ.get("LMS_PRIVACY_SALT")
    if self._mode not in {"pseudonymous", "id_only"}:
      raise ValueError("Privacy mode must be 'pseudonymous' or 'id_only'.")
    if self._mode == "pseudonymous" and not self._salt:
      raise ValueError("LMS_PRIVACY_SALT is required for pseudonymous privacy mode.")

  def get_course(self, course_id: int) -> LMSCourse:
    return PrivacyCourseAdapter(self._backend.get_course(course_id), salt=self._salt, mode=self._mode)


@dataclass
class PrivacyCourseAdapter(LMSCourse):
  _course: LMSCourse
  salt: str
  mode: str

  @property
  def id(self):
    return self._course.id

  @property
  def name(self):
    return self._course.name

  def _student_alias(self, student: LMSUser) -> PseudonymousStudent:
    raw_id = str(student.user_id)
    if self.mode == "id_only":
      return PseudonymousStudent(
        name=f"Student {raw_id}",
        user_id=raw_id,
        real_user_id=student.user_id
      )
    hashed = _hash_id(f"{self.id}:{raw_id}", self.salt)
    short = hashed[:8]
    return PseudonymousStudent(
      name=f"Student {short}",
      user_id=hashed,
      real_user_id=student.user_id
    )

  def get_assignment(self, assignment_id: int) -> LMSAssignment | None:
    assignment = self._course.get_assignment(assignment_id)
    if assignment is None:
      return None
    return PrivacyAssignmentAdapter(assignment, salt=self.salt, course_id=str(self.id), mode=self.mode)

  def get_assignments(self, **kwargs) -> list[LMSAssignment]:
    return [
      PrivacyAssignmentAdapter(a, salt=self.salt, course_id=str(self.id), mode=self.mode)
      for a in self._course.get_assignments(**kwargs)
    ]

  def get_students(self):
    return [self._student_alias(s) for s in self._course.get_students()]


@dataclass
class PrivacyAssignmentAdapter(LMSAssignment):
  _assignment: LMSAssignment
  salt: str
  course_id: str
  mode: str

  @property
  def id(self):
    return self._assignment.id

  @property
  def name(self):
    return self._assignment.name

  def _student_alias(self, student: LMSUser) -> PseudonymousStudent:
    raw_id = str(student.user_id)
    if self.mode == "id_only":
      return PseudonymousStudent(
        name=f"Student {raw_id}",
        user_id=raw_id,
        real_user_id=student.user_id
      )
    hashed = _hash_id(f"{self.course_id}:{raw_id}", self.salt)
    short = hashed[:8]
    return PseudonymousStudent(
      name=f"Student {short}",
      user_id=hashed,
      real_user_id=student.user_id
    )

  def get_submissions(self, **kwargs) -> list[Submission]:
    submissions = self._assignment.get_submissions(**kwargs)
    for submission in submissions:
      if getattr(submission, "student", None) is not None:
        submission.student = self._student_alias(submission.student)
    return submissions

  def push_feedback(
      self,
      user_id,
      score: float,
      comments: str,
      attachments=None,
      keep_previous_best: bool = True,
      clobber_feedback: bool = False
  ) -> None:
    self._assignment.push_feedback(
      user_id=user_id,
      score=score,
      comments=comments,
      attachments=attachments,
      keep_previous_best=keep_previous_best,
      clobber_feedback=clobber_feedback
    )
