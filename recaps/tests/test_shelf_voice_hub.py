"""Unit tests for the Shelf Compliance, Pricing & Competitor Voice Hub."""

from __future__ import annotations

from recaps.shelf_voice_hub import (
    classify_mention_trend,
    classify_price_drift_pct,
    compute_before_after_pairs,
)


def test_classify_price_drift_pct():
    assert classify_price_drift_pct(2.99, 2.49) == 20.1
    assert classify_price_drift_pct(2.24, 2.49) == -10.0
    assert classify_price_drift_pct(2.49, 2.49) == 0.0
    assert classify_price_drift_pct(None, 2.49) is None
    assert classify_price_drift_pct(2.49, None) is None
    assert classify_price_drift_pct(2.49, 0.0) is None


def test_classify_mention_trend():
    assert classify_mention_trend(4, 0) == "new"
    assert classify_mention_trend(5, 2) == "rising"
    assert classify_mention_trend(3, 3) == "steady"
    assert classify_mention_trend(1, 4) == "cooling"
    assert classify_mention_trend(0, 0) == "steady"


def test_compute_before_after_pairs_orders_chronologically():
    photos = [
        {
            "file_id": "20",
            "store_key": "heb:725",
            "store_name": "HEB #725",
            "market": "Houston, TX",
            "state_code": "TX",
            "event_date": "2026-11-12",
            "photo_url": "https://example.com/after.jpg",
            "facings": 5,
        },
        {
            "file_id": "10",
            "store_key": "heb:725",
            "store_name": "HEB #725",
            "market": "Houston, TX",
            "state_code": "TX",
            "event_date": "2026-10-02",
            "photo_url": "https://example.com/before.jpg",
            "facings": 2,
        },
        {
            "file_id": "30",
            "store_key": "kroger:11",
            "store_name": "Kroger #11",
            "market": "Dallas, TX",
            "state_code": "TX",
            "event_date": "2026-11-01",
            "photo_url": "https://example.com/only.jpg",
            "facings": 3,
        },
    ]

    pairs = compute_before_after_pairs(photos)
    assert len(pairs) == 1
    assert pairs[0]["store_name"] == "HEB #725"
    assert pairs[0]["photo_count"] == 2
    assert pairs[0]["before_photo"]["file_id"] == "10"
    assert pairs[0]["after_photo"]["file_id"] == "20"


def test_shelf_voice_hub_runtime_imports_and_geo_extraction():
    """Verify every lazy runtime import used by build_shelf_voice_hub resolves."""
    from types import SimpleNamespace

    from events.market import parse_address_geo
    from events.retailer_aliases import accepted_alias_map, resolve_store_name
    from recaps.comment_insights import brand_key, normalise, written_in
    from recaps.feedback_themes import _short_quote, gather_comments
    from recaps.models import CustomRecap, CustomRecapFile, Recap
    from recaps.photo_gallery import _image_url
    from recaps.shelf_check import (
        NO,
        SHELF_CATEGORY_IREGEX,
        YES,
        _image_q,
        effective_answers,
        retail_recap_q,
        visit_answer,
    )
    from recaps.shelf_voice_hub import _extract_event_geo
    from recaps.store_places import store_header, tenant_places
    from recaps.store_signals import normalize_store_signals
    from recaps.tenant_overview import (
        _approved_only,
        _filter_event_window,
        _inclusive_dates_to_window,
    )
    from tenants.models import TenantCommentInsight

    assert all(
        obj is not None
        for obj in (
            parse_address_geo,
            accepted_alias_map,
            resolve_store_name,
            brand_key,
            normalise,
            written_in,
            _short_quote,
            gather_comments,
            CustomRecap,
            CustomRecapFile,
            Recap,
            _image_url,
            NO,
            SHELF_CATEGORY_IREGEX,
            YES,
            _image_q,
            effective_answers,
            retail_recap_q,
            visit_answer,
            store_header,
            tenant_places,
            normalize_store_signals,
            _approved_only,
            _filter_event_window,
            _inclusive_dates_to_window,
            TenantCommentInsight,
        )
    )

    ev = SimpleNamespace(
        retailer=SimpleNamespace(name="Whole Foods #104"),
        address="525 N Lamar Blvd, Austin, TX 78703",
        location=None,
        state=None,
        request=None,
    )
    name, market, state_code, _addr = _extract_event_geo(
        ev, SimpleNamespace(), header={}, aliases={}
    )
    assert name == "Whole Foods #104"
    assert market == "Austin, TX"
    assert state_code == "TX"

    # Compile every ORM queryset shape used in build_shelf_voice_hub to prove
    # zero FieldError regressions on select_related / filter field paths.
    for qs in (
        _approved_only(
            _filter_event_window(
                CustomRecap.objects.filter(tenant_id=1).filter(retail_recap_q()),
                "event__",
                _inclusive_dates_to_window(None, None),
            ),
            "",
        )
        .filter(archived_at__isnull=True)
        .select_related(
            "event",
            "event__retailer",
            "event__request",
            "event__request__retailer",
            "event__location",
            "event__location__state",
            "event__state",
            "retailer",
            "state",
        ),
        CustomRecapFile.objects.filter(
            custom_recap_id__in=[1],
            file_recap_category__name__iregex=SHELF_CATEGORY_IREGEX,
        )
        .filter(_image_q())
        .select_related("shelf_reading", "shelf_correction", "file_recap_category"),
        Recap.objects.filter(event__tenant_id=1, id__in=[1]).select_related(
            "event",
            "event__retailer",
            "event__request",
            "event__request__retailer",
            "event__location",
            "event__location__state",
            "event__state",
            "retailer",
            "state",
        ),
        TenantCommentInsight.objects.filter(tenant_id=1, kind="competitors").order_by(
            "-generated_at"
        ),
    ):
        sql, _params = qs.query.sql_with_params()
        assert "SELECT" in sql.upper()
