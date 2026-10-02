"""Torch THC Event Activation: sampling-only template on the TH-2HRV3D walk-up."""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.management.commands.setup_torch_event_activation import (
    ACTIVATION_BUCKETS,
    COMPETITOR_FIELD,
    EVENT_LABEL,
    SALES_FIELD_RE,
    SPEC,
    TEMPLATE_NAME,
    build_spec,
)
from recaps.models import (
    CustomField,
    CustomFieldValue,
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileRecapCategory,
    RecapSection,
)
from recaps.tenant_overview import _ACTIVATION_BUCKETS, tenant_conversion_kpis
from recaps.types import _CONSUMERS_SAMPLED_RE

VALID_SECRET = "test-cron-secret-value-only-for-tests"
URL = "/internal/cron/setup-torch-event-activation"


def _labels(spec=SPEC) -> list[str]:
    return [name for _, fields in spec for name, *_ in fields]


class TestSpec:
    def test_sampling_only_no_sales_fields(self):
        for label in _labels():
            assert not SALES_FIELD_RE.search(label), label
        blob = " ".join(_labels()).lower()
        for banned in ("units sold", "packs", "single cans", "account spend", "conversion"):
            assert banned not in blob

    def test_torch_copy_only(self):
        blob = " ".join(_labels())
        for other in ("Liquid Death", "Mark Anthony", "White Claw", "Neutonic", "Hiyo"):
            assert other not in blob
        assert "Torch" in blob

    def test_sections_and_competitor_question(self):
        assert [s for s, _ in SPEC] == [
            "Event Details",
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Products Sampled",
        ]
        assert COMPETITOR_FIELD in _labels()

    def test_products_sampled_from_catalog(self):
        opts = ["Lite — Black Cherry 5mg 12oz", "Classic — Iced Tea Lemonade 10mg 12oz"]
        section, fields = build_spec(opts)[-1]
        assert section == "Products Sampled"
        name, kind, required, options, _ = fields[0]
        assert (name, kind, required, options) == ("Products Sampled", "multiselect", True, opts)
        assert SPEC[-1][1][0][3] == []

    def test_consumers_sampled_and_people_engaged_stay_distinct(self):
        sampled = [n for n in _labels() if _CONSUMERS_SAMPLED_RE.search(n)]
        assert sampled == ["How many TOTAL consumers did you sample?"]
        engaged = next(n for n in _labels() if "engage with in total" in n)
        assert not _CONSUMERS_SAMPLED_RE.search(engaged)

    def test_activation_buckets_mirror_ld(self):
        assert [b["name"] for b in ACTIVATION_BUCKETS] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        assert ACTIVATION_BUCKETS[1]["min"] == 8

    def test_buckets_as_event_not_retail_or_onprem(self):
        for name in (EVENT_LABEL, TEMPLATE_NAME):
            hit = next(key for key, _, pat in _ACTIVATION_BUCKETS if pat.search(name))
            assert hit == "event"
            assert not re.search(r"retail|on[-\s]?prem|bar|venue", name, re.I)


