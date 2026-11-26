"""Pure unit tests for AI Wave Planner (`propose_demo_wave`) and source links."""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from brand_chat import service
from brand_chat.tools import (
    CHAT_TOOLS,
    build_wave_proposal,
    openai_tools,
    resolve_wave_window,
)
from spark_mcp import tools as mcp


def test_propose_demo_wave_schema_is_strict_and_tenant_free():
    assert "propose_demo_wave" in CHAT_TOOLS
    for tool in openai_tools():
        params = tool["function"]["parameters"]
        assert tool["function"]["strict"] is True
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])
        assert not any("tenant" in k or "brand" in k for k in params["properties"])


def test_resolve_wave_window_defaults_to_next_month_and_shifts_stale_year():
    today = date(2026, 11, 26)
    assert resolve_wave_window(None, today) == (date(2026, 12, 1), date(2026, 12, 31))
    # Stale training year (2023-12) shifts forward to 2026-12.
    assert resolve_wave_window("2023-12", today) == (date(2026, 12, 1), date(2026, 12, 31))
    # Past month in current year (2026-02) shifts to 2027-02.
    assert resolve_wave_window("2026-02", today) == (date(2027, 2, 1), date(2027, 2, 28))
    with pytest.raises(mcp.ToolInputError):
        resolve_wave_window("december", today)


def test_build_wave_proposal_excludes_scheduled_and_generates_wave_url():
    today = date(2026, 11, 26)
    directory = {
        "s101": {"name": "HEB Mueller", "address": "1801 E 51st St", "city": "Austin", "state": "TX"},
        "s102": {"name": "Whole Foods Lamar", "address": "525 N Lamar Blvd", "city": "Austin", "state": "TX"},
        "s103": {"name": "Central Market Westgate", "address": "4477 S Lamar Blvd", "city": "Austin", "state": "TX"},
        "s104": {"name": "Sprouts Sunset", "address": "6920 Manchaca Rd", "city": "Austin", "state": "TX"},
    }
    recap_rows = [
        {
            "event_id": 1,
            "store": "HEB Mueller",
            "state": "TX",
            "city": "Austin",
            "day": date(2026, 9, 12),
            "consumers": 180,
            "base": 180,
            "units": 72,
        },
        {
            "event_id": 2,
            "store": "Whole Foods Lamar",
            "state": "TX",
            "city": "Austin",
            "day": date(2026, 10, 3),
            "consumers": 210,
            "base": 200,
            "units": 90,
        },
        {
            "event_id": 3,
            "store": "Central Market Westgate",
            "state": "TX",
            "city": "Austin",
            "day": date(2026, 8, 15),
            "consumers": 140,
            "base": 140,
            "units": 42,
        },
        # Festival venue should be ignored for retail store wave.
        {
            "event_id": 99,
            "store": "",
            "title_place": "ACL Fest",
            "title_place_kind": "venue",
            "state": "TX",
            "city": "Austin",
            "day": date(2026, 10, 10),
            "consumers": 4000,
            "base": None,
            "units": None,
        },
    ]
    event_store_map = {1: "s101", 2: "s102", 3: "s103"}
    # s102 already has an upcoming visit scheduled -> excluded!
    upcoming_keys = {"s102"}
    shift_samples = [(5, 11, 15), (6, 11, 15), (6, 11, 15), (0, 12, 16)]

    payload, count = build_wave_proposal(
        brand_name="Alpha Brew",
        today=today,
        win_start=date(2026, 12, 1),
        win_end=date(2026, 12, 31),
        store_count=3,
        strategy="balanced",
        state_filter="TX",
        directory=directory,
        upcoming_keys=upcoming_keys,
        recap_rows=recap_rows,
        event_store_map=event_store_map,
        shift_samples=shift_samples,
    )

    assert count == 3
    assert payload["already_scheduled_excluded"] == 1
    assert payload["proposed_count"] == 3
    returned_keys = [s["store_key"] for s in payload["stores"]]
    assert "s102" not in returned_keys
    assert returned_keys[0] == "s101"  # Highest conversion + rested
    assert "s104" in returned_keys  # Never-visited roster store fills remaining slot

    parsed = urlparse(payload["wave_url"])
    assert parsed.path == "/account-map"
    qs = parse_qs(parsed.query)
    assert qs["wave"] == ["1"]
    assert qs["waveStores"] == ["s101,s103,s104"]
    assert qs["waveStart"] == ["2026-12-01"]
    assert qs["waveEnd"] == ["2026-12-31"]
    assert qs["waveHours"] == ["11:00-15:00"]
    assert qs["state"] == ["TX"]

    sources = service.build_sources(
        [{"name": "propose_demo_wave", "ok": True, "args": {}, "payload": payload}],
        ["propose_demo_wave"],
    )
    assert len(sources) == 1
    assert sources[0]["label"] == "Create 3-store wave"
    assert sources[0]["link"] == payload["wave_url"]
    assert "December 2026" in sources[0]["detail"]
