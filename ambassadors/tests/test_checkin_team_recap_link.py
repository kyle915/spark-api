"""Client-team recap link (`Tenant.checkin_team_code`): Torch's own staff file
recaps with no clock — name → event type → the matching recap form.

Load-bearing behaviours:

* the team code resolves without shadowing the BA clock or agency codes
* no clock: clock / sampling-stop refuse it, identify needs no phone
* unlike the agency link it keeps the program picker, the full resource list
  and Email Data Collection
* recaps count in totals (never exclude_from_aggregates, even when the
  tenant's agency link opts out), carry the source label, and stay
  approved=False until an admin approves
* approved-recap routing is the normal Torch routing by execution type
* the setup command is dry-run by default and never re-mints
"""
from __future__ import annotations

import datetime as _dt
import uuid
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client as DjangoClient, override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import checkin_views
from events.models import Event
from events.torch_retail_routing import (
    torch_field_marketing_recap_emails,
    torch_retail_recap_emails,
)
from recaps.aggregates import exclude_non_aggregate_recaps
from recaps.models import (
    CustomField,
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileType,
    RecapSection,
)
from recaps.mutation_parts.notify import _collect_recap_approved_recipients
from recaps.tenant_overview import tenant_conversion_kpis

LABEL = "Submitted by Torch team"
SAMPLED = "Total number of consumers sampled"
CANS = "How many single cans did consumers purchase?"


