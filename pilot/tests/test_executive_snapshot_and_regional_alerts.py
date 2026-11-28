from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pilot.executive_snapshot import (
    compute_conversion_pct,
    compute_mom_delta,
    month_bounds_inclusive,
    rank_top_converting_doors,
    render_executive_snapshot_html,
    resolve_snapshot_month,
)
from pilot.regional_alerts import (
    evaluate_regional_alert_match,
    normalize_chain_key,
    normalize_chains,
    normalize_states,
)


def test_resolve_snapshot_month_defaults_to_prior_complete_month():
    assert resolve_snapshot_month(today=date(2026, 11, 1)) == (2026, 10)
    assert resolve_snapshot_month(today=date(2026, 1, 15)) == (2025, 12)
    assert resolve_snapshot_month(2026, 6, today=date(2026, 11, 1)) == (2026, 6)
    assert month_bounds_inclusive(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))


def test_compute_mom_delta_handles_pct_pp_and_zero_prior():
    up = compute_mom_delta(120, 100)
    assert up["direction"] == "up"
    assert up["label"] == "+20.0%"

    down = compute_mom_delta(80, 100)
    assert down["direction"] == "down"
    assert down["label"] == "-20.0%"

    pp = compute_mom_delta(34.5, 31.0, is_percent_points=True)
    assert pp["direction"] == "up"
    assert pp["label"] == "+3.5 pp"

    zero_prior = compute_mom_delta(42, 0)
    assert zero_prior["direction"] == "up"
    assert zero_prior["label"] == "+42"

    assert compute_conversion_pct(25, 100) == 25.0
    assert compute_conversion_pct(10, 0) is None


def test_rank_top_converting_doors_prioritizes_real_volume_and_caps_at_5():
    rows = [
        {"store_name": "Tiny Door", "market": "Austin, TX", "state_code": "TX", "recaps": 1, "consumers_sampled": 1, "units_sold": 1},
        {"store_name": "H-E-B #104", "market": "Austin, TX", "state_code": "TX", "recaps": 3, "consumers_sampled": 100, "units_sold": 45},
        {"store_name": "Whole Foods Lamar", "market": "Austin, TX", "state_code": "TX", "recaps": 2, "consumers_sampled": 80, "units_sold": 48},
        {"store_name": "Central Market", "market": "Dallas, TX", "state_code": "TX", "recaps": 2, "consumers_sampled": 60, "units_sold": 24},
        {"store_name": "Erewhon Venice", "market": "Venice, CA", "state_code": "CA", "recaps": 2, "consumers_sampled": 50, "units_sold": 18},
        {"store_name": "Sprouts #22", "market": "Denver, CO", "state_code": "CO", "recaps": 1, "consumers_sampled": 40, "units_sold": 10},
        {"store_name": "Kroger #5", "market": "Houston, TX", "state_code": "TX", "recaps": 1, "consumers_sampled": 30, "units_sold": 6},
    ]
    ranked = rank_top_converting_doors(rows, limit=5)
    assert len(ranked) == 5
    assert ranked[0]["store_name"] == "Whole Foods Lamar"
    assert ranked[0]["conversion_label"] == "60.0%"
    assert ranked[1]["store_name"] == "H-E-B #104"
    assert "Tiny Door" not in [r["store_name"] for r in ranked]


def test_render_executive_snapshot_html_escapes_and_includes_all_sections():
    html = render_executive_snapshot_html(
        {
            "tenant_name": "Acme & Co",
            "month_label": "October 2026",
            "prior_month_label": "September 2026",
            "kpis": [
                {"label": "Consumers Sampled", "formatted_value": "1,420", "delta_label": "+14.2%", "direction": "up"}
            ],
            "top_doors": [
                {"store_name": "H-E-B #104", "market": "Austin, TX", "recaps": 3, "consumers_sampled": 120, "units_sold": 54, "conversion_label": "45.0%"}
            ],
            "shelf": {
                "osa_rate_pct": 92.5,
                "price_tag_rate_pct": 88.0,
                "median_price": "4.99",
                "target_msrp": "4.49",
                "callouts": [
                    {"store_name": "H-E-B #202", "market": "Dallas, TX", "severity": "alert", "detail": "Out-of-stock gap logged"}
                ],
            },
            "themes": [{"title": "Refreshing citrus taste", "mentions": 19, "summary": "Loved chilled on shelf."}],
            "photos": [{"url": "https://cdn.example.com/p1.jpg", "store": "H-E-B #104", "event_date": "2026-10-18"}],
        }
    )
    assert "Acme &amp; Co" in html
    assert "October 2026" in html
    assert "Top 5 Converting Doors" in html
    assert "H-E-B #104" in html
    assert "92.5%" in html
    assert "Refreshing citrus taste" in html
    assert "https://cdn.example.com/p1.jpg" in html


def test_regional_alert_normalizers_and_matching_logic():
    assert normalize_states(["tx", "CA, tx", "ny"]) == ["TX", "CA", "NY"]
    with pytest.raises(ValueError):
        normalize_states(["TEXAS"])

    assert normalize_chains(["H-E-B, Whole Foods", "heb"]) == ["H-E-B", "Whole Foods"]
    assert normalize_chain_key("H-E-B #104 (Austin)") == "heb104austin"

    sub = SimpleNamespace(
        is_active=True,
        states=["TX"],
        chains=["H-E-B"],
        alert_oos=True,
        alert_price_drift=True,
        alert_missing_tag=True,
        target_msrp=Decimal("4.99"),
        drift_threshold_pct=Decimal("5.0"),
    )

    # Wrong state -> no match
    assert (
        evaluate_regional_alert_match(
            sub,
            store_name="H-E-B #104",
            state_code="CA",
            product_on_shelf="no",
        )
        is None
    )

    # Wrong chain -> no match
    assert (
        evaluate_regional_alert_match(
            sub,
            store_name="Whole Foods Lamar",
            state_code="TX",
            product_on_shelf="no",
        )
        is None
    )

    # Matching TX + HEB (punctuation normalized!) + OOS + Missing Tag + >5% Price Drift
    hit = evaluate_regional_alert_match(
        sub,
        store_name="HEB Plus #412",
        state_code="tx",
        product_on_shelf="no",
        out_of_stock_gap="yes",
        price_tag_visible="no",
        observed_price=Decimal("5.49"),
    )
    assert hit is not None
    assert hit["trigger_kind"] == "oos"
    assert set(hit["trigger_kinds"]) == {"oos", "missing_tag", "price_drift"}
    assert hit["drift_pct"] == 10.0

    # Price within 5% tolerance & on shelf & tag visible -> no alert
    assert (
        evaluate_regional_alert_match(
            sub,
            store_name="H-E-B #104",
            state_code="TX",
            product_on_shelf="yes",
            out_of_stock_gap="no",
            price_tag_visible="yes",
            observed_price=Decimal("5.10"),
        )
        is None
    )


def test_graphql_schema_builds_with_executive_snapshot_and_regional_alerts():
    from config.schema_client import schema_clients

    sdl = schema_clients.as_str()
    assert "executiveSnapshot(" in sdl
    assert "regionalAlertSubscriptions(" in sdl
    assert "updateExecutiveSnapshotConfig(" in sdl
    assert "sendExecutiveSnapshotNow(" in sdl
    assert "exportExecutiveSnapshotPdf(" in sdl
    assert "upsertRegionalAlertSubscription(" in sdl
    assert "deleteRegionalAlertSubscription(" in sdl
    assert "sendTestRegionalAlert(" in sdl
