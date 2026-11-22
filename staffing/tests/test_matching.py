"""Pure unit tests for Smart BA Matching composite scoring."""

from __future__ import annotations

from staffing.matching import SmartBaSignalInput, score_smart_ba_candidate


def _sig(**overrides) -> SmartBaSignalInput:
    base = dict(
        ba_id=42,
        ba_uuid="00000000-0000-0000-0000-000000000042",
        name="Alex Ambassador",
        email="alex@example.com",
        phone="+15555550100",
        units_per_hour=14.2,
        reliability_rate=98.0,
        on_time_checkins=20,
        total_tracked_shifts=21,
        brand_gigs=4,
        brand_certified=True,
        has_active_cert_doc=True,
        is_favorited=False,
        distance_miles=4.2,
        same_state=True,
        is_available=True,
    )
    base.update(overrides)
    return SmartBaSignalInput(**base)


def test_score_includes_transparent_summary_reason():
    row = score_smart_ba_candidate(_sig())
    assert row["score"] >= 70
    assert "14.2 units/hr" in row["reasons"]
    assert "98% reliable" in row["reasons"]
    assert "Brand certified" in row["reasons"]
    assert "4.2 mi away" in row["reasons"]
    assert row["summary_reason"] == " · ".join(row["reasons"])
    assert "14.2 units/hr · 98% reliable · Brand certified" in row["summary_reason"]


def test_proven_uph_outranks_neutral_unknown():
    strong = score_smart_ba_candidate(_sig(units_per_hour=18.0, reliability_rate=95.0))
    unknown = score_smart_ba_candidate(
        _sig(
            units_per_hour=None,
            reliability_rate=None,
            brand_certified=False,
            brand_gigs=0,
            has_active_cert_doc=False,
            distance_miles=None,
            same_state=False,
            is_available=None,
        )
    )
    assert strong["score"] > unknown["score"]


def test_unavailable_penalty_and_available_bonus():
    available = score_smart_ba_candidate(_sig(is_available=True))
    unavailable = score_smart_ba_candidate(_sig(is_available=False))
    assert available["score"] > unavailable["score"]
