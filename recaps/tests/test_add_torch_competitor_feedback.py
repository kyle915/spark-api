"""Tests for Torch competitor-products feedback field seed."""

from io import StringIO

import pytest
from django.core.management import call_command

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.management.commands.add_torch_competitor_feedback import (
    FIELD_NAME,
    FIELD_ORDER,
    SECTION_NAME,
)


@pytest.mark.django_db
class TestAddTorchCompetitorFeedback(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.tenant = self.create_tenant(name="Torch THC", slug="torch-thc")
        self.tenant.checkin_code = "TH-2HRV3D"
        self.tenant.save(update_fields=["checkin_code"])
        self.system_user = self.get_system_user()
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.section = recap_models.RecapSection.objects.create(
            name=SECTION_NAME,
            tenant=self.tenant,
            order=1,
            created_by=self.system_user,
        )
        self.ftype, _ = recap_models.CustomRecapFieldType.objects.get_or_create(
            name="longtext",
            defaults={"created_by": self.system_user},
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch THC-Retail Sampling",
            tenant=self.tenant,
            event_type=etype,
            created_by=self.system_user,
        )
        recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=self.section,
            custom_field_type=self.ftype,
            name="Consumer Feedback",
            required=True,
            order=1,
            created_by=self.system_user,
        )

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("add_torch_competitor_feedback", stdout=out)
        assert FIELD_NAME in out.getvalue()
        assert "DRY-RUN" in out.getvalue()
        assert not recap_models.CustomField.objects.filter(
            custom_recap_template=self.template, name=FIELD_NAME
        ).exists()

    def test_apply_adds_field_and_is_idempotent(self):
        out = StringIO()
        call_command("add_torch_competitor_feedback", apply=True, stdout=out)
        field = recap_models.CustomField.objects.get(
            custom_recap_template=self.template, name=FIELD_NAME
        )
        assert field.recap_section_id == self.section.id
        assert field.custom_field_type_id == self.ftype.id
        assert field.required is False
        assert field.order == FIELD_ORDER
        assert "added=1" in out.getvalue()

        out2 = StringIO()
        call_command("add_torch_competitor_feedback", apply=True, stdout=out2)
        assert (
            recap_models.CustomField.objects.filter(
                custom_recap_template=self.template, name=FIELD_NAME
            ).count()
            == 1
        )
        assert "already_present=1" in out2.getvalue()
