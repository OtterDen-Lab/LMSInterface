# LMSInterface

Lightweight LMS abstraction focused on Canvas. This repo is primarily used
as a vendored dependency in teaching tools.

## Usage

Set Canvas credentials in `~/.env` (or pass `env_path` to `CanvasInterface`):

- `CANVAS_API_URL`
- `CANVAS_API_KEY`

For production, use:

- `CANVAS_API_URL_prod`
- `CANVAS_API_KEY_prod`

## Tests

Run:

```bash
pytest
```

Tests live in `lms_interface/tests/` so they vendor cleanly.

## Cleanup Missing Helper

Use `cleanup-missing` to normalize Canvas late-policy status for unsubmitted work:

- Past due: set to `missing`
- Future due: set to `none`

The helper can also clear stale placeholder grades (`Incomplete`/`0`) on future-due,
contentless placeholder submissions.

When run in an interactive terminal, the helper now shows a live progress view
with the current assignment and a running results table. Use `--plain-output` if
you want the older plain text behavior.

Run via module:

```bash
python -m lms_interface.helpers cleanup-missing --course-id <COURSE_ID> --dry-run
```

Or via script entry point:

```bash
lms-interface-helper cleanup-missing --course-id <COURSE_ID> --dry-run
```

Useful flags:

- `--assignment-id <ID>`: scope to one assignment
- `--clear-placeholder-grade`: clear stale placeholder grades
- `--include-unpublished`: include unpublished assignments
- `--debug`: verbose per-student logs

Recommended rollout:

1. Run `--dry-run` on one assignment.
2. Run live on one assignment and verify in Canvas UI.
3. Run course-wide with `--dry-run`.
4. Run course-wide live.

## Manual Grade Test Harness

Use `test.py` for one-off grading against a real Canvas submission while you
are validating rubric behavior or feedback formatting.

Example:

```bash
python test.py grade \
  --course-id <COURSE_ID> \
  --assignment-id <ASSIGNMENT_ID> \
  --student-id <STUDENT_ID> \
  --score 8 \
  --comments "Well done." \
  --rubric-assessment '{"criterion 1": 5, "crit2": {"points": 3, "rating_id": "rat3"}}'
```

Useful notes:

- `--rubric-assessment` accepts either inline JSON/YAML or a path to a file
  containing an object.
- Rubric keys should be the exact criterion names shown in Canvas, such as
  `criterion 1` and `crit2` in the example above.
- If you omit `--score`, the Canvas wrapper derives the posted grade from the
  rubric points total.
- This command posts feedback to an existing assignment. It does not create or
  attach rubrics to assignments.

## Course Plan Helper

Generate a student-facing calendar (HTML + JSON) from a course plan YAML and
optionally publish it to Canvas as a page.

Dry-run / local generation only:

```bash
lms-interface-helper plan-course --yaml-path course_plans/cst334_compact.yaml --output-dir build/cst334
```

Publish to Canvas:

```bash
lms-interface-helper plan-course --yaml-path course_plans/cst334_compact.yaml --course-id <COURSE_ID> --publish
```

Useful flags:

- `--dry-run`: show Canvas actions without writing when `--publish` is used
- `--page-title`: override published page title
- `--module-name`: module to place schedule page link in (default: `Course Schedule`)
- `--publish-weekly-slides`: create/reuse `Week XX` modules and add slide URL links
- `--weekly-module-template`: override week module naming (default from plan: `Week {week_number}`)

Plan hint:

- Set `sync.topics_per_meeting: 2` (or higher) when a single class session covers multiple topic blocks.
- Set `duration_hours` on a topic when it needs extra in-class time (for example `duration_hours: 3`).
- Use reusable placeholders to insert spacer blocks without redefining `tbd-*` topics each term.
- Define `resource_defaults.lecture_slides_base_url` to avoid repeating full slide URLs in each topic.

Example:

```yaml
resource_defaults:
  lecture_slides_base_url: https://github.com/CSUMB-SCD-instructors/CST334/tree/main/slides/pdfs

topics:
  - id: process-scheduling-os7
    lecture_slides:
      - OSTEP 07.pdf
```

Reusable placeholder example:

```yaml
placeholders:
  tbd:
    title: Buffer / TBD
    new_material: false

topics:
  - id: mlfq
    duration_hours: 3
  - placeholder: tbd
    duration_hours: 1
```

Validate a plan file against the schema:

```bash
python scripts/validate_course_plan.py course_plans/cst334_compact.yaml
```

## Parallel Uploads

Question uploads support multi-threading to speed up large quizzes. The default
behavior uses a small worker pool and limits in-flight requests.

Defaults:

- `max_workers = 4`
- `max_in_flight = 8`

If any worker receives a Canvas `429`, all workers respect a shared backoff
window before continuing.

