from __future__ import annotations

from lms_interface.course_plan import (
    _build_weekly_slide_items,
    build_schedule,
    infer_title_from_id,
    normalize_course_plan,
    render_calendar_html,
)


def _base_plan() -> dict:
    return {
        "version": "1.1",
        "term": {
            "start_date": "2026-01-05",
            "end_date": "2026-01-16",
            "timezone": "America/Los_Angeles",
            "breaks": [
                {
                    "name": "MW only closure",
                    "start_date": "2026-01-12",
                    "end_date": "2026-01-12",
                    "applies_to": ["sec_mw"],
                }
            ],
        },
        "sections": [
            {"id": "sec_mw", "name": "Mon/Wed", "meeting_days": ["Mon", "Wed"]},
            {"id": "sec_tth", "name": "Tue/Thu", "meeting_days": ["Tue", "Thu"]},
        ],
        "sync": {
            "mode": "lockstep_by_topic",
            "skip_for_all_if_any_section_skips": True,
        },
        "topics": [
            {"id": "topic-1", "lecture_slides": ["https://example.com/slide1.pdf"]},
            {"id": "topic-2", "readings": ["https://example.com/read2.pdf"]},
            {"id": "topic-3"},
            {"id": "topic-4"},
        ],
        "exam_rules": {
            "schedule_mode": "mixed",
            "groups": [
                {
                    "name": "Exam 1",
                    "through_topic": "topic-2",
                    "fixed_date_overrides": {
                        "sec_mw": "2026-01-14",
                        "sec_tth": "2026-01-15",
                    },
                }
            ],
        },
    }


def test_infer_title_from_id_handles_ostep_and_separators():
    assert (
        infer_title_from_id("ostep07-process-scheduling")
        == "OSTEP07 Process Scheduling"
    )
    assert infer_title_from_id("process_scheduling_os7") == "Process Scheduling Os7"


def test_normalize_course_plan_supports_compact_resources_and_exam_coverage():
    raw_plan = _base_plan()
    raw_plan["topics"][0]["appears_on"] = ["Exam 1"]
    normalized = normalize_course_plan(raw_plan)

    first_topic = normalized["topics"][0]
    assert first_topic["title"] == "Topic 1"
    assert first_topic["lecture_slides"][0]["url"] == "https://example.com/slide1.pdf"
    assert normalized["exam_coverage"]["Exam 1"] == ["topic-1"]


def test_normalize_course_plan_sets_weekly_slides_publishing_defaults():
    normalized = normalize_course_plan(_base_plan())

    assert normalized["publishing"]["weekly_slides_title_prefix"] == "Slides: "
    assert normalized["publishing"]["weekly_slides_indent"] == 1
    assert normalized["publishing"]["weekly_slides_section_header"] == "Slides"
    assert normalized["publishing"]["weekly_slides_prune_existing"] is True


def test_normalize_course_plan_respects_weekly_slides_publishing_overrides():
    raw_plan = _base_plan()
    raw_plan["publishing"] = {
        "weekly_slides_title_prefix": "Lecture: ",
        "weekly_slides_indent": 2,
        "weekly_slides_section_header": "Lecture Links",
        "weekly_slides_prune_existing": False,
    }

    normalized = normalize_course_plan(raw_plan)

    assert normalized["publishing"]["weekly_slides_title_prefix"] == "Lecture: "
    assert normalized["publishing"]["weekly_slides_indent"] == 2
    assert normalized["publishing"]["weekly_slides_section_header"] == "Lecture Links"
    assert normalized["publishing"]["weekly_slides_prune_existing"] is False


def test_normalize_course_plan_expands_relative_resources_with_base_urls():
    raw_plan = _base_plan()
    raw_plan["resource_defaults"] = {
        "lecture_slides_base_url": "https://github.com/CSUMB-SCD-instructors/CST334/tree/main/slides/pdfs",
        "readings_base_url": "https://pages.cs.wisc.edu/~remzi/OSTEP/",
    }
    raw_plan["topics"][0]["lecture_slides"] = ["OSTEP 01.pdf"]
    raw_plan["topics"][1]["readings"] = [{"url": "intro.pdf"}]

    normalized = normalize_course_plan(raw_plan)

    assert normalized["topics"][0]["lecture_slides"][0]["url"] == (
        "https://github.com/CSUMB-SCD-instructors/CST334/tree/main/slides/pdfs/OSTEP%2001.pdf"
    )
    assert normalized["topics"][1]["readings"][0]["url"] == (
        "https://pages.cs.wisc.edu/~remzi/OSTEP/intro.pdf"
    )


