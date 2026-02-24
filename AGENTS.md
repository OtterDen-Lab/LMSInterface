# Repository Guidelines

## Project Structure & Module Organization
`lms_interface/` contains the library code. Key modules include `canvas_interface.py` (Canvas API integration), `backends.py`/`interfaces.py` (backend abstraction), `privacy.py` (FERPA-friendly redaction), and `helpers.py` (CLI helpers such as `cleanup-missing`).

Tests live in `lms_interface/tests/` and mirror module behavior (`test_canvas_interface.py`, `test_helpers.py`, `test_privacy.py`). Keep new tests in this directory so vendoring stays clean.

Repository utilities live in `scripts/`:
- `install_git_hooks.sh` installs local hooks and `git bump`.
- `git_bump.sh` bumps version, runs tests, updates `uv.lock`, and commits.
- `vendor_into_project.py` vendors this package into downstream tools.

## Build, Test, and Development Commands
- `uv sync --dev`: install runtime and dev dependencies from `pyproject.toml`/`uv.lock`.
- `uv run pytest -q`: run the full test suite (default `testpaths` is `lms_interface/tests`).
- `uv run ruff check lms_interface lms_interface/tests`: run lint/import-order checks.
- `uv run black lms_interface lms_interface/tests`: format code (88-char lines).
- `bash scripts/install_git_hooks.sh`: enable repo hooks (`.githooks/pre-commit`).
- `uv lock`: regenerate lockfile after dependency or version updates.

## Coding Style & Naming Conventions
Use Python 3.12 and 4-space indentation. Follow Black formatting (`line-length = 88`) and keep imports Ruff-clean (`I` rules enabled). Use `snake_case` for functions/variables, `PascalCase` for classes, and `test_*.py` for test modules.

Prefer small, focused functions and explicit names around LMS concepts (`CanvasCourse`, `PrivacyBackend`, etc.).

## Testing Guidelines
Use `pytest` for all tests. Add regression tests for bug fixes and behavior changes, especially for Canvas request/response handling and privacy redaction paths.

Name tests clearly by behavior (for example, `test_cleanup_missing_skips_future_due_assignments`). Run `uv run pytest -q` before opening a PR.

## Commit & Pull Request Guidelines
Commit messages in this repo are short, imperative, and task-focused (for example, `Add LMS backend abstractions`, `Bump to version 0.4.4`).

PRs should follow `.github/pull_request_template.md`: include a clear description, change type, testing performed, and any relevant screenshots/notes. If `pyproject.toml` version changes, stage `uv.lock` in the same commit (enforced by pre-commit hook).

## Security & Configuration Tips
Store Canvas credentials in environment variables (`CANVAS_API_URL`, `CANVAS_API_KEY`, plus optional `_prod` variants). Never commit secrets or real student-identifying data.