You can override these via `CanvasCourse.create_question(...)` or by calling
the lower-level `_upload_question_payloads(...)`.

## Backend Abstraction (LTI-Ready)

New tools should depend on the LMS-agnostic interfaces and adapters instead
of Canvas directly. This makes it easier to add an LTI backend later.

Recommended entry points:

- `CanvasBackend` for the current Canvas API
- `PrivacyBackend` to enforce FERPA-friendly anonymization
- `CanvasInterface(privacy_mode="id_only")` for quick ID-only redaction

By default, student names are redacted. To request real names, pass
`include_names=True` to:

- `CanvasCourse.get_students(...)`
- `CanvasAssignment.get_submissions(...)`
- `CanvasQuiz.get_quiz_submissions(...)`

Example:

```python
from lms_interface.backends import CanvasBackend
from lms_interface.privacy import PrivacyBackend

backend = CanvasBackend(prod=False)
backend = PrivacyBackend(backend, salt="my-course-salt", mode="pseudonymous")

course = backend.get_course(12345)
students = course.get_students()
```

Privacy modes:

- `pseudonymous`: hashed IDs (requires `LMS_PRIVACY_SALT`)
- `id_only`: uses the real Canvas ID but redacts names (`Student <id>`)

## Vendoring

Use the shared script to vendor into another project:

```bash
python scripts/vendor_into_project.py /path/to/target --top-level
```

## GitHub Release Artifacts

This repo can publish wheel/sdist artifacts to GitHub Releases on tag pushes.

Flow:

1. Bump version and publish commit/tag in one step (for example via `git bump patch`).
2. GitHub Actions builds `dist/*` and attaches artifacts to that release.

Default one-command option:

```bash
git bump patch
```

Optional local-only bump (no push/tag):

```bash
git bump patch --no-tag --no-push
```

Tag/version contract:

- Tag `vX.Y.Z` must match `project.version = "X.Y.Z"` in `pyproject.toml`.
- The release workflow fails if they do not match.

Downstream projects can pin to a release artifact instead of vendoring:

```toml
dependencies = [
  "lms-interface @ https://github.com/<org>/LMSInterface/releases/download/v0.4.5/lms_interface-0.4.5-py3-none-any.whl",
]
```

## Peer-Review Surveys

Create one restricted Canvas graded survey per PDF, with a shared YAML rubric and
randomized student assignments:

```bash
lms-interface-helper peer-review --course-id <COURSE_ID> \
  --yaml-path example_files/peer-review-questions.yaml --dry-run
```

Remove `--dry-run` to create the surveys. The beta deliberately creates a new
batch on every live run. The YAML requires `unlock_at`, `due_at`, and `lock_at`;
`points_possible` defaults to 1, `published` defaults to `false`, and
`reviews_per_student` is a per-reviewer cap (default 3). Each PDF receives at least
three reviews by default, and every eligible reviewer receives at least one distinct
PDF when possible. Configure `min_reviews_per_submission` and
`min_reviews_per_student` to override those minimums.

For submitted reports, use the source assignment instead of the `pdfs` list:

```bash
lms-interface-helper peer-review --course-id <COURSE_ID> \
  --source-assignment-id <ASSIGNMENT_ID> --yaml-path peer-review.yaml
```

The helper takes the first PDF attachment from each source submission and never
assigns a student their own submission. Local `pdfs` remain available for debug
runs. Use `--seed` to reproduce a randomized allocation.

Peer-review YAML ignores legacy section `note` fields. Use `instructions` on the
rubric (shown above the PDF) or on a section (shown under that section heading)
for student-facing guidance.

### Export peer-review results

Export one CSV row per peer-review survey response by giving any
assignment ID from the peer-review batch. The helper derives its assignment
group, so instructors do not need to find a Canvas assignment-group ID:

```bash
lms-interface-helper peer-review-summary --course-id <COURSE_ID> \
  --quiz-id <PEER_REVIEW_QUIZ_ID> \
  --source-assignment-id <ORIGINAL_PDF_ASSIGNMENT_ID> \
  --yaml-path peer-review.yaml \
  --output peer-review-results.csv
```

The CSV includes the PDF ID embedded in each generated quiz title, the reviewer
and source-submitter IDs, submission metadata, and one column per rubric
question. `--source-assignment-id` is optional, but it resolves each PDF ID back
to its original submitter. `--assignment-id` also works when you have the
backing Canvas assignment ID; for convenience it falls back to treating an
unknown assignment ID as a Classic Quiz ID. You can instead select a batch with
`--assignment-group-name`; if multiple groups match, the helper prints every
matching group and assignment so the next run can use an unambiguous assignment
ID or `--assignment-group-id`. Supply the same peer-review YAML with
`--yaml-path` to give the result columns their stable rubric question IDs instead
of Canvas's localized question labels.
