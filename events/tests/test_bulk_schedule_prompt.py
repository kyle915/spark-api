"""Unit tests for natural-language bulk scheduling (`events.bulk_schedule_prompt`)."""

from __future__ import annotations

from datetime import date

from events.bulk_schedule_prompt import (
    BULK_PROMPT_SCHEMA,
    compose_bulk_preview,
    expand_schedule_dates,
    filter_and_rank_tenant_stores,
)


def test_bulk_prompt_schema_is_strict():
    assert BULK_PROMPT_SCHEMA["additionalProperties"] is False
    assert set(BULK_PROMPT_SCHEMA["required"]) == set(BULK_PROMPT_SCHEMA["properties"])


def test_expand_schedule_dates_every_fri_and_sat_in_june():
    today = date(2026, 11, 26)
    spec = {
        "month": 6,
        "year": None,
        "next_n_weeks": None,
        "weekdays": ["fri", "sat"],
        "specific_dates": [],
    }
    dates = expand_schedule_dates(spec, today=today)
    # June 2027 Fridays & Saturdays: Jun 4, 5, 11, 12, 18, 19, 25, 26 (8 dates)
    assert len(dates) == 8
    assert dates[0] == date(2027, 6, 4)
    assert all(d.weekday() in (4, 5) for d in dates)


def test_compose_bulk_preview_matches_top_austin_hebs_and_caps_rows():
    today = date(2026, 11, 26)
    stores = [
        {
            "key": f"s{i}",
            "name": f"H-E-B #{700 + i}",
            "store_number": str(700 + i),
            "address": f"{100 + i} Lamar Blvd, Austin, TX 78701",
            "city": "Austin",
            "state": "TX",
            "lat": 30.27,
            "lng": -97.74,
            "recaps": 4,
            "consumers": 150 + i * 20,
            "conversion_pct": 25.0 + i * 3.0,
            "days_since": 30,
        }
        for i in range(1, 7)
    ] + [
        {
            "key": "s99",
            "name": "H-E-B Houston",
            "store_number": "99",
            "address": "500 Westheimer Rd, Houston, TX 77006",
            "city": "Houston",
            "state": "TX",
            "lat": 29.74,
            "lng": -95.39,
            "recaps": 6,
            "consumers": 400,
            "conversion_pct": 55.0,
            "days_since": 40,
        }
    ]
    catalog = [("u-mango", "Mango Chainsaw"), ("u-lime", "Severed Lime")]
    spec = {
        "retailer_query": "HEBs",
        "city": "Austin",
        "state": "TX",
        "store_limit": 5,
        "selection_mode": "top_conversion",
        "month": 6,
        "year": 2027,
        "next_n_weeks": None,
        "weekdays": ["fri", "sat"],
        "specific_dates": [],
        "start_text": "12pm",
        "end_text": "4pm",
        "products": ["Mango Chainsaw"],
        "notes": "Ask for beverage lead",
    }

    ranked = filter_and_rank_tenant_stores(spec, stores)
    assert len(ranked) == 5
    # Houston H-E-B excluded by Austin city filter; highest Austin conversion first (s6).
    assert all(s["city"] == "Austin" for s in ranked)
    assert ranked[0]["key"] == "s6"

    ok, msg, summary, truncated, time_defaulted, products, rows = compose_bulk_preview(
        spec, today=today, stores=stores, catalog=catalog
    )
    assert ok is True
    assert msg == ""
    # 5 stores * 8 dates = 40 -> capped cleanly at 25 preview rows
    assert len(rows) == 25
    assert truncated is True
    assert time_defaulted is False
    assert rows[0]["start_time"] == "12:00"
    assert rows[0]["end_time"] == "16:00"
    assert products == [("u-mango", "Mango Chainsaw")]
    assert "25 visits across 5 stores" in summary
