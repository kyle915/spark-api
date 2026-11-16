"""Read-only legacy recap audit (``audit_legacy_recaps`` / ``/internal/cron/audit-legacy-recaps``).

Pins what a client deliverable relies on:
  * every legacy recap of the tenant is a row — including ones on a
    soft-deleted request and on a standalone event (no request), each with
    the right ``request_link`` and ``in_campaign_to_date`` flag,
  * approval / archive state is per recap,
  * KPI children (ConsumerEngagements) come through,
  * the window is by EVENT date,
  * other tenants never leak in,
  * BA identity is an opaque id: no names or emails anywhere in the payload,
  * it writes nothing.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_tz

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.legacy_recap_audit import build_legacy_recap_audit

SECRET = "test-cron-secret-value-only-for-tests"


@pytest.mark.django_db(transaction=True)
class TestLegacyRecapAudit(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Legacy Brand")
        self.other = self.create_tenant(name="Other Brand")

        user = self.create_user(
            username="casey-quill-legacy", email="casey.quill@ba.test", role=self.roles["ambassador"]
        )
        user.first_name, user.last_name = "Casey", "Quill"
        user.save()
        self.ba = self.create_ambassador(user)

        self.live_req = self._request(self.tenant, "Live")
        self.dead_req = self._request(self.tenant, "Dead")
        event_models.Request.objects.filter(id=self.dead_req.id).update(deleted_at=timezone.now())

        feb = datetime(2026, 2, 10, 15, tzinfo=dt_tz.utc)
        self.r_live = self._recap(self._event(self.tenant, "Kroger 1", feb, self.live_req), approved=True, consumers=50)
        self.r_dead = self._recap(self._event(self.tenant, "Kroger 2", feb, self.dead_req), approved=True, consumers=40)
        self.r_solo = self._recap(self._event(self.tenant, "Kroger 3", feb, None), approved=False, consumers=30)
        self.r_old = self._recap(
            self._event(self.tenant, "Old", datetime(2025, 6, 1, tzinfo=dt_tz.utc), self.live_req),
            approved=True, consumers=10,
        )
        recap_models.Recap.objects.filter(id=self.r_old.id).update(archived_at=timezone.now())
        self._recap(self._event(self.other, "Other", feb, None), approved=True, consumers=999)

    def _request(self, tenant, name):
        return event_models.Request.objects.create(
            name=name,
            address="1 Main St",
            tenant=tenant,
            status=self.create_request_status(name="Approved", tenant=tenant, slug="approved"),
            request_type=self.create_request_type(name="Retail", tenant=tenant),
            created_by=self.system_user,
        )

    def _event(self, tenant, name, when, request):
        event = self.create_event(name=name, tenant=tenant, date=when, start_time=when,
                                  end_time=when + timedelta(hours=3))
        if request is not None:
            event_models.Event.objects.filter(id=event.id).update(request=request)
            event.refresh_from_db()
        return event

    def _recap(self, event, *, approved, consumers):
        recap = recap_models.Recap.objects.create(
            name=f"recap {event.name}", event=event, approved=approved,
            total_engagements=consumers, total_cans_sold=3, total_packs_sold=2, products_sold=5,
            ambassador=self.ba, created_by=self.system_user, updated_by=self.system_user,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=recap, total_consumer=consumers, first_time_consumers=consumers // 2,
            created_by=self.system_user, updated_by=self.system_user,
        )
        return recap

    def test_every_recap_with_request_link_and_approval(self):
        payload = build_legacy_recap_audit(self.tenant.id)
        rows = {r["recap_id"]: r for r in payload["rows"]}
        assert set(rows) == {self.r_live.id, self.r_dead.id, self.r_solo.id, self.r_old.id}

        assert rows[self.r_live.id]["request_link"] == "live_request"
        assert rows[self.r_live.id]["in_campaign_to_date"] is True
        assert rows[self.r_dead.id]["request_link"] == "request_soft_deleted"
        assert rows[self.r_dead.id]["request_deleted_at"]
        assert rows[self.r_dead.id]["in_campaign_to_date"] is False
        assert rows[self.r_solo.id]["request_link"] == "no_request"
        assert rows[self.r_solo.id]["request_id"] is None

        assert rows[self.r_solo.id]["approved"] is False
        assert rows[self.r_old.id]["approved"] is True
        assert rows[self.r_old.id]["client_visible"] is False  # archived
        assert rows[self.r_live.id]["consumers"] == 50
        assert rows[self.r_live.id]["first_time_consumers"] == 25
        assert rows[self.r_live.id]["event_date"] == "2026-02-10"
        assert payload["summary"]["approved_in_live_request"] == 1

    def test_window_is_by_event_date(self):
        payload = build_legacy_recap_audit(self.tenant.id, start="2026-01-01", end="2026-11-16")
        ids = {r["recap_id"] for r in payload["rows"]}
        assert self.r_old.id not in ids
        assert payload["summary"]["out_of_window"] == 1

    def test_no_ba_names_or_emails_and_no_writes(self):
        before = (recap_models.Recap.objects.count(), event_models.Request.objects.count())
        out = io.StringIO()
        call_command("audit_legacy_recaps", tenant_id=self.tenant.id, as_json=True, stdout=out)
        raw = out.getvalue()
        assert "Casey" not in raw and "Quill" not in raw and "casey.quill" not in raw
        blob = raw.split("LEGACYRECAPS_JSON_START", 1)[1].split("LEGACYRECAPS_JSON_END", 1)[0]
        data = json.loads(blob)
        assert data["rows"][0]["ambassador_id"] == self.ba.id
        assert (recap_models.Recap.objects.count(), event_models.Request.objects.count()) == before

    @override_settings(INTERNAL_CRON_SECRET=SECRET)
    def test_cron_endpoint(self):
        client = Client()
        assert client.post("/internal/cron/audit-legacy-recaps", {"tenant_id": self.tenant.id}).status_code == 401
        resp = client.post(
            "/internal/cron/audit-legacy-recaps", {"tenant_id": self.tenant.id}, HTTP_X_CRON_SECRET=SECRET
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["row_count"] == 4
        bad = client.post("/internal/cron/audit-legacy-recaps", {"tenant_id": "x"}, HTTP_X_CRON_SECRET=SECRET)
        assert bad.status_code == 400
        missing = client.post("/internal/cron/audit-legacy-recaps", {"tenant_id": 999999}, HTTP_X_CRON_SECRET=SECRET)
        assert missing.status_code == 400
