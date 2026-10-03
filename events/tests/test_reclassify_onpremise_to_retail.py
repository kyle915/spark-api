"""reclassify_onpremise_to_retail moves a tenant's On-Premise rows to Retail."""

from datetime import datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client as DjangoClient, override_settings
from django.utils import timezone

from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_overview import tenant_activation_breakdown, tenant_conversion_kpis


@pytest.mark.django_db(transaction=True)
class TestReclassifyOnpremiseToRetail:
    @pytest.fixture(autouse=True)
    def setup(self):
        from tenants.tests.base import BaseGraphQLTestCase

        helper = BaseGraphQLTestCase()
        self.sys = helper.get_system_user()
        self.tenant = helper.create_tenant(name="Torch Onprem Co", slug="torch-op")
        self.other = helper.create_tenant(name="MAB Onprem Co", slug="mab-op")
        self.today = timezone.localdate()
        self.when = timezone.make_aware(datetime(self.today.year, self.today.month, self.today.day, 12))
        self.t = self._types(self.tenant)
        self.o = self._types(self.other)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch THC-Retail Sampling", event_type=self.t["et_retail"], tenant=self.tenant, created_by=self.sys
        )
        num = recap_models.CustomRecapFieldType.objects.create(name="Number", created_by=self.sys)
        section = recap_models.RecapSection.objects.create(name="S", tenant=self.tenant, created_by=self.sys)
        self.fields = {
            name: recap_models.CustomField.objects.create(
                name=name, custom_recap_template=self.template, custom_field_type=num,
                recap_section=section, created_by=self.sys,
            )
            for name in ("Total number of consumers sampled", "How many single cans did consumers purchase?")
        }

    def _types(self, tenant):
        mk_rt = lambda n: event_models.RequestType.objects.create(name=n, tenant=tenant, created_by=self.sys)  # noqa: E731
        mk_et = lambda n: event_models.EventType.objects.create(  # noqa: E731
            name=n, slug=f"{tenant.slug}-{n}".lower().replace(" ", "-"), tenant=tenant, created_by=self.sys
        )
        return {
            "rt_retail": mk_rt("Retail Sampling"),
            "rt_onprem": mk_rt("On-Premise"),
            "rt_bar": mk_rt("Bar Sampling"),
            "rt_event": mk_rt("Event Activation"),
            "et_retail": mk_et("Retail Sampling"),
            "et_onprem": mk_et("On-Premise Sampling"),
            "status": event_models.RequestStatus.objects.create(
                name="Approved", slug="approved", tenant=tenant, created_by=self.sys
            ),
        }

    def _request(self, tenant, types, rt, et):
        req = event_models.Request.objects.create(
            name="Total Wine", address="1 Main", tenant=tenant, status=types["status"],
            request_type=rt, date=self.when, created_by=self.sys, store_number="903",
        )
        ev = event_models.Event.objects.create(
            name="Total Wine", tenant=tenant, request=req, address="1 Main", date=self.when,
            event_type=et, created_by=self.sys, updated_by=self.sys,
        )
        return req, ev

    def _recap(self, ev, sampled, cans):
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=True, submitted_at=self.when, event=ev, tenant=self.tenant,
            custom_recap_template=self.template, created_by=self.sys,
        )
        for name, value in (
            ("Total number of consumers sampled", sampled),
            ("How many single cans did consumers purchase?", cans),
        ):
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=self.fields[name], value=str(value), created_by=self.sys
            )
        return recap

    def _buckets(self, tenant):
        return {b["key"]: b["count"] for b in tenant_activation_breakdown(tenant.id)["buckets"]}

    def test_dry_run_reports_and_writes_nothing(self):
        req, ev = self._request(self.tenant, self.t, self.t["rt_onprem"], self.t["et_onprem"])
        recap = self._recap(ev, 40, 4)
        out = StringIO()
        call_command("reclassify_onpremise_to_retail", tenant="torch-op", stdout=out)
        log = out.getvalue()
        assert "DRY RUN" in log
        assert f"request #{req.id}" in log and "'On-Premise' -> 'Retail Sampling'" in log
        assert f"custom recap #{recap.id}" in log and "Total Wine #903" in log
        req.refresh_from_db()
        assert req.request_type_id == self.t["rt_onprem"].id
        assert event_models.RequestType.objects.filter(id=self.t["rt_onprem"].id).exists()

    def test_apply_moves_rows_retires_types_and_leaves_other_tenant(self):
        req, ev = self._request(self.tenant, self.t, self.t["rt_onprem"], self.t["et_onprem"])
        bar_req, bar_ev = self._request(self.tenant, self.t, self.t["rt_bar"], self.t["et_retail"])
        event_req, _ = self._request(self.tenant, self.t, self.t["rt_event"], self.t["et_retail"])
        recap = self._recap(ev, 40, 4)
        mab_req, mab_ev = self._request(self.other, self.o, self.o["rt_onprem"], self.o["et_onprem"])
        self.tenant.checkin_event_type = self.t["et_onprem"]
        self.tenant.save(update_fields=["checkin_event_type"])
        self.tenant.checkin_event_types.add(self.t["et_retail"], self.t["et_onprem"])
        assert self._buckets(self.tenant)["onprem"] == 2
        conv_before = tenant_conversion_kpis(self.tenant.id, start=self.today, end=self.today)
        req_updated = req.updated_at
        recap_updated = recap.updated_at

        out = StringIO()
        call_command("reclassify_onpremise_to_retail", tenant="torch-op", apply=True, stdout=out)
        assert "APPLIED — moved 2 request(s), 1 event(s)" in out.getvalue()

        for r in (req, bar_req, event_req, mab_req):
            r.refresh_from_db()
        ev.refresh_from_db()
        mab_ev.refresh_from_db()
        recap.refresh_from_db()
        self.tenant.refresh_from_db()
        assert req.request_type_id == bar_req.request_type_id == self.t["rt_retail"].id
        assert event_req.request_type_id == self.t["rt_event"].id
        assert ev.event_type_id == self.t["et_retail"].id
        assert req.updated_at == req_updated and req.status_id == self.t["status"].id
        assert recap.approved is True and recap.updated_at == recap_updated
        assert self.tenant.checkin_event_type_id == self.t["et_retail"].id
        assert list(self.tenant.checkin_event_types.values_list("id", flat=True)) == [self.t["et_retail"].id]
        for key in ("rt_onprem", "rt_bar"):
            assert not event_models.RequestType.objects.filter(id=self.t[key].id).exists()
        assert not event_models.EventType.objects.filter(id=self.t["et_onprem"].id).exists()
        assert event_models.RequestType.objects.filter(id=self.t["rt_event"].id).exists()

        assert mab_req.request_type_id == self.o["rt_onprem"].id
        assert mab_ev.event_type_id == self.o["et_onprem"].id
        assert event_models.RequestType.objects.filter(id=self.o["rt_bar"].id).exists()

        buckets = self._buckets(self.tenant)
        assert buckets["onprem"] == 0 and buckets["retail"] == 2
        conv_after = tenant_conversion_kpis(self.tenant.id, start=self.today, end=self.today)
        assert (conv_after["sold"], conv_after["engagements"]) == (conv_before["sold"], conv_before["engagements"]) == (4, 40)

    def test_keep_types_repoints_without_retiring(self):
        req, _ = self._request(self.tenant, self.t, self.t["rt_onprem"], self.t["et_retail"])
        call_command("reclassify_onpremise_to_retail", tenant="torch-op", apply=True, keep_types=True, stdout=StringIO())
        req.refresh_from_db()
        assert req.request_type_id == self.t["rt_retail"].id
        assert event_models.RequestType.objects.filter(id=self.t["rt_onprem"].id).exists()

    @override_settings(INTERNAL_CRON_SECRET="s3cret")
    def test_cron_view_is_secret_gated_and_dry_run(self):
        http = DjangoClient()
        url = "/internal/cron/reclassify-onpremise-to-retail"
        assert http.post(url, {"tenant": "torch-op"}).status_code in (401, 403)
        res = http.post(url, {"tenant": "torch-op"}, HTTP_X_CRON_SECRET="s3cret")
        assert res.status_code == 200, res.content
        assert "DRY RUN" in res.json()["log"]
        assert event_models.RequestType.objects.filter(id=self.t["rt_onprem"].id).exists()
