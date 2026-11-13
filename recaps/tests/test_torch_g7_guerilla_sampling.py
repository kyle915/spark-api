"""Torch THC: G7 Guerilla Sampling is its own walk-up program."""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.management.commands.setup_torch_g7_guerilla_sampling import (
    EVENT_LABEL,
    OPTIONAL_FIELDS,
    PHOTO_BUCKETS,
    PICKER_DESCRIPTION,
    PICKER_LABEL,
    SPEC_FIELDS,
    TEMPLATE_NAME,
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
from recaps.recap_types import RECAP_TYPE_GUERILLA, recap_type_for_name
from recaps.tenant_overview import tenant_conversion_kpis

VALID_SECRET = "test-cron-secret-value-only-for-tests"
URL = "/internal/cron/setup-torch-g7-guerilla-sampling"


def _labels() -> list[str]:
    return [name for _, fields in SPEC_FIELDS for name, *_ in fields]


class TestSpec:
    def test_doc_questions_without_test_group(self):
        names = _labels()
        assert names == [
            "Your Name",
            "Date & Time slot you sampled",
            "Where did you sample?",
            "Who accepted samples?",
            "TOTAL - how many cans did you give away?",
            "# of samples for each flavor",
            "How many QR Code Scans",
            "Koozies Distributed (total out of 25)",
            "Number of people asking where they could buy the product?",
            "What did people like about the product?",
            "What did people not like about the product?",
            "Consumer feedback (provide 2-3 comments consumers are saying)",
            "Additional notes/feedback (include 2-3 notes about the "
            "sampling location/crowds/product)",
            "Top competitors in the market? Any competitors mentioned?",
            "Are any local businesses interested in supplying Torch? "
            "Include their name, address, and number",
        ]
        assert not any("test group" in name.lower() for name in names)
        optional = {
            name
            for _, fields in SPEC_FIELDS
            for name, _kind, required, *_ in fields
            if not required
        }
        assert optional == set(OPTIONAL_FIELDS)

    def test_photo_recap_required_and_parking_optional(self):
        assert [b["name"] for b in PHOTO_BUCKETS] == ["Photo Recap", "Parking Receipt"]
        assert PHOTO_BUCKETS[0]["min"] == 1
        assert "min" not in PHOTO_BUCKETS[1]

    def test_name_is_guerilla_not_retail_or_store(self):
        assert recap_type_for_name(EVENT_LABEL) == RECAP_TYPE_GUERILLA
        assert recap_type_for_name(TEMPLATE_NAME) == RECAP_TYPE_GUERILLA
        assert "retail" not in EVENT_LABEL.lower()


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
        self.guerilla = self.create_event_type("Guerilla Activation", self.tenant)
        self.tenant.checkin_event_type = self.retail
        self.retail_buckets = [{"name": "Sampling photos"}]
        self.guerilla_buckets = [{"name": "Consumer Sampling Pictures", "min": 8}]
        self.tenant.checkin_photo_buckets = {
            "Retail Sampling": list(self.retail_buckets),
            "Guerilla Activation": list(self.guerilla_buckets),
        }
        self.tenant.checkin_program_picker = {
            "title": "What's your execution type?",
            "options": [
                {
                    "eventType": "Retail Sampling",
                    "label": "Retail",
                    "description": "In-store demos.",
                },
                {
                    "eventType": "Guerilla Activation",
                    "label": "Guerilla",
                    "description": "Street-team sampling.",
                },
            ],
        }
        self.tenant.save()
        self.tenant.checkin_event_types.set([self.retail, self.guerilla])
        for name in ("Sampling photos", "Consumer Sampling Pictures"):
            FileRecapCategory.objects.create(
                name=name, tenant=self.tenant, created_by=self.sys
            )
        self.number = CustomRecapFieldType.objects.create(
            name="number", created_by=self.sys
        )
        self.retail_tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Torch THC-Retail Sampling",
            event_type=self.retail,
            product_samples=True,
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
        call_command("setup_torch_g7_guerilla_sampling", *args, stdout=out)
        return out.getvalue()

    def _template(self):
        return CustomRecapTemplate.objects.filter(
            tenant=self.tenant, name=TEMPLATE_NAME
        ).first()

    def test_dry_run_writes_nothing(self):
        before_buckets = self.tenant.checkin_photo_buckets
        before_picker = self.tenant.checkin_program_picker
        log = self._run()
        assert "DRY-RUN" in log
        assert self._template() is None
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_photo_buckets == before_buckets
        assert self.tenant.checkin_program_picker == before_picker
        assert list(self.tenant.checkin_event_types.order_by("id")) == [
            self.retail,
            self.guerilla,
        ]
        assert not FileRecapCategory.objects.filter(
            tenant=self.tenant, name="Photo Recap"
        ).exists()

    def test_apply_adds_the_program_without_touching_the_others(self):
        self._run("--apply")
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "TH-2HRV3D"
        assert self.tenant.checkin_recap_code == "TH-AGENCY"
        assert self.tenant.checkin_event_type_id == self.retail.id
        assert [et.name for et in self.tenant.checkin_event_types.order_by("id")] == [
            "Retail Sampling",
            "Guerilla Activation",
            EVENT_LABEL,
        ]

        buckets = self.tenant.checkin_photo_buckets
        assert buckets["Retail Sampling"] == self.retail_buckets
        assert buckets["Guerilla Activation"] == self.guerilla_buckets
        assert buckets[EVENT_LABEL] == [
            {"name": "Photo Recap", "helper": "Upload your sampling photos", "min": 1},
            {"name": "Parking Receipt"},
        ]
        assert FileRecapCategory.objects.filter(
            tenant=self.tenant, name="Photo Recap"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=self.tenant, name="Parking Receipt"
        ).exists()

        options = self.tenant.checkin_program_picker["options"]
        assert [row["label"] for row in options] == [
            "Retail",
            "Guerilla",
            PICKER_LABEL,
        ]
        assert options[-1]["eventType"] == EVENT_LABEL
        assert options[-1]["description"] == PICKER_DESCRIPTION

        tpl = self._template()
        assert tpl.event_type.name == EVENT_LABEL
        assert tpl.product_samples is False
        assert tpl.sales_performance is False
        fields = list(
            CustomField.objects.filter(custom_recap_template=tpl).order_by(
                "recap_section__order", "order"
            )
        )
        assert [f.name for f in fields] == _labels()
        assert not any("test group" in f.name.lower() for f in fields)
        businesses = fields[-1]
        assert businesses.required is False
        assert fields[0].required is True
        assert not CustomField.objects.filter(
            custom_recap_template=tpl, recap_section=self.retail_section
        ).exists()
        self.retail_section.refresh_from_db()
        assert self.retail_section.order == 5
        assert self.retail_tpl.product_samples is True

    def test_apply_is_idempotent(self):
        self._run("--apply")
        before = list(
            CustomField.objects.filter(custom_recap_template=self._template())
            .order_by("id")
            .values_list("id", "name", "order", "required")
        )
        categories = FileRecapCategory.objects.filter(tenant=self.tenant).count()
        log = self._run("--apply")
        after = list(
            CustomField.objects.filter(custom_recap_template=self._template())
            .order_by("id")
            .values_list("id", "name", "order", "required")
        )
        assert before == after
        assert "+0 ~0" in log
        assert (
            FileRecapCategory.objects.filter(tenant=self.tenant).count() == categories
        )
        assert CustomRecapTemplate.objects.filter(tenant=self.tenant).count() == 2

    def test_walkup_uses_the_g7_form_and_skips_the_store_number(self):
        self._run("--apply")
        self.tenant.refresh_from_db()
        tpl = self._template()
        event = self.create_event(
            name="Sidewalk", tenant=self.tenant, event_type=tpl.event_type
        )
        retail_event = self.create_event(
            name="Store", tenant=self.tenant, event_type=self.retail
        )

        assert checkin_web.resolve_template_for_event(event).name == TEMPLATE_NAME
        assert (
            checkin_web.resolve_template_for_event(retail_event).id
            == self.retail_tpl.id
        )
        assert checkin_web.is_event_activation_type(tpl.event_type)
        assert not checkin_web.requires_store_identity(self.tenant, event)
        assert checkin_web.requires_store_identity(self.tenant, retail_event)

        photos = checkin_web.serialize_photo_buckets(event)
        assert [(b["name"], b["min"]) for b in photos] == [
            ("Photo Recap", 1),
            ("Parking Receipt", 0),
        ]
        shown = checkin_web.build_tenant_context(self.tenant)["eventTypes"]
        assert [(t["label"], t["name"]) for t in shown] == [
            ("Retail", "Retail Sampling"),
            ("Guerilla", "Guerilla Activation"),
            (PICKER_LABEL, EVENT_LABEL),
        ]

    def test_approved_recap_stays_out_of_conversion(self):
        self._run("--apply")
        tpl = self._template()
        when = timezone.make_aware(
            datetime.combine(timezone.localdate(), datetime.min.time())
        )
        event = self.create_event(
            name="Sidewalk", tenant=self.tenant, event_type=tpl.event_type, date=when
        )
        recap = CustomRecap.objects.create(
            name="Sidewalk",
            approved=True,
            event=event,
            tenant=self.tenant,
            custom_recap_template=tpl,
            total_engagements=40,
            created_by=self.sys,
        )
        field = CustomField.objects.get(
            custom_recap_template=tpl, name="TOTAL - how many cans did you give away?"
        )
        CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=field, value="40", created_by=self.sys
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
        assert args[0] == "setup_torch_g7_guerilla_sampling"
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