def test_build_schedule_lockstep_respects_section_specific_skip():
    normalized = normalize_course_plan(_base_plan())
    schedule, warnings = build_schedule(normalized)

    # Week 2 loses one slot because sec_mw has a section-specific closure and lockstep skip is enabled.
    # The remaining week-2 slot is consumed by the fixed exam date.
    assert len(schedule["rows"]) == 3
    assert any("did not fit" in warning for warning in warnings)
    assert schedule["rows"][0]["topic_id"] == "topic-1"
    assert schedule["rows"][1]["topic_id"] == "topic-2"
    assert schedule["rows"][2]["topic_id"] is None
    assert schedule["rows"][2]["exam_names"] == ["Exam 1"]

    exam_dates = schedule["exam_dates"]["Exam 1"]
    assert exam_dates["sec_mw"] == "2026-01-14"
    assert exam_dates["sec_tth"] == "2026-01-15"


def test_render_calendar_html_contains_topics_and_exam_badge():
    normalized = normalize_course_plan(_base_plan())
    schedule, _ = build_schedule(normalized)
    html_output = render_calendar_html(normalized, schedule)

    assert "Topic 1" in html_output
    assert "Exam 1" in html_output
    assert "Mon/Wed" in html_output
    assert "Tue/Thu" in html_output


def test_build_weekly_slide_items_groups_by_week_and_deduplicates_urls():
    calendar_json = {
        "rows": [
            {
                "week_number": 1,
                "lecture_slides": [
                    {"title": "Slide A", "url": "https://example.com/a.pdf"},
                    {"title": "Slide A dup", "url": "https://example.com/a.pdf"},
                    {"title": "Slide B", "url": "https://example.com/b.pdf"},
                ],
            },
            {
                "week_number": 2,
                "lecture_slides": [
                    {"title": "Slide C", "url": "https://example.com/c.pdf"},
                ],
            },
        ]
    }

    weekly = _build_weekly_slide_items(
        calendar_json,
        module_name_template="Week {week_number}",
    )

    assert list(weekly.keys()) == ["Week 1", "Week 2"]
    assert [item["url"] for item in weekly["Week 1"]] == [
        "https://example.com/a.pdf",
        "https://example.com/b.pdf",
    ]
    assert [item["url"] for item in weekly["Week 2"]] == ["https://example.com/c.pdf"]


def test_build_schedule_supports_multi_hour_topics():
    plan = {
        "version": "1.1",
        "term": {
            "start_date": "2026-01-05",
            "end_date": "2026-01-14",
            "timezone": "America/Los_Angeles",
        },
        "sections": [
            {"id": "sec_mw", "name": "Mon/Wed", "meeting_days": ["Mon", "Wed"]},
        ],
        "sync": {
            "mode": "lockstep_by_topic",
            "topics_per_meeting": 2,
        },
        "topics": [
            {"id": "mlfq", "title": "MLFQ", "duration_hours": 3},
            {"id": "sched", "title": "Scheduling", "duration_hours": 1},
        ],
    }
    normalized = normalize_course_plan(plan)
    schedule, warnings = build_schedule(normalized)

    assert warnings == []
    assert len(schedule["rows"]) >= 2
    assert schedule["rows"][0]["topic_titles"] == ["MLFQ (2h)"]
    assert schedule["rows"][1]["topic_titles"] == ["MLFQ", "Scheduling"]


