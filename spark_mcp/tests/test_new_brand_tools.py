"""Shelf / cost / POS / competitor MCP tools: honest numbers + agency isolation."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from spark_mcp.tools import (
    ToolContext,
    ToolInputError,
    _brand_workspace_access,
    _limit_cost_rollups,
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


def _rollup(key: str, label: str, known: str, *, incomplete: bool = False) -> dict:
    return {
        "key": key,
        "label": label,
        "visit_count": 2,
        "complete_visit_count": 0 if incomplete else 2,
        "incomplete_visit_count": 2 if incomplete else 0,
        "total_cost": None if incomplete else known,
        "known_total_cost": known,
        "supported_cost": known,
        "invoiced_cost": known,
        "paid_cost": "0.00",
        "estimated_cost": "0.00",
        "has_incomplete_costs": incomplete,
        "cost_per_sample": None if incomplete else "1.00",
        "cost_per_observed_unit": None,
    }


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
        lambda *_a, **_k: [_rollup("s1", "Alpha Market", "500.00")],
    )
    payload, n = get_demo_cost_summary(
        _ctx(),
        {"start_date": "2026-10-01", "end_date": "2026-10-31", "group_by": "store"},
    )
    assert n == 1
    assert payload["available"] is True
    assert payload["totals"]["total_cost"] == "500.00"
    assert payload["rollups"][0]["total_cost"] == "500.00"


def test_demo_cost_folds_tail_into_other_and_keeps_totals():
    rows = [_rollup(f"s{i}", f"Store {i}", f"{100 - i}.00") for i in range(30)]
    limited, totals, folded = _limit_cost_rollups(rows)
    assert folded == 6  # 30 - 24 head = 6 in Other (25 slots: 24 + Other)
    assert len(limited) == 25
    assert limited[-1]["key"] == "other"
    assert limited[-1]["is_other_bucket"] is True
    assert limited[-1]["other_bucket_count"] == 6
    # Brand-wide known total includes every store, not just the page.
    assert totals["known_total_cost"] == "2565.00"  # sum 100..71
    assert totals["total_cost"] == "2565.00"
    assert totals["visit_count"] == 60


def test_pos_aligns_start_to_monday_and_ranks(monkeypatch):
    access = SimpleNamespace(tenant=SimpleNamespace(id=1), can_view_brand_consolidated=True)
    monkeypatch.setattr("spark_mcp.tools._brand_workspace_access", lambda ctx: access)
    captured = {}

    def _reader(_access, **kwargs):
        captured.update(kwargs)
        return {
            "available": True,
            "unavailable_message": None,
            "retailer_banners": ["Whole Foods"],
            "coverage": {
                "matched_store_sku_weeks": 4,
                "eligible_store_sku_weeks": 5,
                "coverage_ratio": "0.8000",
                "unmatched_row_count": 1,
                "missing_rows": [],
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
                    "store_key": "low",
                    "store_label": "Low Lift",
                    "week_start": "2026-10-06",
                    "register_units": 10,
                    "crew_purchased_units": 0,
                    "adjusted_register_units": 10,
                    "observed_vs_baseline_diff": "1.00",
                    "incremental_lift_units": "1.00",
                    "eligible_brand_cost": "10.00",
                    "incremental_contribution": "5.00",
                    "roi": "0.0000",
                    "roi_withheld_reason": None,
                    "sku_cells": [{"product_id": 1}],
                },
                {
                    "store_key": "high",
                    "store_label": "High Lift",
                    "week_start": "2026-10-13",
                    "register_units": 40,
                    "crew_purchased_units": 2,
                    "adjusted_register_units": 38,
                    "observed_vs_baseline_diff": "9.00",
                    "incremental_lift_units": "9.00",
                    "eligible_brand_cost": "100.00",
                    "incremental_contribution": "45.00",
                    "roi": "-0.5500",
                    "roi_withheld_reason": None,
                    "sku_cells": [{"product_id": 1}],
                },
            ],
        }

    monkeypatch.setattr("pilot.pos.build_retailer_pos_measurement", _reader)
    # Wednesday start → Monday 2026-10-28
    payload, n = get_store_velocity_and_roi(
        _ctx(), {"start_date": "2026-10-28", "end_date": "2026-11-10"}
    )
    assert captured["period_start"] == date(2026, 10, 26)  # Monday of that week
    assert payload["period_start"] == "2026-10-26"
    assert payload["start_date"] == "2026-10-28"
    assert n == 2
    assert payload["store_weeks"][0]["store_key"] == "high"
    assert "sku_cells" not in payload["store_weeks"][0]
    assert payload["next_offset"] is None


def test_pos_store_week_offset_pagination(monkeypatch):
    access = SimpleNamespace(tenant=SimpleNamespace(id=1), can_view_brand_consolidated=True)
    monkeypatch.setattr("spark_mcp.tools._brand_workspace_access", lambda ctx: access)

    weeks = [
        {
            "store_key": f"s{i}",
            "store_label": f"Store {i}",
            "week_start": "2026-10-06",
            "register_units": i,
            "crew_purchased_units": 0,
            "adjusted_register_units": i,
            "observed_vs_baseline_diff": str(i),
            "incremental_lift_units": str(i),
            "eligible_brand_cost": "1.00",
            "incremental_contribution": str(i),
            "roi": "0.0000",
            "roi_withheld_reason": None,
            "sku_cells": [],
        }
        for i in range(30)
    ]
    monkeypatch.setattr(
        "pilot.pos.build_retailer_pos_measurement",
        lambda *_a, **_k: {
            "available": True,
            "unavailable_message": None,
            "retailer_banners": [],
            "coverage": {
                "matched_store_sku_weeks": 30,
                "eligible_store_sku_weeks": 30,
                "coverage_ratio": "1.0000",
                "unmatched_row_count": 0,
                "missing_rows": [],
            },
            "totals": {
                "rep_observed_units": 0,
                "register_units": 435,
                "crew_purchased_units": 0,
                "adjusted_register_units": 435,
                "observed_vs_baseline_diff": None,
                "incremental_lift_units": None,
                "incremental_contribution": None,
                "eligible_brand_cost": None,
                "roi": None,
                "lift_withheld_reason": None,
                "roi_withheld_reason": None,
            },
            "store_weeks": weeks,
        },
    )
    page1, _ = get_store_velocity_and_roi(
        _ctx(), {"start_date": "2026-10-01", "end_date": "2026-10-31"}
    )
    assert page1["returned_store_weeks"] == 25
    assert page1["store_week_count"] == 30
    assert page1["next_offset"] == 25
    assert page1["store_weeks"][0]["store_key"] == "s29"  # highest lift

    page2, _ = get_store_velocity_and_roi(
        _ctx(),
        {"start_date": "2026-10-01", "end_date": "2026-10-31", "offset": 25},
    )
    assert page2["returned_store_weeks"] == 5
    assert page2["offset"] == 25
    assert page2["next_offset"] is None
    assert page2["store_weeks"][-1]["store_key"] == "s0"


def test_brand_access_prefers_brand_role_over_provider_ui_context(monkeypatch):
    """Active provider UI context must not deny brand-consolidated tools."""
    from pilot.models import WorkspaceMembership
    from pilot.permissions import ResolvedWorkspaceAccess

    user = SimpleNamespace(pk=9, is_active=True, role=None, is_authenticated=True)
    tenant = SimpleNamespace(id=1, pk=1)
    brand_mem = SimpleNamespace(
        workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        provider=None,
        can_approve_recaps=True,
        can_approve_finance=True,
        can_approve_spend=True,
    )

    class _UserQS:
        def select_related(self, *a):
            return self

        def filter(self, **k):
            return self

        def first(self):
            return user

    class _TenantQS:
        def filter(self, **k):
            return self

        def first(self):
            return tenant

    class _MemQS:
        def __init__(self, result):
            self._result = result

        def select_related(self, *a):
            return self

        def filter(self, **k):
            # Brand-role query returns brand_mem; provider-only check returns empty.
            roles = k.get("workspace_role__in")
            if roles == WorkspaceMembership.BRAND_ROLES:
                self._result = brand_mem
            elif roles == WorkspaceMembership.PROVIDER_ROLES:
                self._result = None
            return self

        def order_by(self, *a):
            return self

        def first(self):
            return self._result

        def exists(self):
            return self._result is not None

    monkeypatch.setattr(
        "django.contrib.auth.get_user_model",
        lambda: SimpleNamespace(objects=_UserQS()),
    )
    monkeypatch.setattr(
        "tenants.models.Tenant.objects",
        _TenantQS(),
    )
    monkeypatch.setattr(
        "pilot.models.WorkspaceMembership.objects",
        _MemQS(None),
    )
    monkeypatch.setattr(
        "pilot.permissions.is_ignite_staff_user",
        lambda u: False,
    )

    access = _brand_workspace_access(_ctx())
    assert isinstance(access, ResolvedWorkspaceAccess)
    assert access.can_view_brand_consolidated is True
    assert access.workspace_role == WorkspaceMembership.ROLE_BRAND_ADMIN
    assert access.provider is None


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
