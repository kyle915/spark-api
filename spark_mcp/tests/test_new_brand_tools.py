"""Shelf / cost / POS / competitor MCP tools: honest numbers + agency isolation."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from spark_mcp.tools import (
    ToolContext,
    ToolInputError,
    get_competitor_mentions,
    get_demo_cost_summary,
    get_shelf_compliance_summary,
    get_store_velocity_and_roi,
)


def _ctx(**kwargs) -> ToolContext:
    return ToolContext(
        tenant_id=kwargs.get("tenant_id", 1),
        tenant_name=kwargs.get("tenant_name", "Alpha Brew"),
        user_id=kwargs.get("user_id", 9),
        connection=None,
    )


def test_shelf_compliance_copies_reader_numbers(monkeypatch):
    monkeypatch.setattr(
        "recaps.shelf_check.shelf_summary",
        lambda tenant_id, start, end: {
            "visits": 10,
            "on_shelf": 7,
            "not_found": 2,
            "unclear": 1,
            "price_tag": 4,
            "unread_visits": 3,
            "not_found_stores": [
                {"store_name": "Alpha Market", "event_date": "2026-11-01"}
            ],
            "not_found_more": 0,
        },
    )
    payload, n = get_shelf_compliance_summary(
        _ctx(),
        {"start_date": "2026-10-01", "end_date": "2026-10-31"},
    )
    assert n == 10
    assert payload["on_shelf"] == 7
    assert payload["on_shelf_pct_of_visits"] == 70.0
    assert payload["not_found_pct_of_visits"] == 20.0
    assert payload["not_found_stores"][0]["store_name"] == "Alpha Market"
    assert "Approved retail" in payload["basis"]


def test_demo_cost_refuses_provider_scoped(monkeypatch):
    def _deny(_ctx):
        raise ToolInputError(
            "Outside field agencies cannot see consolidated brand cost"
        )

    monkeypatch.setattr("spark_mcp.tools._brand_workspace_access", _deny)
    with pytest.raises(ToolInputError, match="Outside field agencies"):
        get_demo_cost_summary(_ctx(), {})


def test_demo_cost_returns_plan_gate_without_inventing(monkeypatch):
    from pilot.entitlements import PlanEntitlementError

    access = SimpleNamespace(tenant=SimpleNamespace(id=1), can_view_brand_consolidated=True)
    monkeypatch.setattr("spark_mcp.tools._brand_workspace_access", lambda ctx: access)

    def _raise(*_a, **_k):
        raise PlanEntitlementError("Upgrade to Pro", required_plan="pro")

    monkeypatch.setattr("pilot.costs.build_brand_cost_rollups", _raise)
    payload, n = get_demo_cost_summary(
        _ctx(), {"start_date": "2026-10-01", "end_date": "2026-10-31"}
    )
    assert n == 0
    assert payload["available"] is False
    assert payload["plan_gated"] is True
    assert payload["rollups"] == []
    assert "Upgrade" in payload["message"]


def test_demo_cost_passes_through_rollup_rows(monkeypatch):
    access = SimpleNamespace(tenant=SimpleNamespace(id=1), can_view_brand_consolidated=True)
    monkeypatch.setattr("spark_mcp.tools._brand_workspace_access", lambda ctx: access)
    monkeypatch.setattr(
        "pilot.costs.build_brand_cost_rollups",
        lambda *_a, **_k: [
            {
                "key": "s1",
                "label": "Alpha Market",
                "visit_count": 2,
                "complete_visit_count": 2,
                "incomplete_visit_count": 0,
                "total_cost": "500.00",
                "known_total_cost": "500.00",
                "supported_cost": "500.00",
                "invoiced_cost": "500.00",
                "paid_cost": "0.00",
                "estimated_cost": "0.00",
                "has_incomplete_costs": False,
                "cost_per_sample": "2.50",
                "cost_per_observed_unit": None,
            }
        ],
    )
    payload, n = get_demo_cost_summary(
        _ctx(),
        {"start_date": "2026-10-01", "end_date": "2026-10-31", "group_by": "store"},
    )
    assert n == 1
    assert payload["available"] is True
    assert payload["rollups"][0]["total_cost"] == "500.00"
    assert payload["rollups"][0]["cost_per_sample"] == "2.50"


def test_pos_velocity_strips_sku_cells(monkeypatch):
    access = SimpleNamespace(tenant=SimpleNamespace(id=1), can_view_brand_consolidated=True)
    monkeypatch.setattr("spark_mcp.tools._brand_workspace_access", lambda ctx: access)
    monkeypatch.setattr(
        "pilot.pos.build_retailer_pos_measurement",
        lambda *_a, **_k: {
            "available": True,
            "unavailable_message": None,
            "retailer_banners": ["Whole Foods"],
            "coverage": {
                "matched_store_sku_weeks": 4,
                "eligible_store_sku_weeks": 5,
                "coverage_ratio": "0.8000",
                "unmatched_row_count": 1,
                "missing_rows": [{"secret": "x"}],
            },
            "totals": {
                "rep_observed_units": 12,
                "register_units": 40,
                "crew_purchased_units": 2,
                "adjusted_register_units": 38,
                "observed_vs_baseline_diff": "5.00",
                "incremental_lift_units": "5.00",
                "incremental_contribution": "25.00",
                "eligible_brand_cost": "100.00",
                "roi": "-0.7500",
                "lift_withheld_reason": None,
                "roi_withheld_reason": None,
            },
            "store_weeks": [
                {
                    "store_key": "wf-1",
                    "store_label": "WF Market St",
                    "week_start": "2026-10-06",
                    "register_units": 40,
                    "crew_purchased_units": 2,
                    "adjusted_register_units": 38,
                    "observed_vs_baseline_diff": "5.00",
                    "incremental_lift_units": "5.00",
                    "eligible_brand_cost": "100.00",
                    "incremental_contribution": "25.00",
                    "roi": "-0.7500",
                    "roi_withheld_reason": None,
                    "sku_cells": [{"product_id": 1, "register_units": 40}],
                }
            ],
        },
    )
    payload, n = get_store_velocity_and_roi(
        _ctx(), {"start_date": "2026-10-01", "end_date": "2026-10-31"}
    )
    assert n == 1
    assert payload["totals"]["roi"] == "-0.7500"
    assert payload["totals"]["incremental_lift_units"] == "5.00"
    assert "sku_cells" not in payload["store_weeks"][0]
    assert payload["store_weeks"][0]["store_label"] == "WF Market St"


def test_competitor_mentions_from_tags(monkeypatch):
    monkeypatch.setattr(
        "consumer_mentions.trends.visible_recaps",
        lambda tenant_id, start, end: {
            "C1": date(2026, 10, 5),
            "C2": date(2026, 10, 6),
            "C3": date(2026, 10, 7),
        },
    )
    monkeypatch.setattr(
        "recaps.comment_insights.tenant_names",
        lambda tenant_id: ("Alpha Brew", [(1, "Alpha IPA")]),
    )

    class _QS:
        def filter(self, **_k):
            return self

        def values_list(self, *a):
            return self

        def iterator(self):
            return iter(
                [
                    (
                        "C1",
                        [
                            {"key": "white-claw", "name": "White Claw"},
                            {"key": "alpha-brew", "name": "Alpha Brew"},
                        ],
                    ),
                    ("C2", [{"key": "white-claw", "name": "White Claw"}]),
                    ("C3", [{"key": "truly", "name": "Truly"}]),
                ]
            )

    monkeypatch.setattr(
        "consumer_mentions.models.RecapMentionTag.objects",
        SimpleNamespace(filter=lambda **_k: _QS()),
    )
    monkeypatch.setattr(
        "recaps.comment_insights.is_own_name",
        lambda norm, brand, products: "alpha" in norm,
    )
    payload, n = get_competitor_mentions(
        _ctx(), {"start_date": "2026-10-01", "end_date": "2026-10-31"}
    )
    assert payload["status"] == "ready"
    assert payload["recaps_tagged"] == 3
    brands = {r["brand"]: r["recaps_mentioning"] for r in payload["competitors"]}
    assert brands["White Claw"] == 2
    assert brands["Truly"] == 1
    assert "Alpha Brew" not in brands
    assert n == 2
