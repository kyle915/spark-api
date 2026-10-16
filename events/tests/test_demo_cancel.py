"""Cancel-demo emails Ignite and leaves the request status alone."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from events import models as em
from events.demo_cancel import (
    DemoCancelError,
    request_demo_cancellation,
    request_display_code,
)
from events.mutations import _get_request_cc_emails
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestDemoCancel(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Torch THC")
        self.other = self.create_tenant(name="Other Brand")
        self.sys = self.get_system_user()
        self.rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.other_rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.other, created_by=self.sys
        )
        self.status = em.RequestStatus.objects.create(
            name="Approved",
            slug="approved",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.declined_status = em.RequestStatus.objects.create(
            name="Declined",
            slug="declined",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.event_status = em.EventStatus.objects.create(
            name="Approved",
            slug="approved",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.suspended_event_status = em.EventStatus.objects.create(
            name="Suspended",
            slug="suspended",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.ignite = self.create_user(
            username="ops-cancel",
            email="ops-cancel@igniteproductions.co",
            role=self.roles["spark_admin"],
        )
        self.buyer = self.create_user(
            username="buyer-cancel",
            email="buyer-cancel@torchdrinks.com",
            role=self.roles["client"],
        )

    def _req(self, *, pk, tenant, rt, name="Vons #12", **kwargs):
        return em.Request.objects.create(
            id=pk,
            name=name,
            address="1 Main St",
            retailer_name=name,
            request_type=rt,
            tenant=tenant,
            created_by=self.sys,
            status=self.status if tenant == self.tenant else None,
            date=timezone.now(),
            **kwargs,
        )

    def test_lookup_paths(self):
        req = self._req(pk=424242, tenant=self.tenant, rt=self.rt)
        code = request_display_code(req.id)
        with patch("events.demo_cancel.DemoCancelRequestedMailer.send"):
            by_code = request_demo_cancellation(
                request_id=None,
                lookup=code,
                reason="Account cancelled",
                tenant_id=self.tenant.id,
                actor=self.buyer,
            )
            by_uuid = request_demo_cancellation(
                request_id=None,
                lookup=str(req.uuid),
                reason="Account cancelled",
                tenant_id=self.tenant.id,
                actor=self.buyer,
            )
        assert by_code.request_code == code
        assert by_uuid.request_code == code

    def test_ambiguous_suffix(self):
        self._req(pk=1000925, tenant=self.tenant, rt=self.rt, name="Vons")
        self._req(pk=2000925, tenant=self.tenant, rt=self.rt, name="Ralphs")
        with pytest.raises(DemoCancelError, match="More than one request"):
            request_demo_cancellation(
                request_id=None,
                lookup="REQ-0925",
                reason="Account cancelled",
                tenant_id=self.tenant.id,
                actor=self.buyer,
            )

    def test_other_tenant_and_deleted_are_hidden(self):
        other = self._req(pk=515151, tenant=self.other, rt=self.other_rt, name="Elsewhere")
        deleted = self._req(
            pk=616161,
            tenant=self.tenant,
            rt=self.rt,
            name="Gone",
            deleted_at=timezone.now(),
        )
        with pytest.raises(DemoCancelError, match="isn't on your account"):
            request_demo_cancellation(
                request_id=str(other.id),
                lookup=None,
                reason="Account cancelled",
                tenant_id=self.tenant.id,
                actor=self.buyer,
            )
        with pytest.raises(DemoCancelError, match="deleted"):
            request_demo_cancellation(
                request_id=str(deleted.id),
                lookup=None,
                reason="Account cancelled",
                tenant_id=self.tenant.id,
                actor=self.buyer,
            )

    def test_short_reason_does_not_send(self):
        self._req(pk=424242, tenant=self.tenant, rt=self.rt)
        with patch("events.demo_cancel.DemoCancelRequestedMailer.send") as send:
            with pytest.raises(DemoCancelError, match="reason"):
                request_demo_cancellation(
                    request_id="424242",
                    lookup=None,
                    reason="no",
                    tenant_id=self.tenant.id,
                    actor=self.buyer,
                )
        send.assert_not_called()

    def test_emails_ignite_and_brand_and_cancels_status_and_events(self):
        self.tenant.auto_approve_requests = True
        self.tenant.request_approved_cc_emails = "brand-cc@torchdrinks.com"
        self.tenant.save(update_fields=["auto_approve_requests", "request_approved_cc_emails"])

        rmm = self.create_user(
            username="rmm-cancel",
            email="rmm-cancel@torchdrinks.com",
            role=self.roles["client"],
        )
        req = self._req(
            pk=424242,
            tenant=self.tenant,
            rt=self.rt,
            name="Vons #12",
            requestor_email="requestor@torchdrinks.com",
            client_email="client@torchdrinks.com",
            rmm_asigned=rmm,
        )
        ev = self.create_event(
            name="Vons #12",
            tenant=self.tenant,
            request=req,
            status=self.event_status,
        )
        title = self.create_job_title("Brand Ambassador", self.tenant)
        job = self.create_job(
            "Vons #12 BA", "Sampling", "1 Main St", ev, title, self.tenant,
            closed=False, ongoing=True,
        )
        captured = {}

        def capture(mailer, *args, **kwargs):
            envelope = mailer.envelope()
            captured["to"] = list(envelope.to_emails)
            captured["subject"] = envelope.subject
            captured["html"] = envelope.render_template()

        with patch("events.demo_cancel.DemoCancelRequestedMailer.send", capture), patch(
            "events.mutations._notify_assigned_ambassadors_for_suspended_event"
        ) as notify_bas:
            result = request_demo_cancellation(
                request_id="424242",
                lookup=None,
                reason="The account cancelled this tasting.",
                tenant_id=self.tenant.id,
                actor=self.buyer,
            )

        req.refresh_from_db()
        ev.refresh_from_db()
        job.refresh_from_db()
        assert req.status_id == self.declined_status.id
        assert req.decline_reason == "The account cancelled this tasting."
        assert req.deleted_at is None
        assert ev.status_id == self.suspended_event_status.id
        assert job.closed is True
        assert job.ongoing is False
        notify_bas.assert_called_once_with(ev.id)

        assert result.request_code == request_display_code(req.id)
        assert result.request_code in captured["subject"]
        assert "Vons #12" in captured["subject"]
        assert "The account cancelled this tasting." in captured["html"]
        assert "buyer-cancel@torchdrinks.com" in captured["html"]

        to_lower = {e.lower() for e in captured["to"]}
        assert "ops-cancel@igniteproductions.co" in to_lower
        assert "requestor@torchdrinks.com" in to_lower
        assert "client@torchdrinks.com" in to_lower
        assert "rmm-cancel@torchdrinks.com" in to_lower
        assert "brand-cc@torchdrinks.com" in to_lower

        note = em.RequestActivityLog.objects.get(request=req)
        assert note.kind == em.RequestActivityLog.KIND_NOTE_ADDED
        assert "Demo cancelled" in note.summary
