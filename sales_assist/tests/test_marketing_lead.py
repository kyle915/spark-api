"""Self-serve onboarding → Close lead sync (UTM attribution)."""

from __future__ import annotations

import pytest

from sales_assist.close_client import CloseClient
from sales_assist.marketing_lead import (
    NOTE_MARKER,
    sanitize_utm,
    sync_self_serve_signup_to_close,
)
from sales_assist.models import CloseLink
from sales_assist.tests.test_sales_assist import FakeCloseSession
from tenants.models import Tenant, User


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"utm_source": "google", "utm_campaign": "spark-pricing"}, {"utm_source": "google", "utm_campaign": "spark-pricing"}),
        ({"utm_source": "bad@email.com"}, {}),
        ({"utm_id": "abc-123", "utm_term": "field+ops"}, {"utm_id": "abc-123", "utm_term": "field+ops"}),
        ({"utm_source": "x", "extra": "drop"}, {"utm_source": "x"}),
        (None, {}),
    ],
)
def test_sanitize_utm(raw, expected):
    assert sanitize_utm(raw) == expected


@pytest.mark.django_db(transaction=True)
def test_sync_creates_lead_note_and_link(settings):
    settings.CLOSE_API_KEY = "k_test"
    settings.STANDALONE_SIGNUP_CLOSE_SYNC_ENABLED = True
    session = FakeCloseSession()
    client = CloseClient("k_test", session=session)
    user = User.objects.create_user(
        username="utm-owner",
        email="owner@brand.test",
        password="x",
    )
    tenant = Tenant.objects.create(
        name="Canyon Spritz Co",
        slug="canyon-spritz-co",
        request_url_name="canyon-spritz-co",
        created_by=user,
        updated_by=user,
    )

    result = sync_self_serve_signup_to_close(
        tenant=tenant,
        user=user,
        brand_name="Canyon Spritz Co",
        email="owner@brand.test",
        first_name="Riley",
        last_name="Canyon",
        utm={
            "utm_source": "ignite_web",
            "utm_medium": "spark_marketing",
            "utm_campaign": "spark-pricing",
            "utm_content": "start_free",
        },
        landing_path="/onboarding",
        client=client,
    )

    assert result["ok"] is True
    assert result["created"] is True
    assert result["close_lead_id"].startswith("lead_")
    assert CloseLink.objects.get(tenant=tenant).close_lead_id == result["close_lead_id"]

    post_calls = [c for c in session.calls if c[0] == "POST"]
    assert any(c[1] == "/lead/" for c in post_calls)
    note_calls = [c for c in post_calls if c[1] == "/activity/note/"]
    assert note_calls
    note_text = note_calls[0][2]["note"]
    assert NOTE_MARKER in note_text
    assert "utm_campaign: spark-pricing" in note_text
    assert "utm_content: start_free" in note_text


@pytest.mark.django_db(transaction=True)
def test_sync_noops_without_close_key(settings):
    settings.CLOSE_API_KEY = ""
    user = User.objects.create_user(username="utm-noop", email="n@b.test", password="x")
    tenant = Tenant.objects.create(
        name="Noop Brand",
        slug="noop-brand",
        request_url_name="noop-brand",
        created_by=user,
        updated_by=user,
    )
    result = sync_self_serve_signup_to_close(
        tenant=tenant,
        user=user,
        brand_name="Noop Brand",
        email="n@b.test",
        utm={"utm_source": "google"},
    )
    assert result["ok"] is False
    assert result["reason"] == "close-not-configured"
    assert CloseLink.objects.filter(tenant=tenant).count() == 0
