"""audit_request_statuses: no-status requests + tenants without a default status."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.utils import timezone

from events import models as em
from events.tests.base import EventsGraphQLTestCase
from utils.mailer import Mailer

NY = ZoneInfo("America/New_York")


@pytest.mark.django_db
class TestAuditRequestStatuses(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.sys = self.get_system_user()
        self.torch = self.create_tenant(name="Torch THC", slug="torch-thc")
        self.ld = self.create_tenant(name="Liquid Death", slug="liquid-death")
        self.statuses = {}
        for tenant, default in ((self.torch, False), (self.ld, True)):
            for slug in ("pending", "approved"):
                self.statuses[(tenant.slug, slug)] = em.RequestStatus.objects.create(
                    tenant=tenant, name=slug.title(), slug=slug, created_by=self.sys
                )
                em.EventStatus.objects.create(
                    tenant=tenant, name=slug.title(), slug=slug, created_by=self.sys
                )
        em.RequestStatus.objects.filter(tenant=self.ld, slug="pending").update(is_default=True)
        em.EventStatus.objects.filter(tenant=self.ld, slug="pending").update(is_default=True)
        self.request_type = em.RequestType.objects.create(
            tenant=self.torch, name="Event Activation", created_by=self.sys
        )
        self.today = timezone.localdate()

    def _request(self, name, days_out, *, tenant=None, status=None, created_by="sys"):
        tenant = tenant or self.torch
        starts = datetime.combine(self.today + timedelta(days=days_out), time(17, 0), tzinfo=NY)
        request = em.Request.objects.create(
            name=name,
            date=starts,
            start_time=starts,
            address="1 Main St, Houston, TX 77002",
            request_type=self.request_type,
            tenant=tenant,
            created_by=self.sys if created_by == "sys" else None,
        )
        em.Request.objects.filter(id=request.id).update(status=status)
        request.refresh_from_db()
        return request

    def _run(self, *args):
        out = StringIO()
        with patch.object(Mailer, "send") as send, patch.object(Mailer, "send_now") as send_now:
            call_command("audit_request_statuses", *args, stdout=out)
        assert not send.called and not send_now.called
        return out.getvalue()

    def test_dry_run_reports_and_writes_nothing(self):
        plan_req = self._request("RARE Steak", 1)
        em.FieldMarketingEvent.objects.create(
            tenant=self.torch,
            request=plan_req,
            market="houston",
            activity="event_activation",
            name="RARE Steak",
            starts_on=self.today + timedelta(days=1),
            created_by=self.sys,
        )
        self._request("Old public form", -20, created_by=None)
        self._request("Has status", 3, status=self.statuses[("torch-thc", "approved")])

        out = self._run()
        assert "DRY-RUN" in out
        assert "[%d] torch-thc: 2 live (1 today/future, 1 past, 0 undated)" % self.torch.id in out
        assert "'RARE Steak'" in out and "· plan ·" in out
        assert "'Old public form'" in out and "public spark-form" in out
        assert "'Has status'" not in out
        assert "request default=NONE" in out
        assert "torch-thc: request status default → pending" in out
        assert "liquid-death: request status default" not in out
        assert em.Request.objects.filter(status__isnull=True).count() == 2
        assert not em.RequestStatus.objects.filter(tenant=self.torch, is_default=True).exists()

    def test_apply_restores_defaults_and_moves_only_future_to_pending(self):
        today = self._request("Today", 0)
        future = self._request("Next week", 7)
        past = self._request("Last month", -30)
        ld_future = self._request("LD future", 5, tenant=self.ld)

        out = self._run("--apply")
        assert "APPLY" in out
        pending = self.statuses[("torch-thc", "pending")]
        for r in (today, future):
            r.refresh_from_db()
            assert r.status_id == pending.id
        past.refresh_from_db()
        assert past.status_id is None
        ld_future.refresh_from_db()
        assert ld_future.status_id == self.statuses[("liquid-death", "pending")].id

        pending.refresh_from_db()
        assert pending.is_default is True
        assert em.EventStatus.objects.get(tenant=self.torch, is_default=True).slug == "pending"
        assert em.RequestStatus.objects.get(tenant=self.ld, is_default=True).slug == "pending"
        log = em.RequestActivityLog.objects.get(request=future)
        assert log.kind == em.RequestActivityLog.KIND_STATUS_CHANGED
        assert log.metadata["to"] == "pending"

        again = self._run("--apply")
        assert "Requests moved to Pending: 0" in again
        assert "Default statuses restored: 0" in again

    def test_tenant_scope(self):
        self._request("Torch one", 2)
        ld = self._request("LD one", 2, tenant=self.ld)
        self._run("--tenant", "torch-thc", "--apply")
        ld.refresh_from_db()
        assert ld.status_id is None
