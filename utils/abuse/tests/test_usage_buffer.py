"""Tests for Spark backend speed upgrades: GZipMiddleware, buffered usage
rollups, non-gated LegalAcceptanceGate caching, and upload thumb pre-gen."""

from __future__ import annotations

import gzip
import io
from datetime import datetime, timezone
from unittest import mock

import pytest
from django.core.cache import cache
from django.http import JsonResponse
from django.middleware.gzip import GZipMiddleware
from django.test import RequestFactory
from PIL import Image

from legal import service as legal_service
from tenants.tests.base import BaseGraphQLTestCase
from utils.abuse.identity import Identity
from utils.abuse.models import UsageBucket
from utils.abuse.usage import (
    clear_usage_buffer,
    flush_usage_buffer,
    record_usage,
)
from utils.thumb_views import materialize_thumbs_from_bytes


def test_gzip_middleware_enabled_and_compresses_graphql_json(settings):
    assert "django.middleware.gzip.GZipMiddleware" in settings.MIDDLEWARE
    payload = {"data": {"items": [{"id": str(i), "name": f"Store #{i}"} for i in range(60)]}}
    req = RequestFactory().post(
        "/api/v615204/graphql/clients",
        HTTP_ACCEPT_ENCODING="gzip, deflate, br",
    )
    mw = GZipMiddleware(lambda _r: JsonResponse(payload))
    resp = mw(req)
    assert resp.headers.get("Content-Encoding") == "gzip"
    decompressed = gzip.decompress(resp.content)
    assert len(resp.content) < len(decompressed) * 0.5


@pytest.mark.django_db
class TestBufferedUsageRollups(BaseGraphQLTestCase):
    def setup_method(self):
        clear_usage_buffer()

    def teardown_method(self):
        clear_usage_buffer()

    def test_coalesces_in_memory_then_flushes_exact_totals(self, settings):
        roles = self.setup_default_roles()
        tenant = self.create_tenant(name="Fast Brand")
        user = self.create_user("fast1", "fast1@brand.test", roles["client"])
        ident = Identity(
            user_id=user.pk,
            email=user.email,
            tenant_id=tenant.id,
            is_admin=False,
            role_slug="client",
        )
        settings.ABUSE_USAGE_FLUSH_INTERVAL_SECONDS = 60.0
        now = datetime(2026, 11, 28, 18, 15, tzinfo=timezone.utc)

        for i in range(5):
            record_usage(
                ident,
                "DashboardQuery",
                ip=f"203.0.113.{i % 2 + 1}",
                ua="Mozilla/5.0 Chrome",
                error=(i == 4),
                paged=(i % 2 == 0),
                response_bytes=1000,
                now=now,
            )

        # Zero DB rows written while inside the 60s coalescing window.
        assert UsageBucket.objects.filter(user_id=user.pk).count() == 0

        flushed = flush_usage_buffer()
        assert flushed == 1

        row = UsageBucket.objects.get(user_id=user.pk, operation="DashboardQuery")
        assert row.request_count == 5
        assert row.error_count == 1
        assert row.paged_count == 3
        assert row.response_bytes == 5000
        assert set(row.ips) == {"203.0.113.1", "203.0.113.2"}


@pytest.mark.django_db
class TestNonGatedLegalCache(BaseGraphQLTestCase):
    def test_non_gated_user_cached_after_first_lookup(self):
        cache.clear()
        roles = self.setup_default_roles()
        ba_user = self.create_user("ba_fast", "ba_fast@example.com", roles["ambassador"])
        assert legal_service.needs_acceptance(ba_user.pk) is False

        with mock.patch.object(legal_service, "_user_signals") as spy:
            assert legal_service.needs_acceptance(ba_user.pk) is False
            spy.assert_not_called()


def test_materialize_thumbs_from_bytes_generates_w400_and_w800():
    img = Image.new("RGB", (1600, 1200), color=(40, 120, 60))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    raw = buf.getvalue()

    uploaded: list[tuple[str, int, str]] = []

    def fake_upload(blob_name: str, data: bytes, content_type: str = ""):
        uploaded.append((blob_name, len(data), content_type))

    with mock.patch("utils.thumb_views.upload_bytes", side_effect=fake_upload):
        written = materialize_thumbs_from_bytes("recaps/sample-photo.jpg", raw)

    assert written == [
        "thumbs/w400/recaps/sample-photo.jpg.jpg",
        "thumbs/w800/recaps/sample-photo.jpg.jpg",
    ]
    assert [u[0] for u in uploaded] == written
    assert all(u[2] == "image/jpeg" and u[1] > 0 for u in uploaded)
