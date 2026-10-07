"""Torch THC execution types: Event / Guerilla / Seeding / Retail on TH-2HRV3D."""

from __future__ import annotations

import io
import json
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
from events.checkin_views import _stamp_recap_only
from recaps.management.commands.setup_torch_event_activation import (
    EMAIL_FIELDS,
    EMAIL_SECTION,
    EVENT_LABEL,
    SALES_FIELD_RE,
    SAMPLE_FORMAT_FIELD,
    SAMPLE_FORMAT_OPTS,
    SPEC,
    TEMPLATE_NAME as EVENT_TEMPLATE,
)
from recaps.management.commands.setup_torch_execution_types import (
    DROP_OFF_LOCATIONS_FIELD,
    EMAIL_TEMPLATES,
    GUERILLA_LABEL,
    GUERILLA_TEMPLATE,
    PICKER_TITLE,
    RETAIL_DRY_DEMO_OPT,
    SEEDING_LABEL,
    SEEDING_SPEC,
    SEEDING_TEMPLATE,
    build_guerilla_spec,
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
from recaps.tenant_insights import build_insight_buckets_scoped
from recaps.tenant_overview import (
    _CUSTOM_KPI_NAME_RE,
    _activation_bucket_for_type_name,
    emails_collected_metrics,
    tenant_conversion_kpis,
)
from recaps.types import _CONSUMERS_SAMPLED_RE, _PEOPLE_ENGAGED_RE

VALID_SECRET = "test-cron-secret-value-only-for-tests"
URL = "/internal/cron/setup-torch-execution-types"
GUERILLA_SPEC = build_guerilla_spec([])


def _labels(spec) -> list[str]:
    return [name for _, fields in spec for name, *_ in fields]


def _field(spec, name):
    return next(f for _, fields in spec for f in fields if f[0] == name)


class TestSpec:
    def test_guerilla_is_sampling_only(self):
        for label in _labels(GUERILLA_SPEC):
            assert not SALES_FIELD_RE.search(label), label
        blob = " ".join(_labels(GUERILLA_SPEC)).lower()
        for banned in ("units sold", "packs", "single cans", "account spend"):
            assert banned not in blob

    @pytest.mark.parametrize("spec", [SPEC, GUERILLA_SPEC], ids=["event", "guerilla"])
    def test_sample_format_required_multiselect(self, spec):
        _, kind, required, options, _ = _field(spec, SAMPLE_FORMAT_FIELD)
        assert (kind, required) == ("multiselect", True)
        assert options == ["Full can", "4oz pour"] == SAMPLE_FORMAT_OPTS

    @pytest.mark.parametrize("spec", [SPEC, GUERILLA_SPEC], ids=["event", "guerilla"])
    def test_email_section_optional(self, spec):
        section = dict(spec)[EMAIL_SECTION]
        assert [f[0] for f in section] == [
            "Email addresses collected",
            "Collection method",
            "Email collection notes",
        ]
        assert not any(f[2] for f in section)
        method = section[1]
        assert method[1] == "multiselect"
        assert method[3] == ["QR code", "Tablet / sign-up form", "Paper sheet", "Other"]

    def test_email_fields_never_read_as_kpis(self):
        for name, *_ in EMAIL_FIELDS + [(SAMPLE_FORMAT_FIELD,)]:
            assert not _CUSTOM_KPI_NAME_RE.search(name), name
            assert not _CONSUMERS_SAMPLED_RE.search(name), name
            assert not _PEOPLE_ENGAGED_RE.search(name), name

    def test_programs_bucket_outside_conversion(self):
        assert _activation_bucket_for_type_name(GUERILLA_LABEL)[0] == "event"
        assert _activation_bucket_for_type_name(GUERILLA_TEMPLATE)[0] == "other"
        assert _activation_bucket_for_type_name(SEEDING_LABEL)[0] == "seeding"
        assert _activation_bucket_for_type_name(SEEDING_TEMPLATE)[0] == "seeding"
        for name in (GUERILLA_LABEL, GUERILLA_TEMPLATE, SEEDING_LABEL, SEEDING_TEMPLATE):
            assert not re.search(r"retail|on[-\s]?prem|bar|venue", name, re.I)

    def test_seeding_mirrors_ld(self):
        assert _labels(SEEDING_SPEC) == [
            DROP_OFF_LOCATIONS_FIELD,
            "Account feedback",
            "Total mileage",
            *(f[0] for f in EMAIL_FIELDS),
        ]
        assert not any(f[2] for f in dict(SEEDING_SPEC)[EMAIL_SECTION])
        assert checkin_web.is_product_seeding_event_type(type("ET", (), {"name": SEEDING_LABEL}))

    def test_email_section_on_exactly_four_templates(self):
        assert set(EMAIL_TEMPLATES) == {
            EVENT_TEMPLATE,
            GUERILLA_TEMPLATE,
            SEEDING_TEMPLATE,
            "Torch THC-Retail Sampling",
        }

    def test_recap_only_strips_email_section(self):
        template = {
            "id": "1",
            "sections": [
                {"name": "Consumer Engagement", "fields": []},
                {"name": EMAIL_SECTION, "fields": [{"name": "Email addresses collected"}]},
            ],
        }
        stripped = checkin_web.strip_recap_only_sections(template)
        assert [s["name"] for s in stripped["sections"]] == ["Consumer Engagement"]
        assert len(template["sections"]) == 2
        assert checkin_web.strip_recap_only_sections(None) is None


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
        self.retail_buckets = [{"name": "Sampling photos"}, {"name": "Product Spend"}]
        self.tenant.checkin_photo_buckets = list(self.retail_buckets)
        self.tenant.save()
        for b in self.retail_buckets:
            FileRecapCategory.objects.create(name=b["name"], tenant=self.tenant, created_by=self.sys)
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
        self.sampled = CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=self.retail_tpl,
            custom_field_type=self.number,
            recap_section=self.retail_section,
            order=10,
            created_by=self.sys,
        )
        self.engaged = CustomField.objects.create(
            name="People engaged",
            custom_recap_template=self.retail_tpl,
            custom_field_type=self.number,
            recap_section=self.retail_section,
            order=20,
            created_by=self.sys,
        )
        self.later = CustomField.objects.create(
            name="How many consumers were trying Torch for the first time?",
            custom_recap_template=self.retail_tpl,
            custom_field_type=self.number,
            recap_section=self.retail_section,
            order=30,
            created_by=self.sys,
        )

    def _run(self, *args) -> str:
        out = io.StringIO()
        call_command("setup_torch_execution_types", *args, stdout=out)
        return out.getvalue()

    def _tpl(self, name):
        return CustomRecapTemplate.objects.filter(tenant=self.tenant, name=name).first()

    def test_dry_run_writes_nothing(self):
        log = self._run()
        assert "DRY-RUN" in log
        for name in (EVENT_TEMPLATE, GUERILLA_TEMPLATE, SEEDING_TEMPLATE):
            assert self._tpl(name) is None
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_program_picker is None
        assert not self.tenant.checkin_event_types.exists()
        assert CustomField.objects.filter(custom_recap_template=self.retail_tpl).count() == 3

    def test_apply_builds_four_options_in_order(self):
        self._run("--apply")
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "TH-2HRV3D"
        assert self.tenant.checkin_recap_code == "TH-AGENCY"
        assert self.tenant.checkin_event_type_id == self.retail.id

        ctx = checkin_web.build_tenant_context(self.tenant)
        assert ctx["programPickerTitle"] == PICKER_TITLE
        assert [(t["label"], t["name"]) for t in ctx["eventTypes"]] == [
            ("Event", EVENT_LABEL),
            ("Guerilla", GUERILLA_LABEL),
            ("Seeding", SEEDING_LABEL),
            ("Retail", "Retail Sampling"),
        ]
        assert all(t["description"] for t in ctx["eventTypes"])
        agency = checkin_web.build_tenant_context(self.tenant, recap_only=True)
        assert agency["eventTypes"] == []

        buckets = self.tenant.checkin_photo_buckets
        assert buckets["Retail Sampling"] == self.retail_buckets
        assert [b["name"] for b in buckets[SEEDING_LABEL]] == ["Drop-off Placement"]
        assert buckets[GUERILLA_LABEL] == buckets[EVENT_LABEL]

        guerilla = self._tpl(GUERILLA_TEMPLATE)
        assert guerilla.event_type.name == GUERILLA_LABEL
        assert guerilla.sales_performance is False
        assert sorted(
            CustomField.objects.filter(custom_recap_template=guerilla).values_list("name", flat=True)
        ) == sorted(_labels(GUERILLA_SPEC))
        seeding = self._tpl(SEEDING_TEMPLATE)
        assert seeding.event_type.name == SEEDING_LABEL
        assert seeding.product_samples is True
        assert sorted(
            CustomField.objects.filter(custom_recap_template=seeding).values_list("name", flat=True)
        ) == sorted(_labels(SEEDING_SPEC))
        event = self._tpl(EVENT_TEMPLATE)
        assert CustomField.objects.filter(
            custom_recap_template=event, name=SAMPLE_FORMAT_FIELD, required=True
        ).exists()

    def test_retail_gains_sample_format_and_email_section(self):
        self._run("--apply")
        form = CustomField.objects.get(custom_recap_template=self.retail_tpl, name=SAMPLE_FORMAT_FIELD)
        assert form.required is True
        assert form.options == ["Full can", "4oz pour", RETAIL_DRY_DEMO_OPT]
        assert form.recap_section_id == self.retail_section.id
        assert self.engaged.order < form.order < self.later.order

        emails = CustomField.objects.filter(
            custom_recap_template=self.retail_tpl, recap_section__name=EMAIL_SECTION
        ).order_by("order")
        assert [f.name for f in emails] == [f[0] for f in EMAIL_FIELDS]
        assert not any(f.required for f in emails)
        section = emails[0].recap_section
        assert section.id != self.retail_section.id
        assert section.order == self.retail_section.order

        retail_event = self.create_event(name="Store", tenant=self.tenant, event_type=self.retail)
        names = [s["name"] for s in checkin_web.serialize_template(retail_event)["sections"]]
        assert names.index(EMAIL_SECTION) == names.index("Consumer Engagement") + 1

    def test_apply_is_idempotent(self):
        self._run("--apply")
        snapshot = list(
            CustomField.objects.filter(custom_recap_template__tenant=self.tenant)
            .order_by("id")
            .values_list("id", "name", "order", "required", "options", "recap_section_id")
        )
        self._run("--apply")
        again = list(
            CustomField.objects.filter(custom_recap_template__tenant=self.tenant)
            .order_by("id")
            .values_list("id", "name", "order", "required", "options", "recap_section_id")
        )
        assert snapshot == again
        assert CustomRecapTemplate.objects.filter(tenant=self.tenant).count() == 4
        assert RecapSection.objects.filter(tenant=self.tenant, name=EMAIL_SECTION).count() == 4

    def _email_names(self, template) -> list[str]:
        return list(
            CustomField.objects.filter(
                custom_recap_template=template, recap_section__name=EMAIL_SECTION
            )
            .order_by("order")
            .values_list("name", flat=True)
        )

    def test_email_section_only_on_the_four_templates(self):
        legacy = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Torch THC-On Premise",
            event_type=self.create_event_type("On Premise", self.tenant),
            created_by=self.sys,
        )
        legacy_section = RecapSection.objects.create(
            name=EMAIL_SECTION, tenant=self.tenant, created_by=self.sys
        )
        unanswered, answered = (
            CustomField.objects.create(
                name=name,
                custom_recap_template=legacy,
                custom_field_type=self.number,
                recap_section=legacy_section,
                created_by=self.sys,
            )
            for name in ("Email addresses collected", "Email collection notes")
        )
        recap = CustomRecap.objects.create(
            name="Old",
            event=self.create_event(name="Old", tenant=self.tenant, event_type=legacy.event_type),
            tenant=self.tenant,
            custom_recap_template=legacy,
            created_by=self.sys,
        )
        CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=answered, value="kept", created_by=self.sys
        )

        dry = self._run()
        assert f"would prune 'Email addresses collected' [{unanswered.id}]" in dry
        assert CustomField.objects.filter(id=unanswered.id).exists()

        log = self._run("--apply")
        want = [f[0] for f in EMAIL_FIELDS]
        for name in (EVENT_TEMPLATE, GUERILLA_TEMPLATE, SEEDING_TEMPLATE):
            assert self._email_names(self._tpl(name)) == want, name
        assert self._email_names(self.retail_tpl) == want
        assert not CustomField.objects.filter(id=unanswered.id).exists()
        assert CustomField.objects.filter(id=answered.id).exists()
        assert CustomFieldValue.objects.filter(custom_recap=recap).get().value == "kept"
        assert "1 submitted answer(s)" in log

    def test_agency_payload_hides_email_section(self):
        self._run("--apply")
        store = self.create_event(name="Store", tenant=self.tenant, event_type=self.retail)
        main = checkin_web.build_public_context(store)
        assert EMAIL_SECTION in [s["name"] for s in main["template"]["sections"]]

        agency = _stamp_recap_only(checkin_web.build_public_context(store), "TH-AGENCY", self.tenant)
        names = [s["name"] for s in agency["template"]["sections"]]
        assert EMAIL_SECTION not in names
        assert "Consumer Engagement" in names
        main_again = _stamp_recap_only(
            checkin_web.build_public_context(store), "TH-2HRV3D", self.tenant
        )
        assert EMAIL_SECTION in [s["name"] for s in main_again["template"]["sections"]]

    def test_missing_retail_template_blocks(self):
        self.retail_tpl.name = "Something else"
        self.retail_tpl.save(update_fields=["name"])
        with pytest.raises(CommandError):
            self._run()

    def test_walkup_routes_each_program(self):
        self._run("--apply")
        for label, template in (
            (EVENT_LABEL, EVENT_TEMPLATE),
            (GUERILLA_LABEL, GUERILLA_TEMPLATE),
            (SEEDING_LABEL, SEEDING_TEMPLATE),
        ):
            et = self._tpl(template).event_type
            assert et.name == label
            ev = self.create_event(name=label, tenant=self.tenant, event_type=et)
            assert checkin_web.resolve_template_for_event(ev).name == template
        store = self.create_event(name="Store", tenant=self.tenant, event_type=self.retail)
        assert checkin_web.resolve_template_for_event(store).id == self.retail_tpl.id

    def _approved_recap(self, template, values: dict, approved=True):
        when = timezone.make_aware(datetime.combine(timezone.localdate(), datetime.min.time()))
        event = self.create_event(
            name=template.name, tenant=self.tenant, event_type=template.event_type, date=when
        )
        recap = CustomRecap.objects.create(
            name=template.name,
            approved=approved,
            event=event,
            tenant=self.tenant,
            custom_recap_template=template,
            created_by=self.sys,
        )
        for name, value in values.items():
            CustomFieldValue.objects.create(
                custom_recap=recap,
                custom_field=CustomField.objects.get(custom_recap_template=template, name=name),
                value=value,
                created_by=self.sys,
            )
        return recap

    def test_guerilla_recap_excluded_from_conversion(self):
        self._run("--apply")
        self._approved_recap(
            self._tpl(GUERILLA_TEMPLATE),
            {"How many TOTAL consumers did you sample?": "250"},
        )
        today = timezone.localdate()
        kpis = tenant_conversion_kpis(self.tenant.id, today - timedelta(days=7), today)
        assert kpis["engagements"] == 0
        assert kpis["pct"] is None

    def test_emails_collected_only_from_recorded_approved_recaps(self):
        self._run("--apply")
        guerilla = self._tpl(GUERILLA_TEMPLATE)
        self._approved_recap(
            guerilla,
            {
                "Email addresses collected": "110",
                "Collection method": json.dumps(["QR code"]),
                "How many TOTAL consumers did you sample?": "300",
            },
        )
        self._approved_recap(
            self.retail_tpl,
            {
                "Email addresses collected": "0",
                "Collection method": json.dumps(["Paper sheet", "QR code"]),
            },
        )
        self._approved_recap(guerilla, {"How many TOTAL consumers did you sample?": "50"})
        self._approved_recap(guerilla, {"Email addresses collected": "999"}, approved=False)

        today = timezone.localdate()
        m = emails_collected_metrics(self.tenant.id, today - timedelta(days=7), today)
        assert m["emails"] == 110
        assert m["recaps"] == 2
        assert m["methods"] == [("QR code", 2), ("Paper sheet", 1)]

        self.tenant.insights_trend_series = "sales"
        self.tenant.save(update_fields=["insights_trend_series"])
        buckets, _ = build_insight_buckets_scoped(
            self.tenant.id, today - timedelta(days=7), today
        )
        card = next(b for b in buckets if b["key"] == "emails")
        assert card["metric"] == "110"
        assert "Not counted in consumers sampled" in card["detail"]
        reach = next(b for b in buckets if b["key"] == "reach")
        assert reach["metric"] == "0"

    def test_no_emails_card_without_answers(self):
        self._run("--apply")
        today = timezone.localdate()
        m = emails_collected_metrics(self.tenant.id, None, today)
        assert m == {"emails": 0, "recaps": 0, "methods": []}


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
    def test_dry_run_default_and_apply(self, mock_call):
        resp = Client().post(URL, HTTP_X_CRON_SECRET=VALID_SECRET)
        assert resp.status_code == 200
        args, kwargs = mock_call.call_args
        assert args[0] == "setup_torch_execution_types"
        assert "apply" not in kwargs
        resp = Client().post(URL, {"apply": "true"}, HTTP_X_CRON_SECRET=VALID_SECRET)
        assert mock_call.call_args.kwargs["apply"] is True
        assert resp.json()["applied"] is True
