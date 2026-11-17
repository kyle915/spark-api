"""Read-only impact audit for the recap number fixes (audit_number_fixes)."""

import io
import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.number_fixes_audit import old_first_digits, old_strip_parse

SECRET = "number-fixes-secret"


def test_old_readers_are_the_old_rules():
    assert old_strip_parse("1,234") == 1234
    assert old_first_digits("1,234") == 1
    assert old_strip_parse("12.5") == 125
    assert old_strip_parse("sold 50, then 20") == 5020


@pytest.mark.django_db(transaction=True)
class TestNumberFixesAudit(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch Test")
        request = event_models.Request.objects.create(
            name="Guerilla", address="1 Main St", tenant=self.tenant,
            status=self.create_request_status(name="Approved", tenant=self.tenant, slug="approved"),
            request_type=self.create_request_type(name="Retail", tenant=self.tenant),
            created_by=self.system_user,
        )
        when = timezone.now() - timedelta(days=3)
        self.event = self.create_event(
            name="Sidewalk", tenant=self.tenant, date=when, start_time=when,
            end_time=when + timedelta(hours=3),
        )
        event_models.Event.objects.filter(id=self.event.id).update(request=request)
        event_type = self.create_event_type(name="Guerilla", tenant=self.tenant)
        template = recap_models.CustomRecapTemplate.objects.create(
            name="Guerilla", event_type=event_type, tenant=self.tenant,
            created_by=self.system_user,
        )
        ftype = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.system_user
        )
        fields = {
            name: recap_models.CustomField.objects.create(
                name=name, custom_recap_template=template, custom_field_type=ftype,
                recap_section=section, created_by=self.system_user,
            )
            for name in (
                "TOTAL - how many cans did you give away?",
                "Total number of consumers sampled",
            )
        }
        for approved, values in (
            (True, {"TOTAL - how many cans did you give away?": "40",
                    "Total number of consumers sampled": "1,234"}),
            (False, {"TOTAL - how many cans did you give away?": "25",
                     "Total number of consumers sampled": "60"}),
        ):
            recap = recap_models.CustomRecap.objects.create(
                name="r", approved=approved, event=self.event, tenant=self.tenant,
                custom_recap_template=template, created_by=self.system_user,
            )
            for name, value in values.items():
                recap_models.CustomFieldValue.objects.create(
                    custom_recap=recap, custom_field=fields[name], value=value,
                    created_by=self.system_user,
                )

    def _run(self):
        out = io.StringIO()
        call_command("audit_number_fixes", tenant_id=self.tenant.id, as_json=True, stdout=out)
        raw = out.getvalue()
        blob = raw.split("NUMBERFIXES_JSON_START", 1)[1].split("NUMBERFIXES_JSON_END", 1)[0]
        return raw, json.loads(blob)["tenants"][0]

    def test_reports_each_fix_separately(self):
        raw, row = self._run()
        sold = row["sold_wording"]
        assert sold["units_sold_client_visible"] == {"before": 40, "after": 0}
        assert sold["fields_no_longer_sold"][0]["field"] == (
            "TOTAL - how many cans did you give away?"
        )
        parsing = row["number_parsing"]
        assert parsing["answers_read_differently"] == 1  # "1,234"
        # The report reader took 1; the list/dashboard reader took 1234.
        assert parsing["net_change_vs_first_digits_reader"] == 1233
        assert parsing["net_change_vs_strip_reader"] == 0
        approval = row["approved_only"]
        rep = approval["campaign_reports_all_requests"]
        assert rep["before"]["consumers_reached"] == 1234 + 60
        assert rep["after"]["consumers_reached"] == 1234
        assert approval["client_home_last_30_days"] == {
            "recaps_no_longer_counted": 1,
            "consumers_no_longer_counted": 60,
        }
        assert "Torch Test" in raw

    def test_writes_nothing(self):
        before = list(recap_models.CustomFieldValue.objects.values_list("id", "value"))
        approvals = list(recap_models.CustomRecap.objects.values_list("id", "approved"))
        self._run()
        assert list(recap_models.CustomFieldValue.objects.values_list("id", "value")) == before
        assert list(recap_models.CustomRecap.objects.values_list("id", "approved")) == approvals

    @override_settings(INTERNAL_CRON_SECRET=SECRET)
    def test_cron_endpoint_is_secret_gated(self):
        assert Client().post("/internal/cron/audit-number-fixes").status_code == 401
        resp = Client().post(
            "/internal/cron/audit-number-fixes",
            {"tenant_id": str(self.tenant.id), "no_reports": "1"},
            HTTP_X_CRON_SECRET=SECRET,
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["ok"] is True
        assert "approved_only" not in body["data"]["tenants"][0]
