from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_test_script():
  script_path = Path(__file__).resolve().parents[2] / "test.py"
  spec = importlib.util.spec_from_file_location("manual_grade_test_script", script_path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


def test_load_payload_accepts_inline_json_and_file(tmp_path):
  module = _load_test_script()

  inline = module._load_payload('{"crit1": 5}', label="rubric_assessment")
  assert inline == {"crit1": 5}

  payload_path = tmp_path / "rubric.json"
  payload_path.write_text('{"crit2": {"points": 3}}', encoding="utf-8")
  loaded = module._load_payload(str(payload_path), label="rubric_assessment")
  assert loaded == {"crit2": {"points": 3}}


def test_grade_dry_run_skips_canvas_write(monkeypatch):
  module = _load_test_script()
  calls = []

  class FakeAssignment:
    def resolve_rubric_assessment(self, rubric_assessment):
      assert rubric_assessment == {"criterion 1": 5}
      return {"crit1": {"points": 5}}

    def push_feedback(self, **kwargs):
      calls.append(kwargs)
      return True

  class FakeCourse:
    def get_assignment(self, assignment_id):
      assert assignment_id == 456
      return FakeAssignment()

  class FakeInterface:
    def __init__(self, *, prod):
      self.prod = prod

    def get_course(self, course_id):
      assert course_id == 123
      return FakeCourse()

  monkeypatch.setattr(module, "CanvasInterface", FakeInterface)
  monkeypatch.setattr(
      sys,
      "argv",
      [
          "test.py",
          "grade",
          "--course-id",
          "123",
          "--assignment-id",
          "456",
          "--student-id",
          "789",
          "--score",
          "9",
          "--rubric-assessment",
          '{"criterion 1": 5}',
          "--dry-run",
      ],
  )

  assert module.main() == 0
  assert calls == []
