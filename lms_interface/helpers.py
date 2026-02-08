#!env python

import argparse
import logging
from datetime import datetime, timezone
from typing import List

import canvasapi

from lms_interface.canvas_interface import (
  CanvasAssignment,
  CanvasCourse,
  CanvasInterface,
)

# Configure logging to actually output
logging.basicConfig()
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

def delete_empty_folders(canvas_course: CanvasCourse):
  log.info("delete_empty_folders")
  num_processed = 0
  for f in canvas_course.get_folders():
    try:
      f.delete()
      log.info(f"({num_processed}) : \"{f}\" deleted")
    except canvasapi.exceptions.BadRequest:
      log.info(f"({num_processed}) : \"{f}\" (not deleted) ({len(list(f.get_files()))})")
    num_processed += 1
  
def get_closed_assignments(interface: CanvasCourse) -> List[canvasapi.assignment.Assignment]:
  closed_assignments: List[canvasapi.assignment.Assignment] = []
  for assignment in interface.get_assignments(
      include=["all_dates"],
      order_by="name"
  ):
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
          if (last_lock_datetime is None) or (lock_datetime >= last_lock_datetime):
            last_lock_datetime = lock_datetime
      
      # If we have found a valid lock time, and it's in the past then we lock
      if last_lock_datetime is not None and last_lock_datetime <= datetime.now(timezone.utc):
        closed_assignments.append(assignment)
        continue
    
    else:
      log.warning(f"Cannot find any lock dates for assignment {assignment.name}!")
  
  return closed_assignments

def get_unsubmitted_submissions(interface: CanvasCourse, assignment: canvasapi.assignment.Assignment) -> List[
  canvasapi.submission.Submission]:
  submissions: List[canvasapi.submission.Submission] = list(
    filter(
      lambda s: s.submitted_at is None and s.percentage_score is None and not s.excused,
      assignment.get_submissions()
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

def deprecate_assignment(canvas_course: CanvasCourse, assignment_id) -> List[canvasapi.assignment.Assignment]:
  
  log.debug(canvas_course.__dict__)
  
  # for assignment in canvas_course.get_assignments():
  #   print(assignment)
  
  canvas_assignment: CanvasAssignment = canvas_course.get_assignment(assignment_id=assignment_id)
  
  canvas_assignment.assignment.edit(
    assignment={
      "name": f"{canvas_assignment.assignment.name} (deprecated)",
      "due_at": f"{datetime.now(timezone.utc).isoformat()}",
      "lock_at": f"{datetime.now(timezone.utc).isoformat()}"
    }
  )

def mark_future_assignments_as_ungraded(canvas_course: CanvasCourse):
  
  for assignment in canvas_course.get_assignments(
      include=["all_dates"],
      order_by="name"
  ):
    if assignment.unlock_at is not None:
      if datetime.fromisoformat(assignment.unlock_at) > datetime.now(timezone.utc):
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
    "deprecate": ("deprecate_assignment", True),
    "ungraded": ("mark_future_assignments_as_ungraded", False),
  }

  parser = argparse.ArgumentParser(
    description="Canvas helper utilities for common course management tasks"
  )

  parser.add_argument(
    "helper",
    choices=HELPERS.keys(),
    help="Helper function to run"
  )

  parser.add_argument(
    "--course-id",
    type=int,
    required=True,
    help="Canvas course ID"
  )

  parser.add_argument(
    "--assignment-id",
    type=int,
    help="Canvas assignment ID (required for deprecate and unsubmitted)"
  )

  parser.add_argument(
    "--prod",
    action="store_true",
    help="Use production Canvas instance instead of development"
  )

  args = parser.parse_args()

  # Get helper function name and whether it requires assignment_id
  helper_func_name, requires_assignment = HELPERS[args.helper]

  # Validate assignment_id requirement
  if requires_assignment and not args.assignment_id:
    parser.error(f"--assignment-id is required for '{args.helper}'")

  # Initialize Canvas interface and course
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

  elif helper_func_name == "deprecate_assignment":
    deprecate_assignment(canvas_course, args.assignment_id)
    log.info(f"Assignment {args.assignment_id} has been deprecated")

  elif helper_func_name == "mark_future_assignments_as_ungraded":
    mark_future_assignments_as_ungraded(canvas_course)


if __name__ == "__main__":
  main()