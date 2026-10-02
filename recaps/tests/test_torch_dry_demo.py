"""Torch dry-demo fields: template seed + historical tagging."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.management.commands.add_torch_dry_demo_fields import (
    DRY_DEMO,
    PEOPLE_ENGAGED,
    SAMPLED_FIELD_NAME,
    SAMPLED_PLACEHOLDER,
    SECTION_NAME,
)
from recaps.types import _is_dry_demo_from_fields, _people_engaged_from_fields


def test_dry_demo_parsers():
    assert _is_dry_demo_from_fields([("Dry demo? (no product tasted)", "Yes")])
    assert _is_dry_demo_from_fields([("Dry demo?", " yes ")])
    assert not _is_dry_demo_from_fields([("Dry demo?", "No")])
    assert not _is_dry_demo_from_fields([("Dry demo?", "")])
    assert not _is_dry_demo_from_fields([("General notes", "Yes")])
    assert _people_engaged_from_fields([("People engaged", "42")]) == 42
    assert _people_engaged_from_fields([("People engaged", "")]) is None
    assert _people_engaged_from_fields([("How many people engaged with X?", "9")]) is None


@pytest.mark.django_db
class TestTorchDryDemo(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.tenant = self.create_tenant(name="Torch THC", slug="torch-thc")
        self.tenant.request_url_name = "keee-torch-thc"
        self.tenant.checkin_code = "TH-2HRV3D"
        self.tenant.save(update_fields=["request_url_name", "checkin_code"])
        self.sys = self.get_system_user()
        self.etype = self.create_event_type("Retail Sampling", self.tenant)
        self.section = recap_models.RecapSection.objects.create(
            name=SECTION_NAME, tenant=self.tenant, order=1, created_by=self.sys
        )
        self.notes_section = recap_models.RecapSection.objects.create(
            name="Feedback & Account Notes", tenant=self.tenant, order=2, created_by=self.sys
        )
        self.int_type = recap_models.CustomRecapFieldType.objects.create(
            name="FormInputInteger", created_by=self.sys
        )
        self.select_type = recap_models.CustomRecapFieldType.objects.create(
            name="select", created_by=self.sys
        )
        self.text_type = recap_models.CustomRecapFieldType.objects.create(
            name="FormTextarea", created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch THC-Retail Sampling",
            tenant=self.tenant,
            event_type=self.etype,
            created_by=self.sys,
        )
        self.sampled = self._field(SAMPLED_FIELD_NAME, self.int_type, order=1, required=True)
        self.first_time = self._field("First Time Consumers", self.int_type, order=2)
        self.cans = self._field(
            "How many single cans did consumers purchase?", self.int_type, order=3
        )
        self.notes = self._field(
            "Account Feedback", self.text_type, order=1, section=self.notes_section
        )

    def _field(self, name, ftype, *, order, required=False, section=None):
        return recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=section or self.section,
            custom_field_type=ftype,
            name=name,
            required=required,
            order=order,
            created_by=self.sys,
        )

    def _section_names(self):
        return list(
            recap_models.CustomField.objects.filter(
                custom_recap_template=self.template, recap_section=self.section
            )
            .order_by("order", "id")
            .values_list("name", flat=True)
        )

    def _recap(self, values):
        event = event_models.Event.objects.create(
            name="walk-in",
            tenant=self.tenant,
            address="1 Main St",
            event_type=self.etype,
            created_by=self.sys,
            updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="Total Wine #1",
            approved=True,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.sys,
        )
        for field, value in values.items():
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=field, value=value, created_by=self.sys
            )
        return recap

    def _values(self, recap):
        return {
            v.custom_field.name: v.value
            for v in recap_models.CustomFieldValue.objects.filter(
                custom_recap=recap
            ).select_related("custom_field")
        }

    # ---------------------------------------------------------- template

    def test_add_fields_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("add_torch_dry_demo_fields", stdout=out)
        assert "DRY-RUN" in out.getvalue()
        assert DRY_DEMO["name"] in out.getvalue()
        assert self._section_names() == [
            SAMPLED_FIELD_NAME,
            "First Time Consumers",
            "How many single cans did consumers purchase?",
        ]
        self.sampled.refresh_from_db()
        assert self.sampled.placeholder == ""

    def test_add_fields_apply_places_around_sampled_and_is_idempotent(self):
        call_command("add_torch_dry_demo_fields", apply=True, stdout=StringIO())
        assert self._section_names() == [
            DRY_DEMO["name"],
            SAMPLED_FIELD_NAME,
            PEOPLE_ENGAGED["name"],
            "First Time Consumers",
            "How many single cans did consumers purchase?",
        ]
        dry = recap_models.CustomField.objects.get(
            custom_recap_template=self.template, name=DRY_DEMO["name"]
        )
        people = recap_models.CustomField.objects.get(
            custom_recap_template=self.template, name=PEOPLE_ENGAGED["name"]
        )
        assert dry.custom_field_type_id == self.select_type.id
        assert dry.options == ["No", "Yes"]
        assert dry.required is True
        assert people.custom_field_type_id == self.int_type.id
        assert people.required is False
        self.sampled.refresh_from_db()
        assert self.sampled.required is True
        assert self.sampled.placeholder == SAMPLED_PLACEHOLDER
        self.notes.refresh_from_db()
        assert self.notes.order == 1

        out = StringIO()
        call_command("add_torch_dry_demo_fields", apply=True, stdout=out)
        assert "added=0" in out.getvalue()
        assert (
            recap_models.CustomField.objects.filter(
                custom_recap_template=self.template,
                name__in=[DRY_DEMO["name"], PEOPLE_ENGAGED["name"]],
            ).count()
            == 2
        )

    def test_walkup_form_check_shows_new_fields(self):
        event_models.Event.objects.create(
            name="walk-in",
            tenant=self.tenant,
            address="1 Main St",
            event_type=self.etype,
            created_by=self.sys,
            updated_by=self.sys,
        )
        out = StringIO()
        call_command("add_torch_dry_demo_fields", apply=True, stdout=out)
        check = out.getvalue().split("Walk-up form check")[1]
        assert f"template [{self.template.id}]" in check
        assert "'Dry demo? (no product tasted)' options=['No', 'Yes']" in check
        assert "'People engaged'" in check
        assert SAMPLED_PLACEHOLDER in check

    def test_add_fields_resolves_request_url_name(self):
        out = StringIO()
        call_command("add_torch_dry_demo_fields", tenant="keee-torch-thc", stdout=out)
        assert f"[{self.tenant.id}]" in out.getvalue()

    def test_add_fields_unknown_tenant(self):
        with pytest.raises(CommandError):
            call_command("add_torch_dry_demo_fields", tenant="nope", stdout=StringIO())

    # ----------------------------------------------------------- tagging

    def test_tag_requires_fields_first(self):
        recap = self._recap({self.sampled: "20", self.notes: "I did a dry sampling."})
        out = StringIO()
        call_command("tag_torch_dry_demos", ids=str(recap.id), apply=True, stdout=out)
        assert "lacks the dry-demo fields" in out.getvalue()
        assert DRY_DEMO["name"] not in self._values(recap)

    def test_tag_dry_run_then_apply(self):
        call_command("add_torch_dry_demo_fields", apply=True, stdout=StringIO())
        dry = self._recap(
            {
                self.sampled: "30",
                self.cans: "25",
                self.notes: "For this gig I didn't have samples, they bought on my word.",
            }
        )
        live = self._recap({self.sampled: "50", self.notes: "Great tasting day."})
        zero = self._recap({self.sampled: "0", self.notes: "This was a dry tasting."})
        ids = f"{dry.id},{live.id},{zero.id}"

        out = StringIO()
        call_command("tag_torch_dry_demos", ids=ids, stdout=out)
        log = out.getvalue()
        assert "DRY-RUN" in log
        assert f"#{dry.id}: WOULD TAG" in log
        assert f"#{live.id}: SKIP — no no-tasting phrase" in log
        assert DRY_DEMO["name"] not in self._values(dry)

        out = StringIO()
        call_command("tag_torch_dry_demos", ids=ids, apply=True, stdout=out)
        log = out.getvalue()
        assert "before  : dry_demo='(blank)' people_engaged='(blank)' consumers_sampled=30" in log
        assert "after   : dry_demo='Yes' people_engaged='30' consumers_sampled=30" in log

        dv = self._values(dry)
        assert dv[DRY_DEMO["name"]] == "Yes"
        assert dv[PEOPLE_ENGAGED["name"]] == "30"
        assert dv[SAMPLED_FIELD_NAME] == "30"
        assert dv["How many single cans did consumers purchase?"] == "25"
        assert DRY_DEMO["name"] not in self._values(live)
        zv = self._values(zero)
        assert zv[DRY_DEMO["name"]] == "Yes"
        assert PEOPLE_ENGAGED["name"] not in zv
        assert zv[SAMPLED_FIELD_NAME] == "0"

        out = StringIO()
        call_command("tag_torch_dry_demos", ids=str(dry.id), apply=True, stdout=out)
        assert "already answered" in out.getvalue()
        assert (
            recap_models.CustomFieldValue.objects.filter(
                custom_recap=dry, custom_field__name=DRY_DEMO["name"]
            ).count()
            == 1
        )

    def test_tag_skips_protected_and_other_tenants(self):
        call_command("add_torch_dry_demo_fields", apply=True, stdout=StringIO())
        other_tenant = self.create_tenant(name="Other", slug="other-co")
        other_tpl = recap_models.CustomRecapTemplate.objects.create(
            name="Other",
            tenant=other_tenant,
            event_type=self.create_event_type("Retail Sampling", other_tenant),
            created_by=self.sys,
        )
        event = event_models.Event.objects.create(
            name="x", tenant=other_tenant, address="x", created_by=self.sys, updated_by=self.sys
        )
        foreign = recap_models.CustomRecap.objects.create(
            name="x",
            event=event,
            tenant=other_tenant,
            custom_recap_template=other_tpl,
            created_by=self.sys,
        )
        out = StringIO()
        call_command(
            "tag_torch_dry_demos", ids=f"{foreign.id},1577", apply=True, stdout=out
        )
        log = out.getvalue()
        assert f"#{foreign.id}: SKIP — belongs to tenant {other_tenant.id}" in log
        assert "#1577: SKIP — protected" in log
        assert "Tagged 0" in log

    def test_tag_rejects_bad_ids(self):
        with pytest.raises(CommandError):
            call_command("tag_torch_dry_demos", ids="12,abc", stdout=StringIO())
