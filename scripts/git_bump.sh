#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  git bump [patch|minor|major] [-m "commit message"] [--no-commit] [--dry-run] [--skip-tests] [--tag] [--push] [--remote <name>]

Behavior:
  1. Run tests (unless --skip-tests)
  2. Bump version via `uv version --bump <kind>`
  3. Stage `pyproject.toml` and `uv.lock`
  4. Commit (unless --no-commit)
  5. Optionally create tag `v<version>` (with --tag)
  6. Optionally push branch and tag (with --push)

Notes:
  - Requires a clean index and working tree (tracked files).
  - Uses normal `git commit -m ...` (no pathspec commit).
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ $*"
    return 0
  fi
  "$@"
}

BUMP_KIND="patch"
COMMIT_MESSAGE=""
NO_COMMIT="0"
DRY_RUN="0"
SKIP_TESTS="0"
CREATE_TAG="0"
PUSH_CHANGES="0"
REMOTE_NAME="origin"
TEST_COMMAND='uv run pytest -q'

while [[ $# -gt 0 ]]; do
  case "$1" in
    patch|minor|major)
      BUMP_KIND="$1"
      shift
      ;;
    -m|--message)
      shift
      [[ $# -gt 0 ]] || die "Missing value for --message"
      COMMIT_MESSAGE="$1"
      shift
      ;;
    --no-commit)
      NO_COMMIT="1"
      shift
      ;;
    --dry-run)
      DRY_RUN="1"
      shift
      ;;
    --skip-tests)
      SKIP_TESTS="1"
      shift
      ;;
    --tag)
      CREATE_TAG="1"
      shift
      ;;
    --push)
      PUSH_CHANGES="1"
      shift
      ;;
    --remote)
      shift
      [[ $# -gt 0 ]] || die "Missing value for --remote"
      REMOTE_NAME="$1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [[ -n "$(git diff --name-only)" ]] || [[ -n "$(git diff --cached --name-only)" ]]; then
  die "Working tree has tracked changes. Commit or stash them before running git bump."
fi

if [[ "$SKIP_TESTS" != "1" ]] && [[ -n "$TEST_COMMAND" ]]; then
  echo "Running tests: $TEST_COMMAND"
  run bash -lc "$TEST_COMMAND"
fi

run uv version --bump "$BUMP_KIND"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)"
run git add pyproject.toml uv.lock

if [[ "$NO_COMMIT" == "1" ]]; then
  if [[ "$CREATE_TAG" == "1" || "$PUSH_CHANGES" == "1" ]]; then
    die "--tag and --push require a commit. Remove --no-commit."
  fi
  echo "Staged version bump updates (no commit created)."
  exit 0
fi

if [[ -z "$COMMIT_MESSAGE" ]]; then
  COMMIT_MESSAGE="Bump to version ${version}"
  run git commit -e -m "$COMMIT_MESSAGE"
else
  run git commit -m "$COMMIT_MESSAGE"
fi

if [[ "$CREATE_TAG" == "1" ]]; then
  tag_name="v${version}"
  if git rev-parse -q --verify "refs/tags/${tag_name}" >/dev/null; then
    die "Tag ${tag_name} already exists."
  fi
  run git tag "${tag_name}"
fi

if [[ "$PUSH_CHANGES" == "1" ]]; then
  branch_name="$(git rev-parse --abbrev-ref HEAD)"
  run git push "$REMOTE_NAME" "$branch_name"
  if [[ "$CREATE_TAG" == "1" ]]; then
    run git push "$REMOTE_NAME" "$tag_name"
  fi
fi