@pytest.mark.django_db(transaction=True)
class TestTeamRecapLink(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.sys = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:6].upper()
        self.tenant = self.create_tenant(name=f"Torch Team {uid}")
        self.tenant.slug = "torch-thc"
        self.clock_code = f"TH-C{uid}"
        self.agency_code = f"TH-A{uid}"
        self.team_code = f"TH-T{uid}"
        self.tenant.checkin_code = self.clock_code
        self.tenant.checkin_recap_code = self.agency_code
        self.tenant.checkin_team_code = self.team_code
        self.tenant.checkin_team_label = LABEL
        self.tenant.checkin_team_title = "Torch THC · Client team recaps (no clock)"
        self.tenant.save()

        self.types = {
            name: self.create_event_type(name, self.tenant)
            for name in (
                "Retail Sampling",
                "Event Activation",
                "Guerilla Activation",
                "Product Seeding",
            )
        }
        self.tenant.checkin_event_type = self.types["Retail Sampling"]
        self.tenant.save(update_fields=["checkin_event_type"])
        self.tenant.checkin_event_types.set(self.types.values())
        self.templates = {
            name: CustomRecapTemplate.objects.create(
                tenant=self.tenant, name=f"Torch {name}", event_type=et, created_by=self.sys
            )
            for name, et in self.types.items()
        }
        FileType.objects.get_or_create(name="image", defaults={"created_by": self.sys})
        num, _ = CustomRecapFieldType.objects.get_or_create(
            name="Number", defaults={"created_by": self.sys}
        )
        section = RecapSection.objects.create(name="S", tenant=self.tenant, created_by=self.sys)
        self.fields = {
            name: CustomField.objects.create(
                name=name,
                custom_recap_template=self.templates["Retail Sampling"],
                custom_field_type=num,
                recap_section=section,
                created_by=self.sys,
            )
            for name in (SAMPLED, CANS)
        }
        self.http = DjangoClient()

    # -- helpers -----------------------------------------------------------

    def _url(self, name: str, code: str | None = None) -> str:
        return reverse(f"events.public_checkin_{name}", kwargs={"code": code or self.team_code})

    def _identify(self, program: str = "Retail Sampling", **extra):
        body = {
            "firstName": "Riley",
            "lastName": "Torchteam",
            "eventDate": dj_tz.localdate().isoformat(),
            "eventTypeId": str(self.types[program].id),
            "address": "1648 NW Chipman Road, LEE'S SUMMIT, MO 64081",
            "storeName": "Total Wine & More (Lee's Summit)",
        }
        body.update(extra)
        return self.http.post(self._url("identify"), data=body, content_type="application/json")

    def _file(self, identified, field_values=None):
        body = identified.json()
        event = Event.objects.get(uuid=body["event"]["uuid"])
        res = self.http.post(
            self._url("recap"),
            data={
                "session": body["sessionToken"],
                "fieldValues": field_values or [],
                "files": [{"blobName": f"recap_files/checkin/{event.uuid}/{uuid.uuid4().hex}.jpg"}],
                "storeName": "Total Wine & More (Lee's Summit)",
                "storeNumber": "1234",
            },
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        return event

    # -- resolve / context -------------------------------------------------

    def test_team_code_resolves_without_shadowing_other_codes(self):
        kind, target = checkin_web.resolve_checkin_target(self.team_code.lower())
        assert (kind, target.id) == ("tenant", self.tenant.id)
        assert checkin_web.is_team_recap_code(self.team_code, target)
        assert checkin_web.is_recap_only_code(self.team_code, target)
        assert not checkin_web.is_third_party_code(self.team_code, target)
        assert checkin_web.team_recap_source_label(self.team_code, target) == LABEL

        assert checkin_web.is_third_party_code(self.agency_code, target)
        assert not checkin_web.is_team_recap_code(self.agency_code, target)
        assert checkin_web.team_recap_source_label(self.agency_code, target) == ""
        assert not checkin_web.is_recap_only_code(self.clock_code, target)

    def test_context_keeps_picker_and_full_resources(self):
        base = "https://client.igniteproductions.co/training/torch"
        self.tenant.checkin_resources = [
            {"label": "BA Sampling Guide", "kind": "pdf", "url": f"{base}/ba-sampling-guide.pdf",
             "hideOnRecapOnly": True},
            {"label": "Product Sales Sheets", "kind": "pdf", "url": f"{base}/product-sales-sheets.pdf"},
        ]
        self.tenant.save(update_fields=["checkin_resources"])

        team = self.http.get(self._url("context")).json()
        assert team["recapOnly"] is True
        assert team["sourceLabel"] == LABEL
        assert team["recentLocations"] == []
        assert {t["name"] for t in team["eventTypes"]} == set(self.types)
        assert [r["label"] for r in team["resources"]] == ["BA Sampling Guide", "Product Sales Sheets"]

        agency = self.http.get(self._url("context", self.agency_code)).json()
        assert agency["eventTypes"] == []
        assert agency["sourceLabel"] == ""
        assert [r["label"] for r in agency["resources"]] == ["Product Sales Sheets"]

    def test_team_payload_keeps_email_data_collection(self):
        template = {"sections": [{"name": "Consumer Engagement"}, {"name": "Email Data Collection"}]}
        team = checkin_views._stamp_recap_only({"template": template}, self.team_code, self.tenant)
        assert [s["name"] for s in team["template"]["sections"]] == [
            "Consumer Engagement",
            "Email Data Collection",
        ]
        agency = checkin_views._stamp_recap_only({"template": template}, self.agency_code, self.tenant)
        assert [s["name"] for s in agency["template"]["sections"]] == ["Consumer Engagement"]

    # -- identify ----------------------------------------------------------

    def test_retail_needs_typed_store_name_and_no_phone(self):
        missing = self._identify(storeName="")
        assert missing.status_code == 400
        assert "store name" in missing.json()["message"].lower()

        res = self._identify()
        assert res.status_code == 200, res.content
        event = Event.objects.get(uuid=res.json()["event"]["uuid"])
        assert event.event_type_id == self.types["Retail Sampling"].id

    @pytest.mark.parametrize("program", ["Event Activation", "Guerilla Activation"])
    def test_venue_programs_need_only_an_address(self, program):
        res = self._identify(program, storeName="", address="2001 Grand Blvd, Kansas City, MO 64108")
        assert res.status_code == 200, res.content
        event = Event.objects.get(uuid=res.json()["event"]["uuid"])
        assert event.event_type_id == self.types[program].id

        missing = self._identify(program, storeName="", address="")
        assert missing.status_code == 400
        assert "venue address" in missing.json()["message"].lower()

    def test_seeding_skips_location_and_past_date_finds_or_creates(self):
        past = dj_tz.localdate() - _dt.timedelta(days=6)
        res = self._identify("Product Seeding", storeName="", address="", eventDate=past.isoformat())
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["event"]["date"].startswith(past.isoformat())
        event = Event.objects.get(uuid=body["event"]["uuid"])
        assert event.event_type_id == self.types["Product Seeding"].id

    def test_clock_and_stops_refuse_the_team_link(self):
        token = self._identify().json()["sessionToken"]
        clock = self.http.post(
            self._url("clock"), data={"session": token, "kind": "in"}, content_type="application/json"
        )
        assert clock.status_code == 403
        assert clock.json()["error"] == "recap_only"
        stop = self.http.post(
            self._url("sampling_stop"), data={"session": token, "name": "Aisle 4"},
            content_type="application/json",
        )
        assert stop.status_code == 403

    # -- submit ------------------------------------------------------------

    def test_recaps_count_in_totals_carry_label_and_need_approval(self):
        # The agency link opting out of totals must not drag the team link along.
        self.tenant.checkin_recap_excludes_aggregates = True
        self.tenant.save(update_fields=["checkin_recap_excludes_aggregates"])

        values = [
            {"customFieldId": self.fields[SAMPLED].id, "value": "40"},
            {"customFieldId": self.fields[CANS].id, "value": "8"},
        ]
        first = self._identify()
        event = self._file(first, values)
        self._file(first, values)
        recaps = list(CustomRecap.objects.filter(event=event).order_by("id"))
        assert len(recaps) == 2  # a second filing never overwrites the first
        for recap in recaps:
            assert recap.source_label == LABEL
            assert recap.is_third_party is False
            assert recap.exclude_from_aggregates is False
            assert recap.approved is False
            assert recap.store_mapping_status == ""
            assert recap.ambassador.user.first_name == "Riley"
        counted = exclude_non_aggregate_recaps(CustomRecap.objects.all(), "event__")
        assert {r.id for r in recaps} <= set(counted.values_list("id", flat=True))

        today = dj_tz.localdate()
        assert tenant_conversion_kpis(self.tenant.id, start=today, end=today)["sold"] == 0
        CustomRecap.objects.filter(event=event).update(approved=True)
        k = tenant_conversion_kpis(self.tenant.id, start=today, end=today)
        assert (k["sold"], k["engagements"]) == (16, 80)

    def test_agency_and_clock_recaps_get_no_source_label(self):
        res = self.http.post(
            self._url("identify", self.agency_code),
            data={
                "firstName": "Alex", "lastName": "Agency",
                "eventDate": dj_tz.localdate().isoformat(),
                "address": "1 Main St, Kansas City, MO 64108", "storeName": "Hy-Vee",
            },
            content_type="application/json",
        )
        body = res.json()
        event = Event.objects.get(uuid=body["event"]["uuid"])
        filed = self.http.post(
            self._url("recap", self.agency_code),
            data={"session": body["sessionToken"], "fieldValues": [],
                  "files": [{"blobName": f"recap_files/checkin/{event.uuid}/a.jpg"}],
                  "storeName": "Hy-Vee", "storeNumber": "12"},
            content_type="application/json",
        )
        assert filed.status_code == 200, filed.content
        recap = CustomRecap.objects.get(event=event)
        assert recap.is_third_party is True
        assert recap.source_label == ""

    def test_routing_follows_torch_execution_type(self):
        retail_event = self._file(self._identify())
        retail = CustomRecap.objects.get(event=retail_event)
        emails = {e for e, _ in _collect_recap_approved_recipients(retail)[0]}
        assert set(torch_retail_recap_emails(None)) <= emails

        event_event = self._file(
            self._identify("Event Activation", storeName="", address="2001 Grand Blvd, Kansas City, MO 64108")
        )
        activation = CustomRecap.objects.get(event=event_event)
        emails = {e for e, _ in _collect_recap_approved_recipients(activation)[0]}
        assert emails == set(torch_field_marketing_recap_emails())


@pytest.mark.django_db(transaction=True)
class TestSetupTeamRecapLinkCommand(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Torch THC")
        self.tenant.slug = "torch-thc"
        self.tenant.request_url_name = "keee-torch-thc"
        self.tenant.checkin_code = "TH-2HRV3D"
        self.tenant.checkin_recap_code = "TH-AGENCY"
        self.tenant.save()

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("setup_team_recap_link", stdout=out)
        assert "DRY-RUN" in out.getvalue()
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_team_code is None
        assert self.tenant.checkin_team_label == ""

    def test_apply_mints_once_and_never_remints(self):
        call_command("setup_team_recap_link", apply=True, stdout=StringIO())
        self.tenant.refresh_from_db()
        code = self.tenant.checkin_team_code
        assert code and code.startswith("TH-") and len(code) == 9
        assert code not in ("TH-2HRV3D", "TH-AGENCY")
        assert self.tenant.checkin_team_label == LABEL
        assert self.tenant.checkin_team_title == "Torch THC · Client team recaps (no clock)"

        out = StringIO()
        call_command("setup_team_recap_link", tenant="keee-torch-thc", apply=True, stdout=out)
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_team_code == code
        assert "KEEPING" in out.getvalue() and "no changes" in out.getvalue()
        assert self.tenant.checkin_code == "TH-2HRV3D"
        assert self.tenant.checkin_recap_code == "TH-AGENCY"

    @override_settings(INTERNAL_CRON_SECRET="s3cret")
    def test_cron_view_is_secret_gated_and_dry_run(self):
        http = DjangoClient()
        url = "/internal/cron/setup-team-recap-link"
        assert http.post(url, {"tenant": "torch-thc"}).status_code in (401, 403)
        res = http.post(url, {"tenant": "torch-thc"}, HTTP_X_CRON_SECRET="s3cret")
        assert res.status_code == 200, res.content
        assert res.json()["applied"] is False
        assert "DRY-RUN" in res.json()["log"]
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_team_code is None
