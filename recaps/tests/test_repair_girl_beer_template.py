"""
Coverage for the `repair_girl_beer_template` management command.

The command brings an EXISTING (drifted) Girl Beer CustomRecapTemplate up
to the canonical seed spec, non-destructively: it RENAMES drifted labels
in place (so historical CustomFieldValues ride along) and ADDS any missing
field — and must NEVER delete a field or value, and must be a no-op on a
clean template. These tests assert exactly that against a deliberately
under-built template.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from tenants.management.commands.onboard_girl_beer import SECTIONS


SPEC_FIELD_COUNT = sum(len(fields) for _name, fields in SECTIONS)
DRIFTED_FOOT_TRAFFIC = "Foot Traffic (people walking by per hour)"
SPEC_FOOT_TRAFFIC = "Foot Traffic (number of people walking by demo table per hour)"


@pytest.mark.django_db(transaction=True)
class TestRepairGirlBeerTemplate(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.owner = self.create_user(
            username="repair-owner",
            email="repair-owner@test.com",
            role=self.roles["spark_admin"],
        )
        self.tenant = self.create_tenant(name="Girl Beer", slug="girl-beer")
        self.event_type = self.create_event_type(
            name="Retail Sampling", tenant=self.tenant
        )
        for ft_name in ("text", "number", "image"):
            recap_models.CustomRecapFieldType.objects.get_or_create(
                name=ft_name, defaults={"created_by": self.system_user}
            )
        self.number_ft = recap_models.CustomRecapFieldType.objects.get(name="number")

        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Girl Beer · Retail Sampling Recap",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
            layout={"sections": ["Customer Interaction"], "version": 1},
        )
        # A drifted Foot-Traffic field carrying a historical value.
        self.ci_section = recap_models.RecapSection.objects.create(
            tenant=self.tenant, name="Customer Interaction",
            created_by=self.system_user,
        )
        self.drifted_field = recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=self.ci_section,
            name=DRIFTED_FOOT_TRAFFIC,
            custom_field_type=self.number_ft,
            created_by=self.system_user,
        )
        # One already-correct field that must be left untouched.
        self.bought_section = recap_models.RecapSection.objects.create(
            tenant=self.tenant, name="Demographics — Bought",
            created_by=self.system_user,
        )
        self.kept_field = recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=self.bought_section,
            name="Men who bought (21-29)",
            custom_field_type=self.number_ft,
            created_by=self.system_user,
        )

        # Historical recap value bound to the drifted field — must survive.
        self.event = event_models.Event.objects.create(
            name="Demo @ Store", tenant=self.tenant, address="123 Main St",
            created_by=self.system_user,
        )
        self.recap = recap_models.CustomRecap.objects.create(
            name="historical", event=self.event, tenant=self.tenant,
            custom_recap_template=self.template, created_by=self.system_user,
        )
        self.value = recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap, custom_field=self.drifted_field,
            value="42", created_by=self.system_user,
        )

    def _run(self) -> str:
        out = StringIO()
        call_command(
            "repair_girl_beer_template", "--owner-email", self.owner.email,
            stdout=out,
        )
        return out.getvalue()

    def test_repair_adds_missing_fields_and_renames_drift(self):
        log = self._run()

        # Template now matches the full spec field count.
        count = recap_models.CustomField.objects.filter(
            custom_recap_template=self.template
        ).count()
        assert count == SPEC_FIELD_COUNT, log

        # Drifted Foot-Traffic field renamed IN PLACE (same row id).
        self.drifted_field.refresh_from_db()
        assert self.drifted_field.name == SPEC_FOOT_TRAFFIC

        # The whole 'Total sampled' group + the (Total) rows now exist.
        for name in (
            "Total sampled (21-29)", "Total sampled (Total)",
            "Men who bought (Total)", "Women who sampled (Total)",
            "Number of Customers Engaged (talked to or sampled product)",
            "Anything that could make future demos better?",
        ):
            assert recap_models.CustomField.objects.filter(
                custom_recap_template=self.template, name=name
            ).exists(), f"missing after repair: {name!r}"

        assert "rename" in log and "+ add" in log

    def test_rename_preserves_historical_value(self):
        self._run()
        # The value row still points at the same (renamed) field and keeps
        # its data — nothing was dropped or re-keyed.
        self.value.refresh_from_db()
        assert self.value.value == "42"
        assert self.value.custom_field_id == self.drifted_field.id

    def test_non_destructive_never_deletes(self):
        before_ids = set(
            recap_models.CustomField.objects.filter(
                custom_recap_template=self.template
            ).values_list("id", flat=True)
        )
        self._run()
        after_ids = set(
            recap_models.CustomField.objects.filter(
                custom_recap_template=self.template
            ).values_list("id", flat=True)
        )
        # Every pre-existing field id still exists (renames keep the row).
        assert before_ids.issubset(after_ids)
        # The kept correct field is untouched.
        self.kept_field.refresh_from_db()
        assert self.kept_field.name == "Men who bought (21-29)"

    def test_idempotent_second_run_is_a_noop(self):
        self._run()
        count_after_first = recap_models.CustomField.objects.filter(
            custom_recap_template=self.template
        ).count()
        log2 = self._run()
        count_after_second = recap_models.CustomField.objects.filter(
            custom_recap_template=self.template
        ).count()
        assert count_after_second == count_after_first
        assert "0 rename(s), 0 field(s) added" in log2, log2

    def test_ensures_longtext_field_type_and_layout_sections(self):
        self._run()
        # 'longtext' wasn't created in setup; the command must register it.
        assert recap_models.CustomRecapFieldType.objects.filter(
            name="longtext"
        ).exists()
        self.template.refresh_from_db()
        spec_sections = {name for name, _ in SECTIONS}
        assert spec_sections.issubset(
            set(self.template.layout.get("sections", []))
        )
