"""Coverage for the client weekly digest:

* :func:`recaps.weekly_digest.build_weekly_digest` — assembles the three
  sections (this week at a glance / coming up / needs your approval) over the
  trailing + look-ahead 7-day windows, and
* the ``send_client_weekly_digest`` management command — the weekly cron that,
  per OPTED-IN tenant with recipients, builds that digest and emails it.

Guarantees locked in:

* **Section math.** Completed activations (events whose ``start_time`` lands in
  the trailing week), recaps filed, the headline KPIs, the next-7-days list,
  and the pending-approval list are each counted from the right rows — and a
  ``reviewed=True`` request is EXCLUDED from "needs your approval".
* **Opt-in OFF safe default.** A tenant is only touched with BOTH
  ``scheduled_report_enabled=True`` AND non-empty recipients. Nothing enabled →
  nothing sent; enabled-but-no-recipients → skipped.
* **Quiet weeks are skipped** unless ``--force``.
* **--dry-run sends nothing.**

Wall-clock independence: ``timezone.now()`` is pinned inside the command module
to a fixed anchor, and fixtures back-date ``created_at`` (``auto_now_add``) /
set ``start_time`` into the target windows — the same trick
test_scheduled_client_reports.py uses. The mailer's ``send`` is mocked so no
real email is sent.
"""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.weekly_digest import build_weekly_digest
from tenants.management.commands import send_client_weekly_digest as cmd_mod

# Fixed "now": mid-June 2026. Trailing window = Jun 8–15, look-ahead = Jun 15–22.
_FAKE_NOW = timezone.make_aware(datetime.datetime(2026, 6, 15, 10, 30, 0))


def _ago(days: int) -> datetime.datetime:
    return _FAKE_NOW - datetime.timedelta(days=days)


def _ahead(days: int) -> datetime.datetime:
    return _FAKE_NOW + datetime.timedelta(days=days)


