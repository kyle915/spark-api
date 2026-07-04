"""Tests for `audit_recap_data_health` — flags custom recaps whose parsed
KPIs are implausible, running the REAL matcher over real CustomFieldValues.
"""

import io

import pytest
from django.core.management import call_command

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


@pytest.mark.django_db(transaction=True)
class TestRecapDataHealth(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Health Tenant")
        self.template = self._template("Health Template")

    # -- helpers (mirror test_tenant_market_performance) -------------------

    def _template(self, name):
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        return recap_models.CustomRecapTemplate.objects.create(
            name=name, event_type=event_type, tenant=self.tenant,
            created_by=self.system_user,
        )

    def _recap(self, name):
        event = self.create_event(name=name, tenant=self.tenant)
        return recap_models.CustomRecap.objects.create(
            name=name, event=event, tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user, updated_by=self.system_user,
        )

    def _field(self, custom_recap, field_name, value):
        ft = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="KPIs", tenant=self.tenant, created_by=self.system_user
        )
        field = recap_models.CustomField.objects.create(
            name=field_name,
            custom_recap_template=custom_recap.custom_recap_template,
            custom_field_type=ft,
            recap_section=section,
            created_by=self.system_user,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=custom_recap, custom_field=field, value=value,
            created_by=self.system_user,
        )

    def _run(self, *args):
        out = io.StringIO()
        call_command("audit_recap_data_health", "--all-time", *args, stdout=out)
        return out.getvalue()

    def test_absurd_consumer_count_is_flagged(self):
        cr = self._recap("SHB-ish")
        self._field(cr, "Consumers Sampled", "1960")  # the classic mash
        log = self._run("--max-consumers", "1000")
        assert "flagged    : 1" in log
        assert f"#{cr.id}" in log
        assert "consumers 1960 > 1000" in log

    def test_clean_recap_is_not_flagged(self):
        cr = self._recap("Clean")
        self._field(cr, "Consumers Sampled", "42")
        self._field(cr, "Cans Sold", "8")
        log = self._run("--max-consumers", "1000", "--max-units", "5000")
        assert "flagged    : 0" in log

    def test_absurd_cans_is_flagged(self):
        cr = self._recap("Cans blowout")
        self._field(cr, "Cans Sold", "9000")
        log = self._run("--max-units", "5000")
        assert "flagged    : 1" in log
        assert "cans 9000 > 5000" in log
