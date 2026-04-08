#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from lms_interface.canvas_interface import CanvasInterface

log = logging.getLogger(__name__)


def _load_payload(value: str | None, *, label: str) -> dict[str, Any] | None:
  if value is None:
    return None

  text = value.strip()
  if not text:
    return None

  path = Path(text)
  if path.exists():
    payload_text = path.read_text(encoding="utf-8")
  else:
    payload_text = text

  try:
    payload = json.loads(payload_text)
  except json.JSONDecodeError:
    payload = yaml.safe_load(payload_text)

  if payload is None:
    return None
  if not isinstance(payload, dict):
    raise ValueError(f"{label} must decode to a JSON/YAML object.")
  return payload


def _build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Manual Canvas testing helpers")
  subparsers = parser.add_subparsers(dest="command", required=True)

  grade = subparsers.add_parser(
      "grade",
      help="Post a manual grade, comments, and optional rubric assessment to Canvas",
  )
  grade.add_argument("--course-id", type=int, required=True, help="Canvas course ID")
  grade.add_argument(
      "--assignment-id", type=int, required=True, help="Canvas assignment ID"
  )
  grade.add_argument("--student-id", required=True, help="Canvas student ID")
  grade.add_argument(
      "--score",
      type=float,
      help="Numeric score to post. Omit this when rubric points should determine the grade.",
  )
  grade.add_argument(
      "--comments",
      default="",
      help="Feedback comment body to upload to Canvas.",
  )
  grade.add_argument(
      "--rubric-assessment",
      help="JSON/YAML object or file path describing rubric assessment data keyed by Canvas criterion names.",
  )
  grade.add_argument(
      "--prod",
      action="store_true",
      help="Use the production Canvas credentials/env vars.",
  )
  grade.add_argument(
      "--dry-run",
      action="store_true",
      help="Print the resolved payload without writing to Canvas.",
  )
  grade.add_argument(
      "--debug",
      action="store_true",
      help="Enable verbose logging from the Canvas client.",
  )
  return parser


def main() -> int:
  parser = _build_parser()
  args = parser.parse_args()

  if args.command != "grade":
    parser.error(f"Unsupported command: {args.command}")

  logging.basicConfig(level=logging.INFO)
  if args.debug:
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("canvasapi.requester").setLevel(logging.INFO)
    logging.getLogger("canvasapi").setLevel(logging.INFO)

  rubric_assessment = _load_payload(args.rubric_assessment, label="rubric_assessment")

  interface = CanvasInterface(prod=args.prod)
  course = interface.get_course(args.course_id)
  assignment = course.get_assignment(args.assignment_id)
  if assignment is None:
    raise ValueError(
        f"Assignment {args.assignment_id} was not found in course {args.course_id}."
    )

  if args.dry_run:
    resolved_rubric_assessment = None
    if rubric_assessment is not None:
      resolved_rubric_assessment = assignment.resolve_rubric_assessment(
          rubric_assessment
      )
    log.info("Dry run; not writing to Canvas.")
    log.info(
        "Resolved payload: course_id=%s assignment_id=%s student_id=%s score=%r rubric_assessment=%r",
        args.course_id,
        args.assignment_id,
        args.student_id,
        args.score,
        resolved_rubric_assessment,
    )
    return 0

  result = assignment.push_feedback(
      user_id=args.student_id,
      score=args.score,
      comments=args.comments,
      rubric_assessment=rubric_assessment,
  )
  if not result:
    raise RuntimeError("Canvas feedback update failed.")

  log.info(
      "Posted feedback for course=%s assignment=%s student=%s",
      args.course_id,
      args.assignment_id,
      args.student_id,
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
