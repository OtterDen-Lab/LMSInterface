from __future__ import annotations

import copy
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit, urlunsplit

import canvasapi

from lms_interface.canvas_interface import CanvasCourse

log = logging.getLogger(__name__)

WEEKDAY_TO_INT = {
    "Mon": 0,
    "Tue": 1,
    "Wed": 2,
    "Thu": 3,
    "Fri": 4,
    "Sat": 5,
    "Sun": 6,
}


@dataclass
class CalendarBuildResult:
    normalized_plan: dict[str, Any]
    calendar_json: dict[str, Any]
    calendar_html: str
    warnings: list[str]
    output_paths: dict[str, Path]
    publish_result: dict[str, Any] | None


def _load_yaml_module():
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyYAML is required for course-plan workflows. "
            "Install dependencies (e.g., `uv sync --dev`)."
        ) from exc
    return yaml


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO date string, got: {value!r}")
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        return []
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def _slugify(value: str) -> str:
    s = value.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def infer_title_from_id(topic_id: str) -> str:
    text = topic_id.strip().replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    tokens = text.split(" ")
    normalized_tokens: list[str] = []
    for token in tokens:
        lower = token.lower()
        if lower.startswith("ostep") and any(ch.isdigit() for ch in lower):
            normalized_tokens.append(lower[:5].upper() + lower[5:])
        elif token.isupper():
            normalized_tokens.append(token)
        else:
            normalized_tokens.append(token.capitalize())
    return " ".join(normalized_tokens).strip()


def infer_resource_title(url: str) -> str:
    parsed = urlparse(url)
    leaf = unquote(parsed.path.rstrip("/").split("/")[-1]).strip()
    if leaf:
        return leaf
    domain = parsed.netloc or "resource"
    return domain


def _canonicalize_http_url(url_value: str) -> str:
    parsed = urlsplit(url_value)
    if parsed.scheme not in {"http", "https"}:
        return url_value
    path = quote(unquote(parsed.path), safe="/:@!$&'()*+,;=-._~")
    query = quote(unquote(parsed.query), safe="=&:@!$'()*+,;/?-._~")
    fragment = quote(unquote(parsed.fragment), safe="=&:@!$'()*+,;/?-._~")
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, fragment))


def _resolve_resource_url(url_value: str, *, base_url: str | None) -> str:
    parsed = urlparse(url_value)
    if parsed.scheme in {"http", "https"}:
        return _canonicalize_http_url(url_value)
    if base_url:
        joined = urljoin(base_url.rstrip("/") + "/", url_value.lstrip("/"))
        return _canonicalize_http_url(joined)
    return url_value


def _normalize_resource_list(
    values: Any,
    *,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    if not values:
        return []
    normalized: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, str):
            resolved_url = _resolve_resource_url(item, base_url=base_url)
            normalized.append(
                {
                    "title": infer_resource_title(resolved_url),
                    "url": resolved_url,
                }
            )
            continue
        if not isinstance(item, dict):
            raise ValueError(f"Invalid resource item: {item!r}")
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"Resource is missing a valid url: {item!r}")
        resolved_url = _resolve_resource_url(url, base_url=base_url)
        entry: dict[str, Any] = {
            "title": item.get("title") or infer_resource_title(resolved_url),
            "url": resolved_url,
        }
        if "type" in item:
            entry["type"] = item["type"]
        if "required" in item:
            entry["required"] = bool(item["required"])
        normalized.append(entry)
    return normalized