def test_build_schedule_derived_exam_consumes_slot():
    plan = {
        "version": "1.1",
        "term": {
            "start_date": "2026-01-05",
            "end_date": "2026-01-15",
            "timezone": "America/Los_Angeles",
        },
        "sections": [
            {"id": "sec_mw", "name": "Mon/Wed", "meeting_days": ["Mon", "Wed"]},
            {"id": "sec_tth", "name": "Tue/Thu", "meeting_days": ["Tue", "Thu"]},
        ],
        "sync": {
            "mode": "lockstep_by_topic",
            "skip_for_all_if_any_section_skips": True,
        },
        "exam_rules": {
            "defaults": {
                "class_meeting_index_in_week": 1,
                "min_class_meetings_after_last_new_material": 0,
            },
            "groups": [
                {
                    "name": "Exam 1",
                    "through_topic": "topic-2",
                }
            ],
        },
        "topics": [
            {"id": "topic-1", "title": "Topic 1"},
            {"id": "topic-2", "title": "Topic 2"},
            {"id": "topic-3", "title": "Topic 3"},
        ],
    }

    normalized = normalize_course_plan(plan)
    schedule, warnings = build_schedule(normalized)

    assert warnings == []
    assert len(schedule["rows"]) == 4
    assert schedule["rows"][0]["topic_id"] == "topic-1"
    assert schedule["rows"][1]["topic_id"] == "topic-2"
    assert schedule["rows"][2]["topic_id"] is None
    assert schedule["rows"][2]["exam_names"] == ["Exam 1"]
    assert schedule["rows"][3]["topic_id"] == "topic-3"
    assert schedule["exam_dates"]["Exam 1"] == {
        "sec_mw": "2026-01-12",
        "sec_tth": "2026-01-13",
    }


def test_build_schedule_fixed_exam_outside_class_slots_adds_exam_row():
    plan = {
        "version": "1.1",
        "term": {
            "start_date": "2026-01-05",
            "end_date": "2026-01-15",
            "timezone": "America/Los_Angeles",
        },
        "sections": [
            {"id": "sec_mw", "name": "Mon/Wed", "meeting_days": ["Mon", "Wed"]},
        ],
        "sync": {
            "mode": "lockstep_by_topic",
        },
        "exam_rules": {
            "groups": [
                {
                    "name": "Final Exam",
                    "fixed_date_overrides": {
                        "sec_mw": "2026-01-16",
                    },
                }
            ],
        },
        "topics": [
            {"id": "topic-1", "title": "Topic 1"},
            {"id": "topic-2", "title": "Topic 2"},
        ],
    }

    normalized = normalize_course_plan(plan)
    schedule, warnings = build_schedule(normalized)

    assert warnings == []
    assert schedule["exam_dates"]["Final Exam"]["sec_mw"] == "2026-01-16"
    exam_rows = [row for row in schedule["rows"] if row.get("exam_names")]
    assert len(exam_rows) == 1
    exam_row = exam_rows[0]
    assert exam_row["exam_names"] == ["Final Exam"]
    assert exam_row["slot_in_week"] == "Exam"
    assert exam_row["topic_ids"] == []
    assert exam_row["dates"]["sec_mw"] == "2026-01-16"


def test_normalize_course_plan_supports_reusable_placeholders():
    plan = {
        "version": "1.1",
        "term": {
            "start_date": "2026-01-05",
            "end_date": "2026-01-12",
            "timezone": "America/Los_Angeles",
        },
        "sections": [
            {"id": "sec_mw", "name": "Mon/Wed", "meeting_days": ["Mon", "Wed"]},
        ],
        "placeholders": {
            "tbd": {
                "title": "Buffer / TBD",
                "new_material": False,
                "lecture_slides": ["placeholder.pdf"],
            }
        },
        "resource_defaults": {
            "lecture_slides_base_url": "https://example.com/slides",
        },
        "topics": [
            {"id": "topic-1", "title": "Topic 1"},
            {"placeholder": "tbd", "duration_hours": 2},
            {"placeholder": "tbd"},
        ],
    }

    normalized = normalize_course_plan(plan)
    topic_ids = [topic["id"] for topic in normalized["topics"]]
    assert topic_ids == ["topic-1", "tbd-1", "tbd-2"]

    second = normalized["topics"][1]
    assert second["title"] == "Buffer / TBD"
    assert second["new_material"] is False
    assert second["duration_hours"] == 2
    assert (
        second["lecture_slides"][0]["url"]
        == "https://example.com/slides/placeholder.pdf"
    )