@pytest.mark.django_db(transaction=True)
class TestSetupCommand(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.sys.is_superuser = True
        self.sys.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.tenant.checkin_code = "TH-2HRV3D"
        self.tenant.checkin_recap_code = "TH-AGENCY"
        self.retail = self.create_event_type("Retail Sampling", self.tenant)
        self.tenant.checkin_event_type = self.retail
        self.retail_buckets = [
            {"name": "Sampling photos"},
            {"name": "Table Set Up"},
            {"name": "Before & After Shelf / Stock"},
            {"name": "Product Spend"},
        ]
        self.tenant.checkin_photo_buckets = list(self.retail_buckets)
        self.tenant.save()
        for b in self.retail_buckets:
            FileRecapCategory.objects.create(
                name=b["name"], tenant=self.tenant, created_by=self.sys
            )
        self.number = CustomRecapFieldType.objects.create(name="number", created_by=self.sys)
        self.retail_tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Torch THC-Retail Sampling",
            event_type=self.retail,
            created_by=self.sys,
        )
        self.retail_section = RecapSection.objects.create(
            name="Consumer Engagement", order=5, tenant=self.tenant, created_by=self.sys
        )
        CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=self.retail_tpl,
            custom_field_type=self.number,
            recap_section=self.retail_section,
            created_by=self.sys,
        )

    def _run(self, *args) -> str:
        out = io.StringIO()
        call_command("setup_torch_event_activation", *args, stdout=out)
        return out.getvalue()

    def _ea_template(self):
        return CustomRecapTemplate.objects.filter(tenant=self.tenant, name=TEMPLATE_NAME).first()

    def test_dry_run_writes_nothing(self):
        log = self._run()
        assert "DRY-RUN" in log
        assert self._ea_template() is None
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_photo_buckets == self.retail_buckets
        assert not self.tenant.checkin_event_types.exists()

    def test_apply_adds_program_template_and_buckets(self):
        self._run("--apply")
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "TH-2HRV3D"
        assert self.tenant.checkin_recap_code == "TH-AGENCY"
        assert self.tenant.checkin_event_type_id == self.retail.id
        names = [et.name for et in self.tenant.checkin_event_types.order_by("id")]
        assert names == ["Retail Sampling", EVENT_LABEL]

        buckets = self.tenant.checkin_photo_buckets
        assert buckets["default"] == self.retail_buckets
        assert buckets["Retail Sampling"] == self.retail_buckets
        assert [b["name"] for b in buckets[EVENT_LABEL]] == [
            b["name"] for b in ACTIVATION_BUCKETS
        ]
        for b in ACTIVATION_BUCKETS:
            assert FileRecapCategory.objects.filter(tenant=self.tenant, name=b["name"]).exists()

        tpl = self._ea_template()
        assert tpl.event_type.name == EVENT_LABEL
        assert tpl.product_samples is True and tpl.sales_performance is False
        fields = CustomField.objects.filter(custom_recap_template=tpl)
        assert sorted(f.name for f in fields) == sorted(_labels())
        assert not fields.filter(recap_section=self.retail_section).exists()

        # Retail Sampling form untouched.
        self.retail_section.refresh_from_db()
        assert self.retail_section.order == 5
        assert CustomField.objects.filter(custom_recap_template=self.retail_tpl).count() == 1

    def test_apply_is_idempotent(self):
        self._run("--apply")
        before = list(
            CustomField.objects.filter(custom_recap_template=self._ea_template())
            .order_by("id")
            .values_list("id", "name", "order", "required")
        )
        log = self._run("--apply")
        after = list(
            CustomField.objects.filter(custom_recap_template=self._ea_template())
            .order_by("id")
            .values_list("id", "name", "order", "required")
        )
        assert before == after
        assert "+0 ~0" in log
        assert CustomRecapTemplate.objects.filter(tenant=self.tenant).count() == 2

    def test_resolves_by_request_url_name(self):
        self.tenant.slug = "torch-prod-slug"
        self.tenant.save(update_fields=["slug"])
        log = self._run()
        assert "Torch THC" in log

    def test_rival_template_on_event_activation_blocks_apply(self):
        ea = self.create_event_type(EVENT_LABEL, self.tenant)
        CustomRecapTemplate.objects.create(
            tenant=self.tenant, name="Old activation form", event_type=ea, created_by=self.sys
        )
        with pytest.raises(CommandError):
            self._run("--apply")

    def test_walkup_resolution_and_store_identity(self):
        self._run("--apply")
        ea = self._ea_template().event_type
        ea_event = self.create_event(name="Fest", tenant=self.tenant, event_type=ea)
        retail_event = self.create_event(
            name="Store", tenant=self.tenant, event_type=self.retail
        )
        untyped = self.create_event(name="Scheduled", tenant=self.tenant)
        untyped.event_type = None
        untyped.save(update_fields=["event_type"])

        assert checkin_web.resolve_template_for_event(ea_event).name == TEMPLATE_NAME
        assert checkin_web.resolve_template_for_event(retail_event).id == self.retail_tpl.id
        # Untyped / unmatched events keep the store form, not the activation one.
        assert checkin_web.resolve_template_for_event(untyped).id == self.retail_tpl.id

        assert checkin_web.requires_store_identity(self.tenant, retail_event)
        assert not checkin_web.requires_store_identity(self.tenant, ea_event)
        assert "storeIdentity" not in checkin_web.build_public_context(ea_event, None)

        names = {
            et["name"]
            for et in checkin_web.build_tenant_context(self.tenant)["eventTypes"]
        }
        assert names == {"Retail Sampling", EVENT_LABEL}
        agency = checkin_web.build_tenant_context(self.tenant, recap_only=True)
        assert agency["eventTypes"] == []

    def test_event_activation_recap_excluded_from_conversion(self):
        self._run("--apply")
        tpl = self._ea_template()
        when = timezone.make_aware(datetime.combine(timezone.localdate(), datetime.min.time()))
        event = self.create_event(
            name="Fest", tenant=self.tenant, event_type=tpl.event_type, date=when
        )
        recap = CustomRecap.objects.create(
            name="Fest",
            approved=True,
            event=event,
            tenant=self.tenant,
            custom_recap_template=tpl,
            total_engagements=300,
            created_by=self.sys,
        )
        field = CustomField.objects.get(
            custom_recap_template=tpl, name="How many TOTAL consumers did you sample?"
        )
        CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=field, value="300", created_by=self.sys
        )
        today = timezone.localdate()
        kpis = tenant_conversion_kpis(self.tenant.id, today - timedelta(days=7), today)
        assert kpis["engagements"] == 0
        assert kpis["pct"] is None


@pytest.mark.django_db
class TestCronView:
    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_requires_secret(self, mock_call):
        resp = Client().post(URL, HTTP_X_CRON_SECRET="wrong")
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_dry_run_default(self, mock_call):
        resp = Client().post(URL, HTTP_X_CRON_SECRET=VALID_SECRET)
        assert resp.status_code == 200
        args, kwargs = mock_call.call_args
        assert args[0] == "setup_torch_event_activation"
        assert "apply" not in kwargs

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_apply_and_command_error(self, mock_call):
        resp = Client().post(URL, {"apply": "true"}, HTTP_X_CRON_SECRET=VALID_SECRET)
        assert mock_call.call_args.kwargs["apply"] is True
        assert resp.json()["applied"] is True
        mock_call.side_effect = CommandError("tenant-not-found: x")
        resp = Client().post(URL, {"tenant": "x"}, HTTP_X_CRON_SECRET=VALID_SECRET)
        assert resp.status_code == 400
        assert resp.json()["error"] == "bad-input"
