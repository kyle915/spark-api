"""Unit tests for Saved Report Views, Outbound Webhooks, and Sample Brand Sandbox."""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_tz
from types import SimpleNamespace

import pytest

from pilot.models import SavedReportView, WebhookDelivery, WebhookEndpoint
from pilot.sandbox import build_sandbox_sample_brand
from pilot.saved_views import (
    describe_saved_view_filters,
    is_saved_view_due_today,
    resolve_saved_view_window,
    sanitize_saved_view_filters,
)
from pilot.webhooks import (
    generate_webhook_secret,
    mask_webhook_secret,
    normalize_webhook_events,
    sign_webhook_payload,
    validate_webhook_url,
    verify_webhook_signature,
)


def test_saved_view_filter_sanitization_and_windows():
    cleaned = sanitize_saved_view_filters(
        {
            "range": "90d",
            "retailerFilter": "  Whole Foods Market  ",
            "marketFilter": "TX",
            "unknownKey": "should-be-dropped",
            "storeFilter": "",
        }
    )
    assert cleaned == {
        "range": "90d",
        "retailerFilter": "Whole Foods Market",
        "marketFilter": "TX",
    }
    assert "Retailer: Whole Foods Market" in describe_saved_view_filters(cleaned)
    assert "Market: TX" in describe_saved_view_filters(cleaned)

    s, e, label = resolve_saved_view_window({"range": "90d"}, today=date(2026, 11, 27))
    assert s == date(2026, 8, 30)
    assert e == date(2026, 11, 27)
    assert "Last 90d" in label

    s_all, e_all, label_all = resolve_saved_view_window({"range": "all"})
    assert s_all is None and e_all is None
    assert label_all == "All time"


def test_saved_view_schedule_due_logic():
    friday = date(2026, 11, 27)  # Friday -> weekday() == 4
    assert friday.weekday() == 4

    view = SimpleNamespace(
        schedule_enabled=True,
        schedule_cadence=SavedReportView.CADENCE_WEEKLY,
        schedule_day_of_week=4,
        last_sent_at=None,
    )
    assert is_saved_view_due_today(view, today=friday) is True
    assert is_saved_view_due_today(view, today=date(2026, 11, 26)) is False

    # Already sent today -> not due again
    view.last_sent_at = datetime(2026, 11, 27, 8, 0, tzinfo=dt_tz.utc)
    assert is_saved_view_due_today(view, today=friday) is False

    # Biweekly sent 7 days ago -> not due yet; 14 days ago -> due
    view.schedule_cadence = SavedReportView.CADENCE_BIWEEKLY
    view.last_sent_at = datetime(2026, 11, 20, 8, 0, tzinfo=dt_tz.utc)
    assert is_saved_view_due_today(view, today=friday) is False
    view.last_sent_at = datetime(2026, 11, 13, 8, 0, tzinfo=dt_tz.utc)
    assert is_saved_view_due_today(view, today=friday) is True


def test_webhook_url_ssrf_guard_and_hmac_signing():
    assert validate_webhook_url("https://hooks.zapier.com/hooks/catch/123/abc/") == (
        "https://hooks.zapier.com/hooks/catch/123/abc/"
    )

    for bad in (
        "",
        "http://hooks.zapier.com/insecure",
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/internal",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://user:pass@hooks.zapier.com/hook",
    ):
        with pytest.raises(ValueError):
            validate_webhook_url(bad)

    events = normalize_webhook_events(
        ["recap.approved", "shelf.oos_detected", "recap.approved"]
    )
    assert events == ["recap.approved", "shelf.oos_detected"]
    with pytest.raises(ValueError):
        normalize_webhook_events(["evil.event"])
    with pytest.raises(ValueError):
        normalize_webhook_events([])

    secret = generate_webhook_secret()
    assert secret.startswith("whsec_")
    assert "••••" in mask_webhook_secret(secret)

    raw = b'{"event":"recap.approved","data":{"recapUuid":"123"}}'
    sig = sign_webhook_payload(secret, 1795800000, raw)
    assert verify_webhook_signature(secret, sig, raw, now_ts=1795800030) is True
    assert verify_webhook_signature(secret, sig, raw + b" ", now_ts=1795800030) is False
    assert verify_webhook_signature(secret, sig, raw, now_ts=1795801000) is False


def test_sandbox_brand_payload_and_model_sql_compilation():
    import json

    from django.test import RequestFactory
    from pilot.views import public_sandbox_brand_view

    payload = build_sandbox_sample_brand(today=date(2026, 11, 27))
    assert payload["brand"]["name"] == "Meadowlark Botanicals"
    assert payload["kpis"]["activations"] >= 30
    assert len(payload["stores"]) >= 8
    assert len(payload["shelfPhotos"]) >= 6
    assert len(payload["beforeAfterPairs"]) >= 2
    assert len(payload["competitorMarkets"]) >= 4
    assert all(
        p["photoUrl"].startswith("data:image/svg+xml")
        for p in payload["shelfPhotos"]
    )

    # Confirm zero real brands, real retailers, or real street addresses appear.
    serialized = json.dumps(payload).lower()
    for forbidden in (
        "whole foods",
        "h-e-b",
        "erewhon",
        "sprouts",
        "central market",
        "poppi",
        "olipop",
        "liquid death",
        "spindrift",
        "ghia",
        "lamar blvd",
        "wilshire blvd",
        "bedford ave",
        "kingsbury st",
        "sepulveda blvd",
        "colfax ave",
        "park ln",
        "lovers ln",
        "alabama st",
    ):
        assert forbidden not in serialized, f"Real entity leaked in sandbox: {forbidden}"

    # Verify admin-only access on /api/public/sandbox-brand.
    rf = RequestFactory()
    anon_req = rf.get("/api/public/sandbox-brand")
    anon_resp = public_sandbox_brand_view(anon_req)
    assert anon_resp.status_code == 401

    client_req = rf.get("/api/public/sandbox-brand")
    client_req.user = SimpleNamespace(
        pk=None,
        is_authenticated=True,
        is_active=True,
        is_staff=False,
        is_superuser=False,
        email="founder@brand.example",
        role=SimpleNamespace(slug="client"),
    )
    client_resp = public_sandbox_brand_view(client_req)
    assert client_resp.status_code == 403

    admin_req = rf.get("/api/public/sandbox-brand")
    admin_req.user = SimpleNamespace(
        pk=None,
        is_authenticated=True,
        is_active=True,
        is_staff=True,
        is_superuser=False,
        email="ops@igniteproductions.co",
        role=SimpleNamespace(slug="spark-admin"),
    )
    admin_resp = public_sandbox_brand_view(admin_req)
    assert admin_resp.status_code == 200
    assert admin_resp["Cache-Control"] == "private, no-store"
    assert json.loads(admin_resp.content)["brand"]["name"] == "Meadowlark Botanicals"

    # Compile ORM queries for all 3 new models to verify zero FieldError issues.
    for qs in (
        SavedReportView.objects.filter(tenant_id=1, surface="buyer_report"),
        WebhookEndpoint.objects.filter(tenant_id=1, is_active=True),
        WebhookDelivery.objects.filter(tenant_id=1, status="delivered"),
    ):
        sql, _params = qs.query.sql_with_params()
        assert "SELECT" in sql.upper()
