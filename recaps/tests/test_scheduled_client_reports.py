"""Coverage for the scheduled monthly client-report backend:

* :func:`recaps.client_report.build_client_monthly_report_pdf` — assembles the
  tenant's monthly performance PDF (header / KPI tiles / chart / trend /
  insights) and renders it through WeasyPrint once, and
* the ``send_scheduled_client_reports`` management command — the monthly cron
  that, per OPTED-IN tenant with recipients, generates that PDF and emails it.

The guarantees these tests lock in:

* **Opt-in OFF safe default.** A tenant is only ever touched when it has BOTH
  ``scheduled_report_enabled=True`` AND non-empty recipients. With nothing
  enabled the command sends NOTHING; an enabled tenant with no recipients is
  skipped (never emailed).
* **Recipients + PDF attachment.** When a tenant IS enabled+recipiented, the
  mailer is invoked with exactly that tenant's resolved recipients and a PDF
  attachment (content_type ``application/pdf``).
* **--dry-run sends nothing** (the mailer is never constructed/sent), but still
  resolves recipients + generates the PDF.
* **Prior COMPLETE month** is the default reporting period — never the
  in-progress current month.

WeasyPrint's native deps aren't installed in CI, so the PDF render is mocked at
``recaps.client_report`` import (``build_client_monthly_report_pdf`` imports
``weasyprint`` lazily inside the render call, exactly so this is patchable); the
test asserts the HTML the renderer is HANDED is well-formed and carries the
tenant's numbers. The command tests mock the mailer so no real email is sent.

Wall-clock independence: the prior-complete-month test pins ``timezone.now()``
inside the command module to a fixed anchor. Fixtures back-date ``created_at``
(it's ``auto_now_add``) into a target month, the same trick
test_tenant_kpi_comparison.py uses.
"""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import client_report
from recaps import models as recap_models
from tenants.management.commands import send_scheduled_client_reports as cmd_mod


def _at(year: int, month: int, day: int = 10):
    """A tz-aware datetime inside ``year``-``month`` (mid-month by default)."""
    return timezone.make_aware(datetime.datetime(year, month, day, 12, 0, 0))


# Fixed "now": mid-June 2026 -> the prior COMPLETE month is May 2026.
_FAKE_NOW = timezone.make_aware(datetime.datetime(2026, 6, 15, 10, 30, 0))


