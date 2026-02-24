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

1. Bump version in `pyproject.toml` (for example via `git bump patch`).
2. Create and push a matching tag (`v<version>`), such as `v0.4.5`.
3. GitHub Actions builds `dist/*` and attaches artifacts to that release.

One-command option:

```bash
git bump patch --tag --push
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
