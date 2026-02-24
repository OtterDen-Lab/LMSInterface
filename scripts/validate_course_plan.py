#!/usr/bin/env python3
"""Validate a course plan YAML/JSON file against the repository schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _dependency_error(message: str) -> None:
    print(message, file=sys.stderr)
    print(
        "Hint: uv run --with pyyaml --with jsonschema "
        "python scripts/validate_course_plan.py <plan.yaml>",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _load_yaml_or_json(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix in {".json"}:
        return json.loads(text)

    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        _dependency_error(
            "PyYAML is required to parse .yaml/.yml files but is not installed."
        )
    return yaml.safe_load(text)


def _path_to_string(error_path) -> str:
    parts = []
    for part in error_path:
        if isinstance(part, int):
            parts.append(f"[{part}]")
        else:
            parts.append(f".{part}")
    if not parts:
        return "$"
    return "$" + "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a course plan file against schemas/course_plan.schema.yaml."
    )
    parser.add_argument("plan", help="Path to plan YAML/JSON file to validate.")
    parser.add_argument(
        "--schema",
        default="schemas/course_plan.schema.yaml",
        help="Path to JSON Schema YAML/JSON file (default: schemas/course_plan.schema.yaml).",
    )
    args = parser.parse_args()

    plan_path = Path(args.plan)
    schema_path = Path(args.schema)

    try:
        plan = _load_yaml_or_json(plan_path)
        schema = _load_yaml_or_json(schema_path)
    except json.JSONDecodeError as exc:
        print(f"JSON parse error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Failed to load input files: {exc}", file=sys.stderr)
        return 2

    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-not-found]
        from jsonschema.exceptions import best_match  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        _dependency_error("jsonschema is required but is not installed.")

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(plan), key=lambda e: list(e.path))

    if not errors:
        print(f"VALID: {plan_path} matches {schema_path}")
        return 0

    print(f"INVALID: {plan_path} does not match {schema_path}")
    print(f"{len(errors)} validation error(s):")
    for err in errors:
        print(f"- {_path_to_string(err.path)}: {err.message}")
        context = best_match(err.context) if err.context else None
        if context is not None:
            print(f"  detail: {context.message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