@pytest.mark.django_db(transaction=True)
class TestScheduledClientReports(AmbassadorsGraphQLTestCase):
    """PDF assembly + the opt-in/dry-run/recipient behavior of the cron."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()

        # Enabled + has recipients -> the one tenant that should get emailed.
        self.enabled_tenant = self.create_tenant(
            name="Girl Beer",
            scheduled_report_enabled=True,
            recap_recipient_emails="client@girlbeer.com, ops@girlbeer.com",
        )
        # Enabled but NO recipients -> opted in, but must be skipped.
        self.enabled_no_recipients = self.create_tenant(
            name="Quiet Brand",
            scheduled_report_enabled=True,
            recap_recipient_emails="",
        )
        # Has recipients but NOT enabled -> opt-in OFF, must never be emailed.
        self.disabled_tenant = self.create_tenant(
            name="Liquid Death",
            scheduled_report_enabled=False,
            recap_recipient_emails="brand@liquiddeath.com",
        )

        # Seed May-2026 (the prior complete month) activity for the enabled
        # tenant so the PDF + command have real numbers to report.
        self._recap_in(
            self.enabled_tenant,
            _at(2026, 5),
            engagements=120,
            consumers=300,
            samples=80,
            products_sold=12,
        )

    # -- fixture helpers ------------------------------------------------

    def _product(self, tenant, name: str):
        product_type = event_models.ProductType.objects.create(
            name=f"type {name}", tenant=tenant, created_by=self.system_user
        )
        return event_models.Product.objects.create(
            name=name,
            product_type=product_type,
            tenant=tenant,
            created_by=self.system_user,
        )

    def _recap_in(
        self,
        tenant,
        when,
        *,
        engagements: int = 0,
        consumers: int = 0,
        samples: int = 0,
        products_sold: int = 0,
    ) -> recap_models.Recap:
        """Create a legacy recap (+ event + children) for ``tenant`` dated to ``when``.

        Everything is back-dated with ``.update(created_at=...)`` because the
        column is ``auto_now_add`` and each KPI source filters on its OWN
        ``created_at`` — so all of them must land in the target window.
        """
        label = when.strftime("%Y-%m-%d-%f")
        event = self.create_event(name=f"ev {label}", tenant=tenant)
        event_models.Event.objects.filter(id=event.id).update(created_at=when)

        recap = recap_models.Recap.objects.create(
            name=f"recap {label}",
            event=event,
            total_engagements=engagements,
            products_sold=products_sold,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.Recap.objects.filter(id=recap.id).update(created_at=when)

        if consumers:
            eng = recap_models.ConsumerEngagements.objects.create(
                recap=recap,
                total_consumer=consumers,
                first_time_consumers=consumers // 3,
                brand_aware_consumers=consumers // 2,
                willing_to_purchase_consumers=consumers // 4,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            recap_models.ConsumerEngagements.objects.filter(id=eng.id).update(
                created_at=when
            )

        if samples:
            ps = recap_models.ProductSamples.objects.create(
                recap=recap,
                product=self._product(tenant, f"prod {label}"),
                quantity=samples,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            recap_models.ProductSamples.objects.filter(id=ps.id).update(
                created_at=when
            )
        return recap

    # -- PDF generator --------------------------------------------------

    def test_pdf_generates_non_empty_bytes_for_tenant_with_data(self):
        """The PDF builder returns non-empty bytes and hands the renderer
        well-formed HTML carrying the tenant's name + the period numbers."""
        captured = {}

        class _FakeHTML:
            def __init__(self, *, string):
                captured["html"] = string

            def write_pdf(self, *args, **kwargs):
                captured["css"] = kwargs.get("stylesheets")
                return b"%PDF-1.7 fake-bytes"

        # weasyprint is imported lazily INSIDE the render call, so patching the
        # module's HTML/CSS here is what the builder will pick up. include_
        # sentiment=False so no AI path is touched.
        with mock.patch.dict(
            "sys.modules",
            {"weasyprint": mock.MagicMock(HTML=_FakeHTML, CSS=lambda **k: object())},
        ):
            pdf = client_report.build_client_monthly_report_pdf(
                self.enabled_tenant.id, 2026, 5, include_sentiment=False
            )

        assert isinstance(pdf, bytes)
        assert len(pdf) > 0
        html = captured["html"]
        # Header label + brand name present.
        assert "May 2026 Performance" in html
        assert "Girl Beer" in html
        # The May numbers are rendered: 300 consumers (samples distributed now
        # mirrors consumers per kyle's rule, also 300), 120 engagements, and 75
        # willing-to-purchase.
        assert "300" in html
        assert "120" in html
        assert "75" in html

    def test_pdf_missing_tenant_raises_clean_error(self):
        with pytest.raises(client_report.ClientMonthlyReportError):
            client_report.build_client_monthly_report_pdf(
                99999, 2026, 5, include_sentiment=False
            )

    # -- command: selection / opt-in ------------------------------------

    def test_command_emails_only_enabled_and_recipiented_tenant(self):
        """Only the enabled+recipiented tenant is mailed; the mailer gets that
        tenant's resolved recipients and a PDF attachment."""
        sent_instances = []

        real_init = cmd_mod.ClientMonthlyReportMailer.__init__

        def _spy_init(self, **kwargs):
            real_init(self, **kwargs)
            sent_instances.append(self)

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod, "build_client_monthly_report_pdf",
            return_value=b"%PDF-1.7 fake",
        ), mock.patch.object(
            cmd_mod.ClientMonthlyReportMailer, "__init__", _spy_init
        ), mock.patch.object(
            cmd_mod.ClientMonthlyReportMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_scheduled_client_reports")

        # Exactly one tenant emailed: Girl Beer.
        assert len(sent_instances) == 1
        assert mock_send.call_count == 1
        mailer = sent_instances[0]
        assert mailer.tenant_name == "Girl Beer"
        # Recipients resolved + deduped from recap_recipient_emails.
        assert mailer.recipients == ["client@girlbeer.com", "ops@girlbeer.com"]
        # Period defaulted to the prior complete month (May 2026).
        assert mailer.period_label == "May 2026"

        # The envelope carries a single application/pdf attachment.
        envelope = mailer.envelope()
        assert len(envelope.attachments) == 1
        assert envelope.attachments[0]["content_type"] == "application/pdf"
        assert envelope.attachments[0]["filename"].endswith(".pdf")
        # Subject names the brand + period.
        assert "Girl Beer" in envelope.subject
        assert "May 2026" in envelope.subject

    def test_command_with_no_enabled_tenants_sends_nothing(self):
        """The safe default: with no tenant opted in, the command emails nobody."""
        # Flip the one enabled tenant off so NOTHING is enabled.
        type(self.enabled_tenant).objects.filter(
            id=self.enabled_tenant.id
        ).update(scheduled_report_enabled=False)
        type(self.enabled_no_recipients).objects.filter(
            id=self.enabled_no_recipients.id
        ).update(scheduled_report_enabled=False)

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod, "build_client_monthly_report_pdf",
            return_value=b"%PDF",
        ) as mock_build, mock.patch.object(
            cmd_mod.ClientMonthlyReportMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_scheduled_client_reports")

        assert mock_send.call_count == 0
        # No enabled tenant -> we never even build a PDF.
        assert mock_build.call_count == 0

    def test_enabled_tenant_without_recipients_is_skipped(self):
        """An opted-in tenant with no recipients is skipped, never emailed."""
        # Disable the fully-configured tenant so only the no-recipients
        # enabled tenant remains in scope.
        type(self.enabled_tenant).objects.filter(
            id=self.enabled_tenant.id
        ).update(scheduled_report_enabled=False)

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod, "build_client_monthly_report_pdf",
            return_value=b"%PDF",
        ) as mock_build, mock.patch.object(
            cmd_mod.ClientMonthlyReportMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_scheduled_client_reports")

        # Skipped before building a PDF or sending.
        assert mock_send.call_count == 0
        assert mock_build.call_count == 0

    # -- command: dry-run -----------------------------------------------

    def test_dry_run_generates_pdf_but_sends_nothing(self):
        """--dry-run resolves recipients + builds the PDF, but sends no email."""
        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod, "build_client_monthly_report_pdf",
            return_value=b"%PDF-1.7 fake",
        ) as mock_build, mock.patch.object(
            cmd_mod.ClientMonthlyReportMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_scheduled_client_reports", "--dry-run")

        # PDF was generated for the enabled+recipiented tenant...
        assert mock_build.call_count == 1
        # ...but NOTHING was sent.
        assert mock_send.call_count == 0

    # -- command: period selection --------------------------------------

    def test_defaults_to_prior_complete_month(self):
        """Default period is the most recent COMPLETE month (May for a June now)."""
        with mock.patch.object(cmd_mod.timezone, "now", return_value=_FAKE_NOW):
            year, month = cmd_mod._prior_complete_month()
        assert (year, month) == (2026, 5)

        # And the command passes that period through to the PDF builder.
        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod, "build_client_monthly_report_pdf",
            return_value=b"%PDF",
        ) as mock_build, mock.patch.object(
            cmd_mod.ClientMonthlyReportMailer, "send", autospec=True
        ):
            call_command("send_scheduled_client_reports")

        # Called for the enabled tenant with (tenant_id, 2026, 5).
        assert mock_build.call_count == 1
        args = mock_build.call_args.args
        assert args[1] == 2026
        assert args[2] == 5

    def test_month_override_is_respected(self):
        """--month YYYY-MM overrides the default period."""
        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod, "build_client_monthly_report_pdf",
            return_value=b"%PDF",
        ) as mock_build, mock.patch.object(
            cmd_mod.ClientMonthlyReportMailer, "send", autospec=True
        ):
            call_command("send_scheduled_client_reports", "--month", "2026-03")

        assert mock_build.call_count == 1
        args = mock_build.call_args.args
        assert args[1] == 2026
        assert args[2] == 3

    def test_one_tenant_failure_does_not_abort_run(self):
        """A per-tenant exception is logged + skipped; other tenants still send."""
        # Add a SECOND enabled+recipiented tenant; make the FIRST one's PDF
        # build blow up. The second must still be emailed.
        other = self.create_tenant(
            name="Borjomi",
            scheduled_report_enabled=True,
            recap_recipient_emails="client@borjomi.com",
        )

        def _build(tenant_id, year, month, **kwargs):
            if tenant_id == self.enabled_tenant.id:
                raise client_report.ClientMonthlyReportError("boom")
            return b"%PDF-ok"

        with mock.patch.object(
            cmd_mod.timezone, "now", return_value=_FAKE_NOW
        ), mock.patch.object(
            cmd_mod, "build_client_monthly_report_pdf", side_effect=_build
        ), mock.patch.object(
            cmd_mod.ClientMonthlyReportMailer, "send", autospec=True
        ) as mock_send:
            call_command("send_scheduled_client_reports")

        # Only the healthy tenant (Borjomi) was emailed; the failure was
        # swallowed, not raised.
        assert mock_send.call_count == 1
        assert other.scheduled_report_recipients() == ["client@borjomi.com"]