def load_course_plan(plan_path: str | Path) -> dict[str, Any]:
    yaml = _load_yaml_module()
    raw = yaml.safe_load(Path(plan_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Course plan must be a mapping/object at the top level.")
    return raw


def normalize_course_plan(raw_plan: dict[str, Any]) -> dict[str, Any]:
    plan = copy.deepcopy(raw_plan)

    term_raw = plan.get("term") or {}
    term_start = _parse_date(term_raw["start_date"])
    term_end = _parse_date(term_raw["end_date"])
    global_no_class_dates = {
        _parse_date(value) for value in term_raw.get("global_no_class_dates", [])
    }

    breaks: list[dict[str, Any]] = []
    for break_raw in term_raw.get("breaks", []):
        applies_to = break_raw.get("applies_to")
        breaks.append(
            {
                "name": break_raw.get("name", "Break"),
                "kind": break_raw.get("kind", "break"),
                "start_date": _parse_date(break_raw["start_date"]),
                "end_date": _parse_date(break_raw["end_date"]),
                "applies_to": set(applies_to) if applies_to else None,
                "notes": break_raw.get("notes"),
            }
        )

    sections: list[dict[str, Any]] = []
    for section_raw in plan.get("sections", []):
        day_values = section_raw.get("meeting_days", [])
        meeting_days = [WEEKDAY_TO_INT[value] for value in day_values]
        sections.append(
            {
                "id": section_raw["id"],
                "name": section_raw.get("name", section_raw["id"]),
                "meeting_days": sorted(set(meeting_days)),
                "meeting_day_labels": day_values,
                "canvas_course_id": section_raw.get("canvas_course_id"),
            }
        )

    exam_coverage: dict[str, list[str]] = {}
    raw_exam_coverage = plan.get("exam_coverage", {}) or {}
    for exam_name, topic_ids in raw_exam_coverage.items():
        exam_coverage[exam_name] = list(topic_ids)

    resource_defaults_raw = plan.get("resource_defaults") or {}
    resource_defaults = {
        "lecture_slides_base_url": resource_defaults_raw.get("lecture_slides_base_url"),
        "readings_base_url": resource_defaults_raw.get("readings_base_url"),
        "resources_base_url": resource_defaults_raw.get("resources_base_url"),
    }

    placeholders_raw = plan.get("placeholders") or {}
    if not isinstance(placeholders_raw, dict):
        raise ValueError("`placeholders` must be a mapping when provided.")

    used_topic_ids: dict[str, int] = {}
    placeholder_counters: dict[str, int] = {}

    topics: list[dict[str, Any]] = []
    for topic_raw in plan.get("topics", []):
        working_topic = copy.deepcopy(topic_raw)

        placeholder_key = working_topic.pop("placeholder", None)
        if placeholder_key:
            template = placeholders_raw.get(placeholder_key)
            if not isinstance(template, dict):
                raise ValueError(
                    f"Topic placeholder '{placeholder_key}' is not defined in top-level `placeholders`."
                )
            merged_topic = copy.deepcopy(template)
            merged_topic.update(working_topic)
            working_topic = merged_topic
            if "id" not in working_topic:
                placeholder_slug = _slugify(str(placeholder_key))
                placeholder_counters[placeholder_slug] = (
                    placeholder_counters.get(placeholder_slug, 0) + 1
                )
                working_topic["id"] = (
                    f"{placeholder_slug}-{placeholder_counters[placeholder_slug]}"
                )

        topic_id = str(working_topic.get("id") or "").strip()
        if not topic_id:
            title_value = str(working_topic.get("title") or "").strip()
            if not title_value:
                raise ValueError("Each topic requires either `id` or `title`.")
            topic_id = _slugify(title_value)
        used_topic_ids[topic_id] = used_topic_ids.get(topic_id, 0) + 1
        if used_topic_ids[topic_id] > 1:
            topic_id = f"{topic_id}-{used_topic_ids[topic_id]}"

        title = working_topic.get("title") or infer_title_from_id(topic_id)
        meetings = int(working_topic.get("meetings", 1))
        duration_hours = int(
            working_topic.get("duration_hours", working_topic.get("hours", meetings))
        )
        topic = {
            "id": topic_id,
            "title": str(title).strip(),
            "meetings": meetings,
            "duration_hours": max(1, duration_hours),
            "new_material": bool(working_topic.get("new_material", True)),
            "lecture_slides": _normalize_resource_list(
                working_topic.get("lecture_slides"),
                base_url=resource_defaults.get("lecture_slides_base_url"),
            ),
            "readings": _normalize_resource_list(
                working_topic.get("readings"),
                base_url=resource_defaults.get("readings_base_url"),
            ),
            "resources": _normalize_resource_list(
                working_topic.get("resources"),
                base_url=resource_defaults.get("resources_base_url"),
            ),
            "notes": working_topic.get("notes"),
            "tags": list(working_topic.get("tags", [])),
        }
        topics.append(topic)
        for exam_name in working_topic.get("appears_on", []) or []:
            exam_coverage.setdefault(exam_name, []).append(topic_id)

    sync_raw = plan.get("sync") or {}
    topics_per_meeting = int(
        sync_raw.get("topics_per_meeting", sync_raw.get("hours_per_meeting", 1))
    )
    sync = {
        "mode": sync_raw.get("mode", "lockstep_by_topic"),
        "skip_for_all_if_any_section_skips": bool(
            sync_raw.get("skip_for_all_if_any_section_skips", True)
        ),
        "carry_over_policy": sync_raw.get("carry_over_policy", "defer_topic"),
        "topics_per_meeting": max(1, topics_per_meeting),
        "hours_per_meeting": max(1, topics_per_meeting),
    }

    publishing_raw = plan.get("publishing") or {}
    weekly_slides_indent = int(publishing_raw.get("weekly_slides_indent", 1))
    publishing = {
        "module_name_template": publishing_raw.get(
            "module_name_template",
            "Week {week_number}",
        ),
        "schedule_page_title": publishing_raw.get(
            "schedule_page_title",
            "Course Schedule",
        ),
        "create_calendar_html": bool(publishing_raw.get("create_calendar_html", True)),
        "weekly_slides_title_prefix": str(
            publishing_raw.get("weekly_slides_title_prefix", "Slides: ")
        ),
        "weekly_slides_indent": max(0, weekly_slides_indent),
        "weekly_slides_section_header": (
            str(publishing_raw["weekly_slides_section_header"]).strip()
            if publishing_raw.get("weekly_slides_section_header") is not None
            else "Slides"
        ),
        "weekly_slides_prune_existing": bool(
            publishing_raw.get("weekly_slides_prune_existing", True)
        ),
    }

    exam_rules_raw = plan.get("exam_rules") or {}
    exam_defaults_raw = exam_rules_raw.get("defaults") or {}
    exam_rules = {
        "schedule_mode": exam_rules_raw.get("schedule_mode", "derived"),
        "defaults": {
            "class_meeting_index_in_week": int(
                exam_defaults_raw.get("class_meeting_index_in_week", 2)
            ),
            "min_class_meetings_after_last_new_material": int(
                exam_defaults_raw.get("min_class_meetings_after_last_new_material", 1)
            ),
            "prefer_before_break": bool(
                exam_defaults_raw.get("prefer_before_break", True)
            ),
            "require_before_break": bool(
                exam_defaults_raw.get("require_before_break", False)
            ),
        },
        "groups": list(exam_rules_raw.get("groups", [])),
    }

    return {
        "version": str(plan.get("version", "1.1")),
        "term": {
            "start_date": term_start,
            "end_date": term_end,
            "timezone": term_raw.get("timezone", "America/Los_Angeles"),
            "global_no_class_dates": global_no_class_dates,
            "breaks": breaks,
        },
        "sections": sections,
        "sync": sync,
        "topics": topics,
        "placeholders": placeholders_raw,
        "exam_rules": exam_rules,
        "exam_coverage": exam_coverage,
        "resource_defaults": resource_defaults,
        "publishing": publishing,
    }


def _blocked_dates_for_section(term: dict[str, Any], section_id: str) -> set[date]:
    blocked = set(term["global_no_class_dates"])
    for break_period in term["breaks"]:
        applies_to = break_period["applies_to"]
        if applies_to and section_id not in applies_to:
            continue
        blocked.update(
            _date_range(break_period["start_date"], break_period["end_date"])
        )
    return blocked


def _section_slots(plan: dict[str, Any]) -> dict[str, dict[tuple[date, int], date]]:
    term = plan["term"]
    start_date = term["start_date"]
    end_date = term["end_date"]
    slots_by_section: dict[str, dict[tuple[date, int], date]] = {}

    for section in plan["sections"]:
        section_id = section["id"]
        blocked_dates = _blocked_dates_for_section(term, section_id)
        meeting_days = set(section["meeting_days"])

        weekly_dates: dict[date, list[date]] = {}
        for day in _date_range(start_date, end_date):
            if day.weekday() not in meeting_days:
                continue
            if day in blocked_dates:
                continue
            week_start = day - timedelta(days=day.weekday())
            weekly_dates.setdefault(week_start, []).append(day)

        section_slots: dict[tuple[date, int], date] = {}
        for week_start, dates in weekly_dates.items():
            for idx, meeting_date in enumerate(sorted(dates), start=1):
                section_slots[(week_start, idx)] = meeting_date
        slots_by_section[section_id] = section_slots

    return slots_by_section


def _next_break_start_after(term: dict[str, Any], pivot: date) -> date | None:
    starts = []
    for break_period in term["breaks"]:
        start_date = break_period["start_date"]
        if start_date > pivot:
            starts.append(start_date)
    return min(starts) if starts else None


def _no_class_labels_for_date(
    term: dict[str, Any],
    *,
    section_id: str,
    target_date: date,
) -> list[str]:
    labels: list[str] = []
    if target_date in term["global_no_class_dates"]:
        labels.append("No class")
    for break_period in term["breaks"]:
        applies_to = break_period["applies_to"]
        if applies_to and section_id not in applies_to:
            continue
        if break_period["start_date"] <= target_date <= break_period["end_date"]:
            labels.append(str(break_period["name"]))
    return labels


def _normalize_no_class_label(label: str) -> str:
    normalized = str(label).strip()
    # Keep break labels concise in schedule notices (e.g., "Break week 11" -> "Break week").
    normalized = re.sub(r"(?i)\bbreak week\s+\d+\b", "Break week", normalized)
    return normalized


def _slot_index_for_section_date(
    slots: list[dict[str, Any]],
    *,
    section_id: str,
    meeting_date: date,
) -> int | None:
    for idx, slot in enumerate(slots):
        if slot["dates"].get(section_id) == meeting_date:
            return idx
    return None


def build_schedule(plan: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    slots_by_section = _section_slots(plan)
    sections = plan["sections"]
    section_ids = [section["id"] for section in sections]
    key_sets = [set(slots_by_section[sid].keys()) for sid in section_ids]

    if not key_sets:
        raise ValueError("At least one section is required.")

    sync_mode = plan["sync"]["mode"]
    skip_all = plan["sync"]["skip_for_all_if_any_section_skips"]
    if sync_mode == "lockstep_by_topic":
        if skip_all and len(key_sets) > 1:
            selected_keys = set.intersection(*key_sets)
        else:
            selected_keys = set.union(*key_sets)
    else:
        selected_keys = key_sets[0]

    sorted_keys = sorted(selected_keys, key=lambda key: (key[0], key[1]))
    week_numbers: dict[date, int] = {}
    for key in sorted_keys:
        week_start = key[0]
        if week_start not in week_numbers:
            week_numbers[week_start] = len(week_numbers) + 1

    slots: list[dict[str, Any]] = []
    for key in sorted_keys:
        week_start, slot_in_week = key
        slot = {
            "week_start": week_start,
            "week_number": week_numbers[week_start],
            "slot_in_week": slot_in_week,
            "dates": {
                section_id: slots_by_section[section_id].get(key)
                for section_id in section_ids
            },
            "allocations": [],
            "topic": None,
        }
        slots.append(slot)

    hours_per_meeting = plan["sync"]["topics_per_meeting"]

    exam_defaults = plan["exam_rules"]["defaults"]
    exam_groups = list(plan["exam_rules"]["groups"])
    if not exam_groups and plan["exam_coverage"]:
        for exam_name, topics in plan["exam_coverage"].items():
            if not topics:
                continue
            exam_groups.append(
                {
                    "name": exam_name,
                    "included_topics": topics,
                }
            )

    exam_warnings: list[str] = []
    exam_dates: dict[str, dict[str, str]] = {}
    exam_slots_by_name: dict[str, set[int]] = {}
    blocked_slot_indices: set[int] = set()

    for group in exam_groups:
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        group_slots: set[int] = set()
        section_exam_dates: dict[str, str] = {}
        fixed_dates = group.get("fixed_date_overrides", {}) or {}
        for section_id, raw_date in fixed_dates.items():
            if section_id not in section_ids:
                exam_warnings.append(
                    f"Exam '{name}' has fixed_date_overrides for unknown section '{section_id}'."
                )
                continue
            parsed_date = _parse_date(raw_date)
            section_exam_dates[section_id] = parsed_date.isoformat()
            slot_idx_for_date = _slot_index_for_section_date(
                slots,
                section_id=section_id,
                meeting_date=parsed_date,
            )
            if slot_idx_for_date is not None:
                group_slots.add(slot_idx_for_date)

        if section_exam_dates:
            exam_dates[name] = section_exam_dates
        if group_slots:
            exam_slots_by_name[name] = set(group_slots)
            blocked_slot_indices.update(group_slots)

    def _allocate_topics(
        blocked_indices: set[int],
    ) -> tuple[dict[str, int], list[str]]:
        local_warnings: list[str] = []
        topic_slot_index: dict[str, int] = {}
        for slot in slots:
            slot["allocations"] = []
            slot["topic"] = None

        slot_idx = 0
        slot_hours_remaining = hours_per_meeting
        for topic in plan["topics"]:
            remaining_topic_hours = max(1, int(topic.get("duration_hours", 1)))
            while remaining_topic_hours > 0:
                while slot_idx < len(slots) and slot_idx in blocked_indices:
                    slot_idx += 1
                    slot_hours_remaining = hours_per_meeting

                if slot_idx >= len(slots):
                    local_warnings.append(
                        f"Topic '{topic['id']}' did not fit in available meeting slots ({remaining_topic_hours}h unscheduled)."
                    )
                    break

                allocation_hours = min(remaining_topic_hours, slot_hours_remaining)
                slots[slot_idx]["allocations"].append(
                    {
                        "topic": topic,
                        "hours": allocation_hours,
                    }
                )
                slots[slot_idx]["topic"] = slots[slot_idx]["allocations"][0]["topic"]
                topic_slot_index[topic["id"]] = slot_idx

                remaining_topic_hours -= allocation_hours
                slot_hours_remaining -= allocation_hours
                if slot_hours_remaining == 0:
                    slot_idx += 1
                    slot_hours_remaining = hours_per_meeting

        return topic_slot_index, local_warnings

    topic_slot_index, allocation_warnings = _allocate_topics(blocked_slot_indices)

    for group in exam_groups:
        name = str(group.get("name") or "").strip()
        if not name:
            continue

        section_exam_dates = dict(exam_dates.get(name, {}))
        group_exam_slots = set(exam_slots_by_name.get(name, set()))
        if len(section_exam_dates) < len(section_ids) and group_exam_slots:
            anchor_slot = slots[min(group_exam_slots)]
            for section_id in section_ids:
                if section_id in section_exam_dates:
                    continue
                meeting_date = anchor_slot["dates"].get(section_id)
                if meeting_date is not None:
                    section_exam_dates[section_id] = meeting_date.isoformat()

        coverage_topics = list(group.get("included_topics", []))
        if not coverage_topics and name in plan["exam_coverage"]:
            coverage_topics = list(plan["exam_coverage"][name])
        through_topic = group.get("through_topic")
        if through_topic:
            coverage_topics.append(through_topic)

        last_slot_index = None
        for topic_id in coverage_topics:
            slot_idx = topic_slot_index.get(topic_id)
            if slot_idx is not None:
                if last_slot_index is None or slot_idx > last_slot_index:
                    last_slot_index = slot_idx

        if len(section_exam_dates) < len(section_ids):
            if last_slot_index is None:
                exam_warnings.append(
                    f"Exam '{name}' could not be derived (no through_topic/included_topics matched scheduled topics)."
                )
            else:
                min_gap = int(
                    group.get(
                        "min_class_meetings_after_last_new_material",
                        exam_defaults["min_class_meetings_after_last_new_material"],
                    )
                )
                meeting_index = int(
                    group.get(
                        "class_meeting_index_in_week",
                        exam_defaults["class_meeting_index_in_week"],
                    )
                )
                earliest_idx = last_slot_index + min_gap + 1
                candidate_indices = [
                    idx
                    for idx in range(earliest_idx, len(slots))
                    if idx not in blocked_slot_indices
                    if slots[idx]["slot_in_week"] == meeting_index
                ]
                if not candidate_indices:
                    candidate_indices = [
                        idx
                        for idx in range(earliest_idx, len(slots))
                        if idx not in blocked_slot_indices
                    ]

                prefer_before_break = bool(
                    group.get(
                        "prefer_before_break", exam_defaults["prefer_before_break"]
                    )
                )
                require_before_break = bool(
                    group.get(
                        "require_before_break", exam_defaults["require_before_break"]
                    )
                )
                if candidate_indices and (prefer_before_break or require_before_break):
                    pivot_date = min(
                        d
                        for d in slots[last_slot_index]["dates"].values()
                        if d is not None
                    )
                    next_break_start = _next_break_start_after(plan["term"], pivot_date)
                    if next_break_start is not None:
                        before_break = []
                        for idx in candidate_indices:
                            slot_dates = [
                                d for d in slots[idx]["dates"].values() if d is not None
                            ]
                            if slot_dates and max(slot_dates) < next_break_start:
                                before_break.append(idx)
                        if before_break:
                            candidate_indices = before_break
                        elif require_before_break:
                            exam_warnings.append(
                                f"Exam '{name}' requires placement before break starting {next_break_start.isoformat()}, but no slot matched."
                            )

                if candidate_indices:
                    chosen_idx = candidate_indices[0]
                    chosen_slot = slots[chosen_idx]
                    group_exam_slots.add(chosen_idx)
                    for section_id in section_ids:
                        if section_id in section_exam_dates:
                            continue
                        meeting_date = chosen_slot["dates"].get(section_id)
                        if meeting_date is not None:
                            section_exam_dates[section_id] = meeting_date.isoformat()
                    if chosen_idx not in blocked_slot_indices:
                        blocked_slot_indices.add(chosen_idx)
                        topic_slot_index, allocation_warnings = _allocate_topics(
                            blocked_slot_indices
                        )
                else:
                    exam_warnings.append(
                        f"Exam '{name}' could not be scheduled; no remaining candidate slots."
                    )

        if section_exam_dates:
            exam_dates[name] = section_exam_dates
        if group_exam_slots:
            exam_slots_by_name[name] = group_exam_slots

    warnings.extend(allocation_warnings)
    warnings.extend(exam_warnings)

    topic_to_exams: dict[str, set[str]] = {}
    for exam_name, topic_ids in plan["exam_coverage"].items():
        for topic_id in topic_ids:
            topic_to_exams.setdefault(topic_id, set()).add(exam_name)

    rows = []
    slot_exam_lookup: dict[int, list[str]] = {}
    for exam_name, slot_indices in exam_slots_by_name.items():
        for idx in slot_indices:
            slot_exam_lookup.setdefault(idx, []).append(exam_name)

    for idx, slot in enumerate(slots):
        allocations = slot.get("allocations", [])
        primary_topic = allocations[0]["topic"] if allocations else None
        exam_names = sorted(slot_exam_lookup.get(idx, []))
        readings: list[dict[str, Any]] = []
        lecture_slides: list[dict[str, Any]] = []
        resources: list[dict[str, Any]] = []
        topic_ids: list[str] = []
        topic_titles: list[str] = []
        topic_allocations: list[dict[str, Any]] = []
        coverage_exams: set[str] = set()
        for allocation in allocations:
            topic = allocation["topic"]
            hours = int(allocation["hours"])
            topic_ids.append(topic["id"])
            topic_exam_names = sorted(topic_to_exams.get(topic["id"], set()))
            coverage_exams.update(topic_exam_names)
            readings.extend(topic["readings"])
            lecture_slides.extend(topic["lecture_slides"])
            resources.extend(topic["resources"])
            label = topic["title"] if hours == 1 else f"{topic['title']} ({hours}h)"
            topic_titles.append(label)
            topic_allocations.append(
                {
                    "topic_id": topic["id"],
                    "topic_title": topic["title"],
                    "hours": hours,
                }
            )
        rows.append(
            {
                "week_number": slot["week_number"],
                "slot_in_week": slot["slot_in_week"],
                "dates": {
                    sid: value.isoformat() if value else None
                    for sid, value in slot["dates"].items()
                },
                "topic_ids": topic_ids,
                "topic_titles": topic_titles,
                "topic_allocations": topic_allocations,
                "topic_id": primary_topic["id"] if primary_topic else None,
                "topic_title": primary_topic["title"] if primary_topic else None,
                "exam_names": exam_names,
                "coverage_exams": sorted(coverage_exams),
                "readings": readings,
                "lecture_slides": lecture_slides,
                "resources": resources,
                "new_material": any(
                    bool(allocation["topic"]["new_material"])
                    for allocation in allocations
                ),
                "no_class": False,
                "no_class_label": None,
                "_week_start": slot["week_start"],
            }
        )

    existing_slot_keys = {(slot["week_start"], slot["slot_in_week"]) for slot in slots}
    no_class_rows_by_key: dict[tuple[date, int], dict[str, Any]] = {}
    term_start = plan["term"]["start_date"]
    term_end = plan["term"]["end_date"]
    for section in sections:
        section_id = section["id"]
        day_to_slot_in_week = {
            day_value: idx for idx, day_value in enumerate(section["meeting_days"], start=1)
        }
        blocked_dates = sorted(_blocked_dates_for_section(plan["term"], section_id))
        for blocked_date in blocked_dates:
            if blocked_date < term_start or blocked_date > term_end:
                continue
            slot_in_week = day_to_slot_in_week.get(blocked_date.weekday())
            if slot_in_week is None:
                continue
            week_start = blocked_date - timedelta(days=blocked_date.weekday())
            key = (week_start, slot_in_week)
            if key in existing_slot_keys:
                continue

            candidate_row = no_class_rows_by_key.get(key)
            if candidate_row is None:
                candidate_row = {
                    "week_number": 0,
                    "slot_in_week": slot_in_week,
                    "dates": {sid: None for sid in section_ids},
                    "topic_ids": [],
                    "topic_titles": [],
                    "topic_allocations": [],
                    "topic_id": None,
                    "topic_title": None,
                    "exam_names": [],
                    "coverage_exams": [],
                    "readings": [],
                    "lecture_slides": [],
                    "resources": [],
                    "new_material": False,
                    "no_class": True,
                    "no_class_label": "No class",
                    "_week_start": week_start,
                    "_labels": set(),
                }
                no_class_rows_by_key[key] = candidate_row

            candidate_row["dates"][section_id] = blocked_date.isoformat()
            for label in _no_class_labels_for_date(
                plan["term"],
                section_id=section_id,
                target_date=blocked_date,
            ):
                candidate_row["_labels"].add(label)

    for candidate_row in no_class_rows_by_key.values():
        labels = sorted(candidate_row.pop("_labels"))
        if labels:
            if labels == ["No class"]:
                candidate_row["no_class_label"] = "No class"
            else:
                normalized_labels = [
                    _normalize_no_class_label(label)
                    for label in labels
                    if label != "No class"
                ]
                candidate_row["no_class_label"] = (
                    "No class: " + ", ".join(sorted(set(normalized_labels or labels)))
                )
        rows.append(candidate_row)

    exams_with_slot_rows = {
        exam_name for exam_names in slot_exam_lookup.values() for exam_name in exam_names
    }
    week_numbers_with_exams = dict(week_numbers)
    next_week_number = len(week_numbers_with_exams) + 1
    for exam_name, section_dates in exam_dates.items():
        if exam_name in exams_with_slot_rows:
            continue
        parsed_dates: dict[str, date] = {}
        for section_id, iso_date in section_dates.items():
            try:
                parsed_dates[section_id] = _parse_date(iso_date)
            except Exception:
                continue
        if not parsed_dates:
            continue

        earliest_exam_date = min(parsed_dates.values())
        exam_week_start = earliest_exam_date - timedelta(days=earliest_exam_date.weekday())
        if exam_week_start not in week_numbers_with_exams:
            week_numbers_with_exams[exam_week_start] = next_week_number
            next_week_number += 1
        rows.append(
            {
                "week_number": week_numbers_with_exams[exam_week_start],
                "slot_in_week": "Exam",
                "dates": {
                    sid: section_dates.get(sid)
                    for sid in section_ids
                },
                "topic_ids": [],
                "topic_titles": [],
                "topic_allocations": [],
                "topic_id": None,
                "topic_title": None,
                "exam_names": [exam_name],
                "coverage_exams": [],
                "readings": [],
                "lecture_slides": [],
                "resources": [],
                "new_material": False,
                "no_class": False,
                "no_class_label": None,
                "_week_start": exam_week_start,
            }
        )

    week_starts_in_order = sorted(
        {
            row["_week_start"]
            for row in rows
            if not bool(row.get("no_class"))
        },
    )
    week_number_by_start = {
        week_start: idx for idx, week_start in enumerate(week_starts_in_order, start=1)
    }
    for row in rows:
        row["week_number"] = week_number_by_start.get(row["_week_start"])

    rows.sort(
        key=lambda row: (
            row["_week_start"],
            0 if isinstance(row.get("slot_in_week"), int) else 1,
            row.get("slot_in_week"),
        )
    )

    grouped_no_class_rows: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        if bool(row.get("no_class")) and row.get("week_number") is None:
            grouped_no_class_rows.setdefault(row["_week_start"], []).append(row)

    filtered_rows: list[dict[str, Any]] = []
    emitted_no_class_weeks: set[date] = set()
    for row in rows:
        week_start = row["_week_start"]
        if bool(row.get("no_class")) and row.get("week_number") is None:
            if week_start in emitted_no_class_weeks:
                continue
            emitted_no_class_weeks.add(week_start)
            group_rows = grouped_no_class_rows.get(week_start, [row])
            labels = [
                str(group_row.get("no_class_label") or "No class")
                for group_row in group_rows
                if str(group_row.get("no_class_label") or "").strip()
            ]
            unique_labels = sorted(set(labels)) or ["No class"]
            all_dates = sorted(
                {
                    date_value
                    for group_row in group_rows
                    for date_value in group_row.get("dates", {}).values()
                    if date_value
                }
            )
            notice_label = unique_labels[0]
            if len(unique_labels) > 1:
                notice_label = "No class"
            filtered_rows.append(
                {
                    "week_number": None,
                    "slot_in_week": "",
                    "dates": {sid: None for sid in section_ids},
                    "topic_ids": [],
                    "topic_titles": [],
                    "topic_allocations": [],
                    "topic_id": None,
                    "topic_title": None,
                    "exam_names": [],
                    "coverage_exams": [],
                    "readings": [],
                    "lecture_slides": [],
                    "resources": [],
                    "new_material": False,
                    "no_class": True,
                    "no_class_notice": True,
                    "no_class_label": notice_label,
                    "no_class_dates": all_dates,
                }
            )
            continue
        filtered_rows.append(row)

    for row in filtered_rows:
        row.pop("_week_start", None)

    schedule = {
        "sections": sections,
        "rows": filtered_rows,
        "exam_dates": exam_dates,
        "term": {
            "start_date": plan["term"]["start_date"].isoformat(),
            "end_date": plan["term"]["end_date"].isoformat(),
            "timezone": plan["term"]["timezone"],
            "global_no_class_dates": [
                d.isoformat()
                for d in sorted(plan["term"]["global_no_class_dates"])
            ],
            "breaks": [
                {
                    "name": b["name"],
                    "kind": b["kind"],
                    "start_date": b["start_date"].isoformat(),
                    "end_date": b["end_date"].isoformat(),
                    "applies_to": (
                        sorted(list(b["applies_to"])) if b["applies_to"] else None
                    ),
                }
                for b in plan["term"]["breaks"]
            ],
        },
    }
    return schedule, warnings


def _render_resource_links(resources: list[dict[str, Any]]) -> str:
    if not resources:
        return ""
    links = []
    for resource in resources:
        label = html.escape(
            str(resource.get("title") or infer_resource_title(resource["url"]))
        )
        href = html.escape(str(resource["url"]), quote=True)
        links.append(
            f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>'
        )
    return "<br/>".join(links)


def render_calendar_html(
    normalized_plan: dict[str, Any],
    calendar_json: dict[str, Any],
) -> str:
    section_columns = calendar_json["sections"]
    show_other_resources = any(
        bool(row.get("resources"))
        for row in calendar_json.get("rows", [])
        if not bool(row.get("no_class_notice"))
    )

    break_items = []
    for break_period in calendar_json["term"]["breaks"]:
        break_name = _normalize_no_class_label(str(break_period["name"]))
        label = (
            f"{break_name}: {break_period['start_date']} to {break_period['end_date']}"
        )
        break_items.append(f"<li>{html.escape(label)}</li>")
    break_html = (
        "<ul>" + "".join(break_items) + "</ul>" if break_items else "<p>None</p>"
    )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    page_title = html.escape(normalized_plan["publishing"]["schedule_page_title"])
    today = date.today()
    cell_style = "border:1px solid #ccc;padding:8px;vertical-align:top;"
    header_style = (
        "border:1px solid #ccc;padding:8px;vertical-align:top;background:#f5f5f5;"
    )

    rows_html = []
    for row in calendar_json["rows"]:
        if bool(row.get("no_class_notice")):
            notice_text = str(row.get("no_class_label") or "No class")
            total_columns = 2 + len(section_columns) + 3 + (
                1 if show_other_resources else 0
            )
            rows_html.append(
                '<tr class="no-class-notice-row">'
                f'<td colspan="{total_columns}" style="{cell_style}"><strong>{html.escape(notice_text)}</strong></td>'
                "</tr>"
            )
            continue

        titles = row.get("topic_titles") or []
        exam_names = row.get("exam_names") or []
        coverage_exams = row.get("coverage_exams") or []
        no_class = bool(row.get("no_class"))
        if no_class:
            topic_title = f"<em>{html.escape(str(row.get('no_class_label') or 'No class'))}</em>"
        elif titles:
            topic_title = "<br/>".join(
                f"<strong>{html.escape(title)}</strong>" for title in titles
            )
        elif exam_names:
            topic_title = "<br/>".join(
                f"<strong>{html.escape(f'EXAM DAY: {exam}')}</strong>"
                for exam in exam_names
            )
        else:
            topic_title = html.escape(row.get("topic_title") or "")
        readings_html = _render_resource_links(row["readings"])
        slides_html = _render_resource_links(row["lecture_slides"])
        extra_html = _render_resource_links(row["resources"])
        if not readings_html:
            readings_html = "&nbsp;"
        if not slides_html:
            slides_html = "&nbsp;"
        if not extra_html:
            extra_html = "&nbsp;"

        row_dates = []
        for section in section_columns:
            section_id = section["id"]
            date_value = row["dates"].get(section_id)
            if date_value:
                try:
                    row_dates.append(_parse_date(date_value))
                except ValueError:
                    pass

        row_status_class = "upcoming-row"
        if row_dates:
            if max(row_dates) < today:
                row_status_class = "past-row"
            elif min(row_dates) <= today <= max(row_dates):
                row_status_class = "current-row"

        row_classes = [row_status_class]
        if no_class:
            row_classes.append("no-class-row")
        elif exam_names:
            row_classes.append("exam-row")

        coverage_html = ""
        if coverage_exams and not no_class and not exam_names:
            coverage_html = (
                '<div style="margin-top:6px;"><em>Included on: '
                + html.escape(", ".join(coverage_exams))
                + "</em></div>"
            )

        row_cell_style = cell_style
        topic_cell_style = cell_style
        if exam_names and not no_class:
            row_cell_style = (
                cell_style
                + "background:#ffe6c9;border-top:3px solid #2b6cb0;border-bottom:3px solid #2b6cb0;"
            )
            topic_cell_style = (
                row_cell_style
                + "font-size:16px;font-weight:700;letter-spacing:0.2px;"
            )

        cells = [
            f"<td style=\"{row_cell_style}\">{'' if row.get('week_number') is None else row['week_number']}</td>",
            f"<td style=\"{row_cell_style}\">{row['slot_in_week']}</td>",
        ]

        for section in section_columns:
            section_id = section["id"]
            date_value = row["dates"].get(section_id)
            if date_value:
                cells.append(
                    f"<td style=\"{row_cell_style}\">{html.escape(date_value)}</td>"
                )
            else:
                cells.append(
                    f'<td class="muted" style="{row_cell_style}">No class</td>'
                )

        cells.extend(
            [
                f"<td style=\"{topic_cell_style}\">{topic_title if topic_title else '&nbsp;'}{coverage_html}</td>",
                f"<td style=\"{row_cell_style}\">{readings_html}</td>",
                f"<td style=\"{row_cell_style}\">{slides_html}</td>",
            ]
        )
        if show_other_resources:
            cells.append(f"<td style=\"{row_cell_style}\">{extra_html}</td>")

        rows_html.append(
            f'<tr class="{" ".join(row_classes)}">' + "".join(cells) + "</tr>"
        )

    section_headers = "".join(
        f"<th style=\"{header_style}\">{html.escape(section['name'])}</th>"
        for section in section_columns
    )
    other_resources_header = (
        f"<th style=\"{header_style}\">Other Resources</th>"
        if show_other_resources
        else ""
    )

    return f"""<!-- course-plan-generated:start -->
<style>
.course-calendar {{
  --line: #cfd6dd;
  --past-bg: #f7f8fa;
  --current-bg: #e8f3ff;
  --upcoming-bg: #ffffff;
  --no-class-bg: #e6f7ea;
  --exam-bg: #ffe6c9;
  --accent: #2b6cb0;
  font-family: Arial, sans-serif;
  font-size: 14px;
}}
.course-calendar table {{ border-collapse: collapse; width: 100%; border: 1px solid var(--line); }}
.course-calendar .muted {{ color: #777; }}
.course-calendar .past-row td {{ background: var(--past-bg); }}
.course-calendar .current-row td {{ background: var(--current-bg); }}
.course-calendar .upcoming-row td {{ background: var(--upcoming-bg); }}
.course-calendar .no-class-row td {{
  background: var(--no-class-bg);
  border-top: 2px solid #91c49b !important;
  border-bottom: 2px solid #91c49b !important;
}}
.course-calendar .no-class-notice-row td {{
  background: #d9f0df;
  border-top: 3px solid #70a978 !important;
  border-bottom: 3px solid #70a978 !important;
  text-align: center;
}}
.course-calendar .exam-row td {{
  background: var(--exam-bg);
  border-top: 3px solid var(--accent) !important;
  border-bottom: 3px solid var(--accent) !important;
}}
.course-calendar .exam-badge {{
  display: inline-block;
  margin-left: 6px;
  padding: 2px 6px;
  background: #ffe8a3;
  border: 1px solid #e0c34d;
  border-radius: 4px;
  font-size: 12px;
}}
</style>
<div class="course-calendar">
  <h2>{page_title}</h2>
  <p><strong>Generated:</strong> {generated_at}</p>
  <p><strong>Term:</strong> {calendar_json['term']['start_date']} to {calendar_json['term']['end_date']} ({html.escape(calendar_json['term']['timezone'])})</p>
  <h3>Breaks</h3>
  {break_html}
  <h3>Schedule</h3>
  <table>
    <thead>
      <tr>
        <th style="{header_style}">Week</th>
        <th style="{header_style}">Slot</th>
        {section_headers}
        <th style="{header_style}">Topic</th>
        <th style="{header_style}">Readings</th>
        <th style="{header_style}">Slides</th>
        {other_resources_header}
      </tr>
    </thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
</div>
<!-- course-plan-generated:end -->
"""


def _find_page_by_title(canvas_course: CanvasCourse, title: str):
    for page in canvas_course.course.get_pages():
        if str(getattr(page, "title", "")).strip() == title.strip():
            return page
    return None


def _find_module_by_name(canvas_course: CanvasCourse, module_name: str):
    for module in canvas_course.course.get_modules():
        if str(getattr(module, "name", "")).strip() == module_name.strip():
            return module
    return None


def _build_weekly_slide_items(
    calendar_json: dict[str, Any],
    *,
    module_name_template: str,
) -> dict[str, list[dict[str, str]]]:
    weekly: dict[str, list[dict[str, str]]] = {}
    seen_by_module: dict[str, set[str]] = {}

    for row in calendar_json.get("rows", []):
        week_number = row.get("week_number")
        try:
            module_name = module_name_template.format(week_number=week_number)
        except Exception:
            module_name = f"Week {week_number}"
        weekly.setdefault(module_name, [])
        seen_by_module.setdefault(module_name, set())

        for slide in row.get("lecture_slides", []) or []:
            if not isinstance(slide, dict):
                continue
            url = str(slide.get("url") or "").strip()
            if not url:
                continue
            if url in seen_by_module[module_name]:
                continue
            seen_by_module[module_name].add(url)
            weekly[module_name].append(
                {
                    "title": str(slide.get("title") or infer_resource_title(url)),
                    "url": url,
                }
            )
    return weekly


def publish_calendar_to_canvas(
    canvas_course: CanvasCourse,
    *,
    calendar_json: dict[str, Any],
    page_title: str,
    module_name: str | None,
    publish_weekly_slides: bool,
    weekly_module_template: str,
    weekly_slides_title_prefix: str,
    weekly_slides_indent: int,
    weekly_slides_section_header: str | None,
    weekly_slides_prune_existing: bool,
    lecture_slides_base_url: str | None,
    html_body: str,
    dry_run: bool,
) -> dict[str, Any]:
    actions: list[str] = []
    page_url = _slugify(page_title)
    normalized_title_prefix = str(weekly_slides_title_prefix or "")
    normalized_weekly_indent = max(0, int(weekly_slides_indent))
    normalized_lecture_base = (
        _canonicalize_http_url(lecture_slides_base_url)
        if lecture_slides_base_url
        else None
    )
    section_header_value = (
        str(weekly_slides_section_header).strip()
        if weekly_slides_section_header is not None
        else ""
    )

    def _ensure_module_published(module_obj: Any, *, module_label: str) -> None:
        if bool(getattr(module_obj, "published", False)):
            return
        actions.append(f"publish-module:{module_label}")
        log.info("Publishing module '%s'", module_label)
        if not dry_run:
            module_obj.edit(module={"published": True})

    def _ensure_module_item_published(item_obj: Any, *, item_label: str) -> None:
        if bool(getattr(item_obj, "published", False)):
            return
        actions.append(f"publish-module-item:{item_label}")
        log.info("Publishing module item '%s'", item_label)
        if not dry_run:
            item_obj.edit(module_item={"published": True})

    existing_page = _find_page_by_title(canvas_course, page_title)
    if existing_page is not None:
        actions.append(f"update-page:{page_title}")
        page_url = getattr(existing_page, "url", page_url) or page_url
        if not dry_run:
            existing_page.edit(
                wiki_page={
                    "title": page_title,
                    "body": html_body,
                    "published": True,
                }
            )
    else:
        actions.append(f"create-page:{page_title}")
        if not dry_run:
            created_page = canvas_course.course.create_page(
                wiki_page={
                    "title": page_title,
                    "body": html_body,
                    "published": True,
                }
            )
            page_url = getattr(created_page, "url", page_url) or page_url

    if module_name:
        module = _find_module_by_name(canvas_course, module_name)
        if module is None:
            actions.append(f"create-module:{module_name}")
            if not dry_run:
                module = canvas_course.course.create_module(
                    module={
                        "name": module_name,
                        "published": True,
                    }
                )
        else:
            actions.append(f"reuse-module:{module_name}")

        if module is not None:
            _ensure_module_published(module, module_label=module_name)
            page_item = None
            for item in module.get_module_items():
                item_type = str(getattr(item, "type", "")).lower()
                item_title = str(getattr(item, "title", ""))
                item_page_url = str(getattr(item, "page_url", ""))
                if item_type == "page" and (
                    item_page_url == page_url
                    or item_title.strip() == page_title.strip()
                ):
                    page_item = item
                    break
            if page_item is None:
                actions.append(f"link-page-in-module:{module_name}:{page_title}")
                if not dry_run:
                    page_item = module.create_module_item(
                        module_item={
                            "type": "Page",
                            "title": page_title,
                            "page_url": page_url,
                            "published": True,
                        }
                    )
                    _ensure_module_item_published(
                        page_item,
                        item_label=f"{module_name}:{page_title}",
                    )
            else:
                actions.append(f"module-link-exists:{module_name}:{page_title}")
                _ensure_module_item_published(
                    page_item,
                    item_label=f"{module_name}:{page_title}",
                )

    if publish_weekly_slides:
        weekly_modules = _build_weekly_slide_items(
            calendar_json,
            module_name_template=weekly_module_template,
        )
        total_modules = len(weekly_modules)
        for module_idx, (weekly_module_name, links) in enumerate(
            weekly_modules.items(), start=1
        ):
            log.info(
                "Weekly slides module %d/%d: %s (%d links)",
                module_idx,
                total_modules,
                weekly_module_name,
                len(links),
            )
            weekly_module = _find_module_by_name(canvas_course, weekly_module_name)
            if weekly_module is None:
                actions.append(f"create-module:{weekly_module_name}")
                log.info("Creating module '%s'", weekly_module_name)
                if not dry_run:
                    weekly_module = canvas_course.course.create_module(
                        module={
                            "name": weekly_module_name,
                            "published": True,
                        }
                    )
            else:
                actions.append(f"reuse-module:{weekly_module_name}")
                log.info("Reusing module '%s'", weekly_module_name)

            if weekly_module is not None:
                _ensure_module_published(weekly_module, module_label=weekly_module_name)

            desired_links: list[dict[str, str]] = []
            desired_urls: set[str] = set()
            for link in links:
                raw_title = str(link.get("title") or "").strip()
                raw_url = _canonicalize_http_url(str(link.get("url") or "").strip())
                if not raw_url:
                    continue
                desired_title = (
                    f"{normalized_title_prefix}{raw_title}"
                    if normalized_title_prefix
                    else raw_title
                )
                desired_links.append(
                    {
                        "title": desired_title,
                        "url": raw_url,
                    }
                )
                desired_urls.add(raw_url)

            existing_external_items_by_url: dict[str, list[Any]] = {}
            managed_external_items: list[tuple[Any, str]] = []
            existing_subheaders: list[Any] = []
            if weekly_module is not None:
                for item in weekly_module.get_module_items():
                    item_type = str(getattr(item, "type", "")).lower().replace("_", "")
                    item_title = str(getattr(item, "title", "")).strip()
                    if item_type == "subheader" and section_header_value:
                        if item_title == section_header_value:
                            existing_subheaders.append(item)
                        continue
                    if item_type != "externalurl":
                        continue
                    external_url = _canonicalize_http_url(
                        str(getattr(item, "external_url", "")).strip()
                    )
                    if external_url:
                        existing_external_items_by_url.setdefault(external_url, []).append(
                            item
                        )
                        is_managed = False
                        if normalized_title_prefix and item_title.startswith(
                            normalized_title_prefix
                        ):
                            is_managed = True
                        if (
                            normalized_lecture_base
                            and external_url.startswith(
                                normalized_lecture_base.rstrip("/") + "/"
                            )
                        ):
                            is_managed = True
                        if is_managed:
                            managed_external_items.append((item, external_url))

            if section_header_value:
                if existing_subheaders:
                    actions.append(f"slides-header-exists:{weekly_module_name}")
                    for subheader_item in existing_subheaders:
                        _ensure_module_item_published(
                            subheader_item,
                            item_label=f"{weekly_module_name}:{section_header_value}",
                        )
                else:
                    actions.append(
                        f"add-slides-header:{weekly_module_name}:{section_header_value}"
                    )
                    log.info(
                        "Adding section header '%s' in module '%s'",
                        section_header_value,
                        weekly_module_name,
                    )
                    if not dry_run and weekly_module is not None:
                        created_subheader = weekly_module.create_module_item(
                            module_item={
                                "type": "SubHeader",
                                "title": section_header_value,
                                "published": True,
                            }
                        )
                        _ensure_module_item_published(
                            created_subheader,
                            item_label=f"{weekly_module_name}:{section_header_value}",
                        )

            if weekly_slides_prune_existing:
                stale_items = [
                    (item, url_value)
                    for item, url_value in managed_external_items
                    if url_value not in desired_urls
                ]
                for stale_item, stale_url in stale_items:
                    stale_title = str(getattr(stale_item, "title", "")).strip()
                    actions.append(
                        f"remove-weekly-link:{weekly_module_name}:{stale_title}:{stale_url}"
                    )
                    log.info(
                        "Removing stale weekly link in '%s': %s (%s)",
                        weekly_module_name,
                        stale_title,
                        stale_url,
                    )
                    if not dry_run:
                        stale_item.delete()

            total_links = len(desired_links)
            for link_idx, link in enumerate(desired_links, start=1):
                title = link["title"]
                url = link["url"]
                existing_for_url = existing_external_items_by_url.get(url, [])
                has_matching = False
                for existing_item in existing_for_url:
                    existing_title = str(getattr(existing_item, "title", "")).strip()
                    existing_indent = int(getattr(existing_item, "indent", 0) or 0)
                    if (
                        existing_title == title
                        and existing_indent == normalized_weekly_indent
                    ):
                        has_matching = True
                        break

                if has_matching:
                    for existing_item in existing_for_url:
                        existing_title = str(getattr(existing_item, "title", "")).strip()
                        existing_indent = int(getattr(existing_item, "indent", 0) or 0)
                        if (
                            existing_title == title
                            and existing_indent == normalized_weekly_indent
                        ):
                            _ensure_module_item_published(
                                existing_item,
                                item_label=f"{weekly_module_name}:{existing_title}",
                            )
                            break
                    if weekly_slides_prune_existing:
                        for existing_item in existing_for_url:
                            existing_title = str(
                                getattr(existing_item, "title", "")
                            ).strip()
                            existing_indent = int(
                                getattr(existing_item, "indent", 0) or 0
                            )
                            if (
                                existing_title == title
                                and existing_indent == normalized_weekly_indent
                            ):
                                continue
                            actions.append(
                                f"remove-duplicate-weekly-link:{weekly_module_name}:{existing_title}:{url}"
                            )
                            log.info(
                                "Removing duplicate weekly link in '%s': %s (%s)",
                                weekly_module_name,
                                existing_title,
                                url,
                            )
                            if not dry_run:
                                existing_item.delete()
                    actions.append(
                        f"weekly-link-exists:{weekly_module_name}:{title}:{url}"
                    )
                    log.info(
                        "Week %s link %d/%d already present: %s",
                        weekly_module_name,
                        link_idx,
                        total_links,
                        title,
                    )
                    continue

                if weekly_slides_prune_existing:
                    for existing_item in existing_for_url:
                        existing_title = str(getattr(existing_item, "title", "")).strip()
                        actions.append(
                            f"replace-weekly-link:{weekly_module_name}:{existing_title}:{url}"
                        )
                        log.info(
                            "Replacing weekly link in '%s': %s (%s)",
                            weekly_module_name,
                            existing_title,
                            url,
                        )
                        if not dry_run:
                            existing_item.delete()

                actions.append(f"add-weekly-link:{weekly_module_name}:{title}:{url}")
                log.info(
                    "Week %s link %d/%d add: %s",
                    weekly_module_name,
                    link_idx,
                    total_links,
                    title,
                )
                if not dry_run and weekly_module is not None:
                    created_item = weekly_module.create_module_item(
                        module_item={
                            "type": "ExternalUrl",
                            "title": title,
                            "external_url": url,
                            "indent": normalized_weekly_indent,
                            "new_tab": True,
                            "published": True,
                        }
                    )
                    _ensure_module_item_published(
                        created_item,
                        item_label=f"{weekly_module_name}:{title}",
                    )

    return {
        "dry_run": dry_run,
        "actions": actions,
        "page_title": page_title,
        "page_url": page_url,
        "module_name": module_name,
        "publish_weekly_slides": publish_weekly_slides,
        "weekly_module_template": weekly_module_template,
    }


def build_course_calendar(
    *,
    plan_path: str | Path,
    output_dir: str | Path,
    publish: bool = False,
    publish_weekly_slides: bool = False,
    dry_run: bool = True,
    canvas_course: CanvasCourse | None = None,
    page_title_override: str | None = None,
    module_name: str | None = None,
    weekly_module_template_override: str | None = None,
) -> CalendarBuildResult:
    raw = load_course_plan(plan_path)
    normalized = normalize_course_plan(raw)
    calendar_json, warnings = build_schedule(normalized)
    calendar_html = render_calendar_html(normalized, calendar_json)

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    html_path = output_root / "calendar.html"
    json_path = output_root / "calendar.json"
    normalized_path = output_root / "normalized_plan.yaml"

    yaml = _load_yaml_module()
    html_path.write_text(calendar_html, encoding="utf-8")
    json_path.write_text(json.dumps(calendar_json, indent=2) + "\n", encoding="utf-8")
    normalized_path.write_text(
        yaml.safe_dump(
            {
                **normalized,
                "term": {
                    **normalized["term"],
                    "start_date": normalized["term"]["start_date"].isoformat(),
                    "end_date": normalized["term"]["end_date"].isoformat(),
                    "global_no_class_dates": [
                        d.isoformat()
                        for d in sorted(normalized["term"]["global_no_class_dates"])
                    ],
                    "breaks": [
                        {
                            **break_period,
                            "start_date": break_period["start_date"].isoformat(),
                            "end_date": break_period["end_date"].isoformat(),
                            "applies_to": (
                                sorted(list(break_period["applies_to"]))
                                if break_period["applies_to"]
                                else None
                            ),
                        }
                        for break_period in normalized["term"]["breaks"]
                    ],
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    publish_result = None
    if publish:
        if canvas_course is None:
            raise ValueError("canvas_course is required when publish=True.")
        page_title = (
            page_title_override or normalized["publishing"]["schedule_page_title"]
        )
        module_name_value = module_name or "Course Schedule"
        weekly_module_template = (
            weekly_module_template_override
            or normalized["publishing"]["module_name_template"]
        )
        publish_result = publish_calendar_to_canvas(
            canvas_course,
            calendar_json=calendar_json,
            page_title=page_title,
            module_name=module_name_value,
            publish_weekly_slides=publish_weekly_slides,
            weekly_module_template=weekly_module_template,
            weekly_slides_title_prefix=normalized["publishing"][
                "weekly_slides_title_prefix"
            ],
            weekly_slides_indent=normalized["publishing"]["weekly_slides_indent"],
            weekly_slides_section_header=normalized["publishing"][
                "weekly_slides_section_header"
            ],
            weekly_slides_prune_existing=normalized["publishing"][
                "weekly_slides_prune_existing"
            ],
            lecture_slides_base_url=normalized["resource_defaults"][
                "lecture_slides_base_url"
            ],
            html_body=calendar_html,
            dry_run=dry_run,
        )

    return CalendarBuildResult(
        normalized_plan=normalized,
        calendar_json=calendar_json,
        calendar_html=calendar_html,
        warnings=warnings,
        output_paths={
            "calendar_html": html_path,
            "calendar_json": json_path,
            "normalized_plan": normalized_path,
        },
        publish_result=publish_result,
    )
