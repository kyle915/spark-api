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
        self.text_ft = recap_models.CustomRecapFieldType.objects.get(name="text")
        self.image_ft = recap_models.CustomRecapFieldType.objects.get(name="image")

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

        # A field that exists with the spec LABEL already but the WRONG
        # type: "Product purchase receipt (image)" created as TEXT. This
        # is the exact prod drift — repair must RETYPE it to IMAGE
        # in place (keeping the row + its values). It lives in the
        # "Staff & Demo Experience" section per the spec.
        self.staff_section = recap_models.RecapSection.objects.create(
            tenant=self.tenant, name="Staff & Demo Experience",
            created_by=self.system_user,
        )
        self.receipt_field = recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=self.staff_section,
            name="Product purchase receipt (image)",
            custom_field_type=self.text_ft,  # WRONG — spec is image
            created_by=self.system_user,
        )
        # A stale non-blob value that would render as a broken <img>
        # once the field is IMAGE — repair must CLEAR it (keep the row).
        self.stale_receipt_value = recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap, custom_field=self.receipt_field,
            value="see attached receipt", created_by=self.system_user,
        )
        # A second recap whose receipt value IS a plausible blob path —
        # repair must LEAVE it intact.
        self.recap2 = recap_models.CustomRecap.objects.create(
            name="historical-2", event=self.event, tenant=self.tenant,
            custom_recap_template=self.template, created_by=self.system_user,
        )
        self.blob_receipt_value = recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap2, custom_field=self.receipt_field,
            value="recaps/receipts/abc-123/1700000000-receipt.jpg",
            created_by=self.system_user,
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
        assert "0 rename(s)" in log2 and "0 field(s) added" in log2, log2

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

    # ─── Retype (TEXT → IMAGE) coverage ──────────────────────────────

    def test_retypes_text_field_to_image_in_place(self):
        """The mis-typed "Product purchase receipt (image)" field (TEXT)
        is flipped to IMAGE on the SAME row — not deleted/re-added."""
        before_id = self.receipt_field.id
        log = self._run()

        self.receipt_field.refresh_from_db()
        assert self.receipt_field.id == before_id, "row must be reused"
        assert self.receipt_field.custom_field_type.name == "image", log
        assert "retype" in log

        # Still exactly one receipt field on the template (no duplicate).
        assert (
            recap_models.CustomField.objects.filter(
                custom_recap_template=self.template,
                name="Product purchase receipt (image)",
            ).count()
            == 1
        )

    def test_retype_clears_stale_non_blob_value_keeps_row(self):
        """A TEXT→IMAGE retype blanks a non-blob value (so nothing
        renders as a broken image) WITHOUT deleting the value row."""
        before_value_id = self.stale_receipt_value.id
        self._run()

        self.stale_receipt_value.refresh_from_db()
        # Row preserved, value blanked.
        assert self.stale_receipt_value.id == before_value_id
        assert self.stale_receipt_value.value == ""
        # The row still points at the same field/recap.
        assert self.stale_receipt_value.custom_field_id == self.receipt_field.id

    def test_retype_keeps_plausible_blob_value(self):
        """A value that already looks like a GCS blob path is a real
        image and must survive the TEXT→IMAGE retype untouched."""
        self._run()
        self.blob_receipt_value.refresh_from_db()
        assert (
            self.blob_receipt_value.value
            == "recaps/receipts/abc-123/1700000000-receipt.jpg"
        )

    def test_retype_is_idempotent(self):
        """Second run leaves the (now IMAGE) field alone — 0 retypes."""
        self._run()
        self.receipt_field.refresh_from_db()
        assert self.receipt_field.custom_field_type.name == "image"

        log2 = self._run()
        # No further retypes / clears on a clean template.
        assert "0 retype(s) (0 stale value(s) cleared)" in log2, log2
        self.receipt_field.refresh_from_db()
        assert self.receipt_field.custom_field_type.name == "image"

    def test_retype_never_drops_the_field_or_its_values(self):
        """Whole-row + value survival guarantee across the retype."""
        field_ids_before = set(
            recap_models.CustomField.objects.filter(
                custom_recap_template=self.template
            ).values_list("id", flat=True)
        )
        value_ids_before = set(
            recap_models.CustomFieldValue.objects.filter(
                custom_field=self.receipt_field
            ).values_list("id", flat=True)
        )
        self._run()
        field_ids_after = set(
            recap_models.CustomField.objects.filter(
                custom_recap_template=self.template
            ).values_list("id", flat=True)
        )
        value_ids_after = set(
            recap_models.CustomFieldValue.objects.filter(
                custom_field=self.receipt_field
            ).values_list("id", flat=True)
        )
        # Every field id survived (retype keeps the row).
        assert field_ids_before.issubset(field_ids_after)
        # Both receipt value rows survived (one blanked, one kept).
        assert value_ids_before == value_ids_after

    def test_dry_run_reports_retype_without_writing(self):
        """--dry-run reports the retype but doesn't touch the DB."""
        out = StringIO()
        call_command(
            "repair_girl_beer_template", "--owner-email", self.owner.email,
            "--dry-run", stdout=out,
        )
        log = out.getvalue()
        assert "retype" in log
        # Nothing written: field still TEXT, stale value still present.
        self.receipt_field.refresh_from_db()
        assert self.receipt_field.custom_field_type.name == "text"
        self.stale_receipt_value.refresh_from_db()
        assert self.stale_receipt_value.value == "see attached receipt"
