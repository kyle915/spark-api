"""End-to-end coverage for the ``import_demo_recaps`` bulk loader against the
REAL committed Girl Beer H-E-B make-good dataset.

Because prod's DB isn't reachable locally, this test IS the correctness proof
for the command Kyle runs in prod: it builds a Girl-Beer-shaped tenant +
template + a representative subset of fields, points the command at the actual
``data/girlbeer_heb_makegood_2026_07_04.json``, and asserts:

  * dry-run writes nothing and reports the column→field mapping (+ unmapped);
  * --apply creates one standalone (request-less) approved Event + one
    CustomRecap per row, credited to ``external_ba_name`` with the template
    attached, values written only for mapped fields, numbers cleaned, and the
    multiselect stored as a JSON array of matched options;
  * total_engagements is populated from the configured engagements field;
  * re-running --apply is idempotent (creates nothing new).
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from events import models as event_models
from events.tests.base import EventsGraphQLTestCase
from recaps import models as recap_models

DATASET = "girlbeer_heb_makegood_2026_07_04"


def _json_result(out: str) -> dict:
    for line in out.splitlines():
        if line.startswith("JSON_RESULT: "):
            return json.loads(line[len("JSON_RESULT: ") :])
    raise AssertionError("no JSON_RESULT line in command output:\n" + out)


@pytest.mark.django_db(transaction=True)
class TestImportDemoRecaps(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        # The command resolves created_by by this email.
        self.owner = self.create_user(
            username="kyle",
            email="kyle@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.tenant = self.create_tenant(name="Girl Beer", slug="girl-beer")

        self.event_type = event_models.EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.system_user
        )
        self.approved = event_models.EventStatus.objects.create(
            name="Approved", slug="approved", tenant=self.tenant,
            created_by=self.system_user,
        )
        # create_request needs an approved RequestStatus for the tenant.
        event_models.RequestStatus.objects.create(
            name="Approved", slug="approved", tenant=self.tenant,
            created_by=self.system_user,
        )
        event_models.TimeZone.objects.create(
            name="Central Daylight Time", code="CDT", offset=-300,
            created_by=self.system_user,
        )
        event_models.State.objects.create(
            name="Texas", code="TX", created_by=self.system_user
        )

        self.num_type = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        self.text_type = recap_models.CustomRecapFieldType.objects.create(
            name="text", created_by=self.system_user
        )
        self.multi_type = recap_models.CustomRecapFieldType.objects.create(
            name="multiselect", created_by=self.system_user
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Girl Beer Demo Recap", event_type=self.event_type,
            tenant=self.tenant, created_by=self.system_user,
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Details", tenant=self.tenant, created_by=self.system_user
        )
        # A representative SUBSET of the 42 columns — enough to exercise every
        # value type; the rest land in "unmapped" and are skipped by design.
        self._field("Total Samples Given Out", self.num_type)
        self._field(
            "Number of Customers Engaged (talked to or sampled product)",
            self.num_type,
        )
        self._field("# of PURPLE Variety Packs sold", self.num_type)
        self._field("Store Associate Spoken To", self.text_type)
        self._field("Positive Feedback From Customers", self.text_type)
        self._field(
            "What flavors were available to taste?",
            self.multi_type,
            options=[
                "Pineapple Yuzu", "Grapefruit Guava", "Tangerine", "Peach",
                "Blueberry Lavender", "Strawberry Watermelon",
                "Purple Variety Pack", "Red Variety Pack",
            ],
        )

    def _field(self, name, ftype, options=None):
        return recap_models.CustomField.objects.create(
            name=name, custom_recap_template=self.template, custom_field_type=ftype,
            recap_section=self.section, created_by=self.system_user,
            options=options or [],
        )

    def _run(self, *, apply: bool):
        out = StringIO()
        args = ["import_demo_recaps", "--dataset", DATASET]
        if apply:
            args.append("--apply")
        call_command(*args, stdout=out)
        return _json_result(out.getvalue())

    # --------------------------------------------------------------- tests
    def test_dry_run_writes_nothing_and_maps_columns(self):
        res = self._run(apply=False)
        assert res["mode"] == "DRY_RUN"
        assert res["rows_in"] == 7
        assert res["would_create_events"] == 7
        assert res["would_create_requests"] == 7
        assert res["events_created"] == 0
        assert event_models.Event.objects.count() == 0
        assert recap_models.CustomRecap.objects.count() == 0
        assert event_models.Request.objects.count() == 0

        # Our subset is mapped; a known column we didn't model is unmapped.
        mapped = res["columns_mapped"]
        assert "Total Samples Given Out" in mapped
        assert "What flavors were available to taste?" in mapped
        assert "Tangerine 6-packs Sold" in res["columns_unmapped"]
        assert res["tenant"]["slug"] == "girl-beer"
        assert res["state"] == "TX"
        assert res["external_ba_name"] == "Internal"
        assert res["create_request"] is True

    def test_apply_creates_events_recaps_requests_and_values(self):
        res = self._run(apply=True)
        assert res["events_created"] == 7
        assert res["recaps_created"] == 7
        assert res["requests_created"] == 7
        assert event_models.Event.objects.count() == 7
        assert recap_models.CustomRecap.objects.count() == 7
        assert event_models.Request.objects.count() == 7

        # Every event now has a linked (approved, already-scheduled) Request →
        # it lands on the Master Tracker + mirror.
        assert event_models.Event.objects.filter(request__isnull=True).count() == 0
        for req in event_models.Request.objects.all():
            assert req.status.slug == "approved"
            assert req.scheduling_status == "already_scheduled"
            assert req.tenant_id == self.tenant.id
            assert req.state.code == "TX"

        # All approved, template attached, TX, Central.
        for ev in event_models.Event.objects.all():
            assert ev.status.slug == "approved"
            assert ev.custom_recap_template_id == self.template.id
            assert ev.state.code == "TX"
            assert ev.date.year == 2026 and ev.date.month == 7 and ev.date.day == 4

        # Every recap credited "Internal", approved, no ambassador, template set.
        for r in recap_models.CustomRecap.objects.all():
            assert r.external_ba_name == "Internal"
            assert r.ambassador_id is None
            assert r.approved is True
            assert r.custom_recap_template_id == self.template.id
            assert r.tenant_id == self.tenant.id

        # Pick the Kyle, TX row (row 1): purple packs 3, engaged 34, flavors 4.
        kyle = event_models.Event.objects.filter(address__startswith="5401").first()
        assert kyle is not None
        recap = recap_models.CustomRecap.objects.get(event=kyle)
        assert recap.total_engagements == 34  # from the engagements field

        vals = {
            v.custom_field.name: v.value
            for v in recap_models.CustomFieldValue.objects.filter(custom_recap=recap)
        }
        # Only mapped fields were written (6 modeled fields, all present on row 1).
        assert vals["# of PURPLE Variety Packs sold"] == "3"  # number cleaned
        assert vals["Total Samples Given Out"] == "30"
        assert vals["Store Associate Spoken To"] == "Beer & Wine Lead"
        # Multiselect stored as a JSON array of matched options.
        flavors = json.loads(vals["What flavors were available to taste?"])
        assert isinstance(flavors, list)
        assert "Pineapple Yuzu" in flavors and "Peach" in flavors
        # No value row for an unmapped column.
        assert "Tangerine 6-packs Sold" not in vals

    def test_apply_is_idempotent(self):
        first = self._run(apply=True)
        assert first["events_created"] == 7
        assert first["requests_created"] == 7
        again = self._run(apply=True)
        assert again["events_created"] == 0
        assert again["recaps_created"] == 0
        assert again["requests_created"] == 0
        # Nothing duplicated on the second pass.
        assert event_models.Event.objects.count() == 7
        assert recap_models.CustomRecap.objects.count() == 7
        assert event_models.Request.objects.count() == 7

    def test_second_pass_adds_only_requests_to_recap_only_events(self):
        """The real prod scenario: events + recaps already exist (from the
        first, request-less load); a re-run adds ONLY the Requests."""
        # Simulate the prior state: run once WITHOUT create_request by stripping
        # the linked requests after a full apply.
        self._run(apply=True)
        event_models.Event.objects.update(request=None)
        event_models.Request.objects.all().delete()
        assert event_models.Event.objects.filter(request__isnull=True).count() == 7

        again = self._run(apply=True)
        assert again["events_created"] == 0
        assert again["recaps_created"] == 0
        assert again["requests_created"] == 7
        assert event_models.Event.objects.filter(request__isnull=True).count() == 0
