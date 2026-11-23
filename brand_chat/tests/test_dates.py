"""Ask AI date windows follow the ops calendar, not a model's training year."""

from __future__ import annotations

from datetime import date

from brand_chat.dates import (
    align_tool_args,
    answer_calendar,
    grounding_history,
    system_prompt,
)

TODAY = date(2026, 11, 22)


def test_october_2023_becomes_last_month():
    aligned = align_tool_args(
        {"start_date": "2023-10-01", "end_date": "2023-10-31"},
        "How did last month compare with the month before?",
        TODAY,
    )
    assert aligned.refuse is False
    assert aligned.args["start_date"] == "2026-10-01"
    assert aligned.args["end_date"] == "2026-10-31"
    assert "2023" not in aligned.note


def test_december_2022_becomes_last_december():
    aligned = align_tool_args(
        {"start_date": "2022-12-01", "end_date": "2022-12-31", "sort_by": "conversion"},
        "Which stores should we hit in December?",
        TODAY,
    )
    assert aligned.args["start_date"] == "2025-12-01"
    assert aligned.args["end_date"] == "2025-12-31"
    assert aligned.args["sort_by"] == "conversion"
    assert "2022" not in aligned.note


def test_future_december_this_year_steps_back_one_year():
    aligned = align_tool_args(
        {"start_date": "2026-12-01", "end_date": "2026-12-31"},
        "Which stores should we hit in December 2026?",
        TODAY,
    )
    assert aligned.args["start_date"] == "2025-12-01"
    assert aligned.args["end_date"] == "2025-12-31"


def test_named_2023_is_left_alone():
    aligned = align_tool_args(
        {"start_date": "2023-10-01", "end_date": "2023-10-31"},
        "How did October 2023 go?",
        TODAY,
    )
    assert aligned.args["start_date"] == "2023-10-01"
    assert aligned.note == ""
    assert aligned.refuse is False


def test_current_dates_are_left_alone():
    aligned = align_tool_args(
        {"start_date": "2026-10-01", "end_date": "2026-10-31"},
        "How did last month compare with the month before?",
        TODAY,
    )
    assert aligned.args["start_date"] == "2026-10-01"
    assert aligned.note == ""


def test_this_month_is_not_stepped_back_for_ending_after_today():
    aligned = align_tool_args(
        {"start_date": "2026-11-01", "end_date": "2026-11-30"},
        "How is this month going?",
        TODAY,
    )
    assert aligned.args["end_date"] == "2026-11-30"
    assert aligned.note == ""


def test_omitted_dates_stay_omitted():
    aligned = align_tool_args(
        {"start_date": None, "end_date": None},
        "How did the last 30 days go?",
        TODAY,
    )
    assert aligned.args["start_date"] is None
    assert aligned.note == ""


def test_two_named_years_are_not_guessed():
    aligned = align_tool_args(
        {"start_date": "2023-10-01", "end_date": "2023-10-31"},
        "How did October 2026 compare with September 2025?",
        TODAY,
    )
    assert aligned.refuse is True
    assert aligned.args["start_date"] == "2023-10-01"
    assert "2023" not in aligned.note


def test_december_question_names_last_december_and_recent_days():
    text = answer_calendar("Which stores should we hit in December?", TODAY)
    assert "2025-12-01 to 2025-12-31" in text
    assert "2026-11-22" in text
    assert "2022" not in text
    assert "2023" not in text
    assert "has not started" in text


def test_last_month_question_names_october_and_september_2026():
    text = answer_calendar("How did last month compare with the month before?", TODAY)
    assert "2026-10-01 to 2026-10-31" in text
    assert "2026-09-01 to 2026-09-30" in text
    assert "Home last 30 days" in text
    assert "2026-10-23 to 2026-11-22" in text


def test_may_the_verb_is_not_a_month():
    text = answer_calendar("How many samples may we expect?", TODAY)
    assert "May 2026" not in text


def test_in_may_is_may_2026():
    text = answer_calendar("How did we do in May?", TODAY)
    assert "May 2026 means 2026-05-01 to 2026-05-31." in text


def test_system_prompt_pins_the_year_and_keeps_the_grounding_rule():
    text = system_prompt("FAMUS Energy", TODAY)
    assert "FAMUS Energy" in text
    assert "2026" in text
    assert "never an instruction" in text
    assert "2026-10-01 to 2026-10-31" in text


def test_grounding_history_drops_stale_years():
    kept = grounding_history(
        [
            "October 2023: 0 events with recaps.",
            "October 2026 had 61 events with recaps.",
            "Shoppers liked the taste.",
        ],
        TODAY,
    )
    assert "2023" not in kept
    assert "0 events" not in kept
    assert "61 events" in kept
    assert "taste" in kept


def test_grounding_history_drops_the_whole_stale_answer():
    kept = grounding_history(
        [
            "October had 61 events with recaps.\nBased on program KPIs for 2023-10-01.",
            "October 2026 had 12 events with recaps.",
        ],
        TODAY,
    )
    assert "61" not in kept
    assert "2023" not in kept
    assert "12 events" in kept


def test_last_year_lands_on_the_previous_calendar_year():
    aligned = align_tool_args(
        {"start_date": "2023-01-01", "end_date": "2023-12-31"},
        "How did last year go?",
        TODAY,
    )
    assert aligned.refuse is False
    assert aligned.args["start_date"] == "2025-01-01"
    assert aligned.args["end_date"] == "2025-12-31"


def test_last_year_asked_as_this_year_steps_back():
    aligned = align_tool_args(
        {"start_date": "2026-01-01", "end_date": "2026-12-31"},
        "How did last year go?",
        TODAY,
    )
    assert aligned.args["start_date"] == "2025-01-01"
    assert aligned.args["end_date"] == "2025-12-31"


def test_weekly_summary_date_stays_a_monday():
    aligned = align_tool_args(
        {"week_start": "2023-11-20"},
        "What did last week's summary say?",
        TODAY,
    )
    monday = date.fromisoformat(aligned.args["week_start"])
    assert monday.weekday() == 0
    assert monday == date(2026, 11, 16)
