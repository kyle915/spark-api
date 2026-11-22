"""Self-serve onboarding → Close lead sync (UTM attribution)."""

from __future__ import annotations

import pytest

from sales_assist.close_client import CloseClient, CloseError
from sales_assist.marketing_lead import (
    NOTE_MARKER,
    lead_contact_emails,
    sanitize_utm,
    sync_self_serve_signup_to_close,
)
from sales_assist.models import CloseLink
from sales_assist.tests.test_sales_assist import FakeCloseSession, FakeResponse
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


def test_lead_contact_emails_includes_all_contacts():
    lead = {
        "contacts": [
            {"emails": [{"email": "a@brand.test"}, {"email": "b@brand.test"}]},
            {"emails": [{"email": "c@brand.test"}]},
            {"emails": [{"email": ""}]},
        ]
    }
    assert lead_contact_emails(lead) == {"a@brand.test", "b@brand.test", "c@brand.test"}


@pytest.mark.django_db(transaction=True)
def test_sync_matches_secondary_contact_email(settings):
    settings.CLOSE_API_KEY = "k_test"
    settings.STANDALONE_SIGNUP_CLOSE_SYNC_ENABLED = True
    session = FakeCloseSession(
        leads=[
            {
                "id": "lead_existing",
                "name": "Other Name",
                "display_name": "Other Name",
                "contacts": [
                    {
                        "id": "cont_1",
                        "name": "Primary",
                        "emails": [{"email": "primary@other.test"}],
                    },
                    {
                        "id": "cont_2",
                        "name": "Riley",
                        "emails": [{"email": "owner@brand.test"}],
                    },
                ],
            }
        ]
    )
    client = CloseClient("k_test", session=session)
    user = User.objects.create_user(
        username="utm-secondary",
        email="owner@brand.test",
        password="x",
    )
    tenant = Tenant.objects.create(
        name="Canyon Spritz Co",
        slug="canyon-secondary",
        request_url_name="canyon-secondary",
        created_by=user,
        updated_by=user,
    )
    result = sync_self_serve_signup_to_close(
        tenant=tenant,
        user=user,
        brand_name="Canyon Spritz Co",
        email="owner@brand.test",
        client=client,
    )
    assert result["ok"] is True
    assert result["created"] is False
    assert result["close_lead_id"] == "lead_existing"
    assert not any(c[1] == "/lead/" for c in session.calls if c[0] == "POST")


@pytest.mark.django_db(transaction=True)
def test_sync_aborts_create_when_lookup_errors(settings):
    settings.CLOSE_API_KEY = "k_test"
    settings.STANDALONE_SIGNUP_CLOSE_SYNC_ENABLED = True

    class FailingSearchSession(FakeCloseSession):
        def request(self, method, url, params=None, json=None, timeout=None):
            path = url.replace(
                "https://api.close.com/api/v1",
                "",
            )
            if method == "GET" and path == "/lead/":
                return FakeResponse(500, {"error": "boom"})
            return super().request(method, url, params=params, json=json, timeout=timeout)

    session = FailingSearchSession()
    client = CloseClient("k_test", session=session)
    user = User.objects.create_user(
        username="utm-lookup-fail",
        email="fail@brand.test",
        password="x",
    )
    tenant = Tenant.objects.create(
        name="Lookup Fail Co",
        slug="lookup-fail-co",
        request_url_name="lookup-fail-co",
        created_by=user,
        updated_by=user,
    )
    result = sync_self_serve_signup_to_close(
        tenant=tenant,
        user=user,
        brand_name="Lookup Fail Co",
        email="fail@brand.test",
        client=client,
    )
    assert result["ok"] is False
    assert result["reason"].startswith("lookup-failed:")
    assert not any(c[1] == "/lead/" and c[0] == "POST" for c in session.calls)
    assert CloseLink.objects.filter(tenant=tenant).count() == 0
