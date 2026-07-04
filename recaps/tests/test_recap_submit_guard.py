"""Tests for the recap submit-time data guard (recaps.mutations).

Covers the pure plausibility rules (implausibility_reasons), the stamping
helper that writes CustomRecap.data_quality_flags via the REAL matcher over
real CustomFieldValues, and the alert email. The mailer + recipient resolver
are patched — no real email smoked. (The guard's thin async wrapper just
threads these two sync helpers through sync_to_async, so we test the sync
pieces directly and avoid async-DB-in-thread teardown flakiness.)
"""

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.report_service import CampaignReportKpis, implausibility_reasons


class TestImplausibilityReasons:
    """Pure-function threshold rules — no DB."""

    def test_clean_kpis_have_no_reasons(self):
        k = CampaignReportKpis(consumers_reached=200, willing_to_purchase=50,
                               cans_sold=100, packs_sold=20)
        assert implausibility_reasons(k) == []

    def test_conversion_over_100_flagged(self):
        k = CampaignReportKpis(consumers_reached=100, willing_to_purchase=150)
        reasons = implausibility_reasons(k)
        assert any("conversion >100%" in r for r in reasons)

    def test_absurd_consumers_flagged(self):
        k = CampaignReportKpis(consumers_reached=1960)
        assert any("consumers 1960 > 1000" in r for r in implausibility_reasons(k))

    def test_absurd_units_flagged(self):
        k = CampaignReportKpis(cans_sold=9000, packs_sold=8000)
        reasons = implausibility_reasons(k)
        assert any("cans 9000" in r for r in reasons)
        assert any("packs 8000" in r for r in reasons)


@pytest.mark.django_db(transaction=True)
class TestRecapSubmitGuard(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Guard Tenant")
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Guard Template", event_type=event_type, tenant=self.tenant,
            created_by=self.system_user,
        )

    def _recap(self, name):
        event = self.create_event(name=name, tenant=self.tenant)
        return recap_models.CustomRecap.objects.create(
            name=name, event=event, tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user, updated_by=self.system_user,
        )

    def _field(self, cr, field_name, value):
        ft = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="KPIs", tenant=self.tenant, created_by=self.system_user
        )
        field = recap_models.CustomField.objects.create(
            name=field_name, custom_recap_template=self.template,
            custom_field_type=ft, recap_section=section,
            created_by=self.system_user,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=cr, custom_field=field, value=value,
            created_by=self.system_user,
        )

    def test_implausible_recap_is_stamped(self):
        from recaps.mutations import _compute_recap_data_quality_flags

        cr = self._recap("Suspect")
        self._field(cr, "Consumers Sampled", "1960")
        reasons = _compute_recap_data_quality_flags(cr)
        assert reasons
        cr.refresh_from_db()
        assert "consumers 1960 > 1000" in cr.data_quality_flags

    def test_clean_recap_not_stamped(self):
        from recaps.mutations import _compute_recap_data_quality_flags

        cr = self._recap("Clean")
        self._field(cr, "Consumers Sampled", "42")
        self._field(cr, "Cans Sold", "8")
        reasons = _compute_recap_data_quality_flags(cr)
        assert reasons == []
        cr.refresh_from_db()
        assert cr.data_quality_flags == ""

    def test_alert_email_dispatches(self, monkeypatch):
        import tenants.support
        from recaps import mutations
        from utils.mailer import Mailer

        calls: list[int] = []
        monkeypatch.setattr(
            tenants.support, "_resolve_ignite_recipients",
            lambda: ["ops@igniteproductions.co"],
        )
        monkeypatch.setattr(Mailer, "send_now", lambda self: calls.append(1))

        cr = self._recap("Suspect2")
        mutations._send_recap_data_quality_alert(cr, ["consumers 5000 > 1000"])
        assert len(calls) == 1

    def test_alert_silent_without_recipients(self, monkeypatch):
        import tenants.support
        from recaps import mutations
        from utils.mailer import Mailer

        calls: list[int] = []
        monkeypatch.setattr(
            tenants.support, "_resolve_ignite_recipients", lambda: [],
        )
        monkeypatch.setattr(Mailer, "send_now", lambda self: calls.append(1))

        cr = self._recap("Suspect3")
        mutations._send_recap_data_quality_alert(cr, ["consumers 5000 > 1000"])
        assert len(calls) == 0