@pytest.mark.django_db(transaction=True)
class TestClientWeeklyDigest(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()

        # Enabled + recipients + a busy week -> the one tenant that gets emailed.
        self.enabled_tenant = self.create_tenant(
            name="Girl Beer",
            scheduled_report_enabled=True,
            recap_recipient_emails="client@girlbeer.com, ops@girlbeer.com",
        )
        # Enabled but NO recipients -> opted in, must be skipped.
        self.enabled_no_recipients = self.create_tenant(
            name="Quiet Brand",
            scheduled_report_enabled=True,
            recap_recipient_emails="",
        )
        # Has recipients but NOT enabled -> opt-in OFF, never emailed.
        self.disabled_tenant = self.create_tenant(
            name="Liquid Death",
            scheduled_report_enabled=False,
            recap_recipient_emails="brand@liquiddeath.com",
        )
        # Enabled + recipients but a totally QUIET week -> skipped unless --force.
        self.quiet_enabled = self.create_tenant(
            name="Sleepy Brand",
            scheduled_report_enabled=True,
            recap_recipient_emails="zzz@sleepy.com",
        )

        self.req_type = self.create_request_type("Demo", self.enabled_tenant)
        self.req_status = self.create_request_status("Pending", self.enabled_tenant)

        # Seed the enabled tenant's week:
        # 1) an activation that RAN 2 days ago + a recap filed 2 days ago
        self._completed_activation(
            self.enabled_tenant, _ago(2), engagements=50, samples=30, products_sold=5
        )
        # 2) an activation COMING UP in 3 days
        self._upcoming_activation(self.enabled_tenant, _ahead(3))
        # 3) a request still PENDING (reviewed=False), submitted yesterday
        self._request(self.enabled_tenant, _ago(1), reviewed=False)
        # 4) a REVIEWED request — must NOT count toward "needs your approval"
        self._request(self.enabled_tenant, _ago(1), reviewed=True)

    # -- fixture helpers ------------------------------------------------

    def _completed_activation(
        self, tenant, when, *, engagements=0, samples=0, products_sold=0
    ):
        tag = when.strftime("%H%M%S%f")
        event = self.create_event(name=f"ran {tag}", tenant=tenant)
        # start_time drives "completed activations"; created_at drives the KPI
        # / recap-count windows. Put BOTH in the trailing week.
        event_models.Event.objects.filter(id=event.id).update(
            start_time=when, created_at=when
        )
        recap = recap_models.Recap.objects.create(
            name=f"recap {tag}",
            event=event,
            total_engagements=engagements,
            products_sold=products_sold,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.Recap.objects.filter(id=recap.id).update(created_at=when)

        if samples:
            ptype = event_models.ProductType.objects.create(
                name=f"pt {tag}", tenant=tenant, created_by=self.system_user
            )
            product = event_models.Product.objects.create(
                name=f"p {tag}",
                product_type=ptype,
                tenant=tenant,
                created_by=self.system_user,
            )
            ps = recap_models.ProductSamples.objects.create(
                recap=recap,
                product=product,
                quantity=samples,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            recap_models.ProductSamples.objects.filter(id=ps.id).update(
                created_at=when
            )
        return event

    def _upcoming_activation(self, tenant, when):
        tag = when.strftime("%H%M%S%f")
        event = self.create_event(name=f"soon {tag}", tenant=tenant)
        # start_time in the look-ahead window; created_at = now (so it does NOT
        # also land in the trailing KPI window).
        event_models.Event.objects.filter(id=event.id).update(
            start_time=when, created_at=_FAKE_NOW
        )
        return event

    def _request(self, tenant, when, *, reviewed: bool):
        return event_models.Request.objects.create(
            name=f"req {when:%H%M%S%f}-{reviewed}",
            address="123 Test St",
            date=when,
            start_time=when,
            tenant=tenant,
            request_type=self.req_type,
            status=self.req_status,
            reviewed=reviewed,
            created_by=self.system_user,
        )

    # -- data builder ---------------------------------------------------

    def test_build_weekly_digest_sections(self):
        d = build_weekly_digest(self.enabled_tenant.id, _FAKE_NOW)

        # Section 1 — this week at a glance.
        assert d.completed_activations == 1
        assert d.recaps_filed == 1
        assert d.kpis.total_engagements == 50
        assert d.kpis.samples_distributed == 30
        assert d.kpis.products_sold == 5

        # Section 2 — coming up (next 7 days).
        assert d.upcoming_total == 1
        assert len(d.upcoming) == 1

        # Section 3 — needs your approval. The reviewed=True request is excluded,
        # so exactly one pending row.
        assert d.pending_total == 1
        assert len(d.pending) == 1

        assert d.has_content is True

    def test_quiet_week_has_no_content(self):
        d = build_weekly_digest(self.quiet_enabled.id, _FAKE_NOW)
        assert d.completed_activations == 0
        assert d.recaps_filed == 0
        assert d.upcoming_total == 0
        assert d.pending_total == 0
        assert d.has_content is False

    # -- command: selection / opt-in ------------------------------------

    def test_command_emails_only_enabled_and_recipiented_tenant(self):
        """Only Girl Beer (enabled + recipients + non-quiet) is emailed."""
        sent_instances = []
        real_init = cmd_mod.ClientWeeklyDigestMailer.__init__

        def _spy_init(self, **kwargs):
            real_init(self, **kwargs)
            sent_instances.append(self)

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod.ClientWeeklyDigestMailer, "__init__", _spy_init
        ), mock.patch.object(
            cmd_mod.ClientWeeklyDigestMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_client_weekly_digest")

        assert len(sent_instances) == 1
        assert mock_send.call_count == 1
        mailer = sent_instances[0]
        assert mailer.tenant_name == "Girl Beer"
        assert mailer.recipients == ["client@girlbeer.com", "ops@girlbeer.com"]

        env = mailer.envelope()
        assert env.template == "events.templates.emails.client_weekly_digest"
        assert "Girl Beer" in env.subject
        # One pending approval -> subject leads with it.
        assert "awaiting approval" in env.subject
        # Template-based email (no attachments, unlike the monthly PDF report).
        assert not env.attachments

    def test_dry_run_sends_nothing(self):
        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod.ClientWeeklyDigestMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_client_weekly_digest", "--dry-run")
        assert mock_send.call_count == 0

    def test_quiet_week_skipped_then_forced(self):
        """A quiet enabled tenant is skipped by default, sent under --force."""
        # Narrow scope to ONLY the quiet enabled tenant.
        for t in (self.enabled_tenant, self.enabled_no_recipients):
            type(t).objects.filter(id=t.id).update(scheduled_report_enabled=False)

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod.ClientWeeklyDigestMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_client_weekly_digest")
        assert mock_send.call_count == 0  # quiet -> skipped

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod.ClientWeeklyDigestMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_client_weekly_digest", "--force")
        assert mock_send.call_count == 1  # forced -> sent

    def test_no_enabled_tenants_sends_nothing(self):
        for t in (
            self.enabled_tenant,
            self.enabled_no_recipients,
            self.quiet_enabled,
        ):
            type(t).objects.filter(id=t.id).update(scheduled_report_enabled=False)

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod.ClientWeeklyDigestMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_client_weekly_digest")
        assert mock_send.call_count == 0

    def test_enabled_tenant_without_recipients_is_skipped(self):
        """An opted-in tenant with no recipients is skipped, never emailed."""
        # Only the no-recipients enabled tenant remains in scope.
        for t in (self.enabled_tenant, self.quiet_enabled):
            type(t).objects.filter(id=t.id).update(scheduled_report_enabled=False)

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod.ClientWeeklyDigestMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_client_weekly_digest")
        assert mock_send.call_count == 0
