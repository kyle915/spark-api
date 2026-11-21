"""``GET checkin/<code>/preview`` — the recap a BA can read before clocking in.

BAs on one bar of signal clock in late and only then discover the recap wants
a shelf photo and a consumer count they never kept. The preview shows the
form from the link alone. It must:

* resolve the SAME template the real walk-in check-in would (program pick →
  tenant + event type match → sole-template fallback);
* expose form structure only — no event, store, BA or internal ids;
* never identify, clock, or create an event / booking / ambassador;
* refuse dead codes and time-sheet-only brands, and rate-limit like the other
  public check-in endpoints.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse

from ambassadors.models import Ambassador, AmbassadorEvent, Attendance
from ambassadors.recap_preview import template_for_program
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestCheckinRecapPreview(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            FileRecapCategory,
            RecapSection,
        )

        cache.clear()
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Preview Brand {uid}")
        self.tenant.checkin_code = f"PV{uid.upper()}"
        self.tenant.checkin_recap_code = f"PVA{uid.upper()}"
        self.guerilla = self.create_event_type("G7 Guerilla Sampling", self.tenant)
        self.retail = self.create_event_type("Retail Sampling", self.tenant)
        self.tenant.checkin_event_type = self.retail
        self.tenant.checkin_photo_buckets = {
            "G7 Guerilla Sampling": [{"name": "Crowd", "min": 3}],
            "Retail Sampling": [
                {"name": "Table Setup", "min": 1, "helper": "Whole table in frame"},
                {"name": "Shelf", "min": 0},
            ],
        }
        self.tenant.save(
            update_fields=[
                "checkin_code",
                "checkin_recap_code",
                "checkin_event_type",
                "checkin_photo_buckets",
            ]
        )
        self.tenant.checkin_event_types.set([self.retail, self.guerilla])
        for name in ("Crowd", "Table Setup", "Shelf"):
            FileRecapCategory.objects.create(
                name=name,
                tenant=self.tenant,
                created_by=self.system_user,
                updated_by=self.system_user,
            )

        types = {
            n: CustomRecapFieldType.objects.get_or_create(
                name=n, defaults={"created_by": self.system_user}
            )[0]
            for n in ("number", "text", "select")
        }
        details = RecapSection.objects.create(
            tenant=self.tenant, name="Details", order=0, created_by=self.system_user
        )
        emails = RecapSection.objects.create(
            tenant=self.tenant,
            name="Email Data Collection",
            order=1,
            created_by=self.system_user,
        )

        def template(name, etype, fields):
            tpl = CustomRecapTemplate.objects.create(
                tenant=self.tenant, name=name, event_type=etype, created_by=self.system_user
            )
            for order, (fname, ftype, options, required, section) in enumerate(fields):
                CustomField.objects.create(
                    custom_recap_template=tpl,
                    recap_section=section,
                    name=fname,
                    custom_field_type=types[ftype],
                    options=options,
                    required=required,
                    order=order,
                    created_by=self.system_user,
                )
            return tpl

        self.g_tpl = template(
            "Guerilla recap",
            self.guerilla,
            [("Crowd vibe", "select", ["Hot", "Cold"], True, details)],
        )
        self.r_tpl = template(
            "Retail recap",
            self.retail,
            [
                ("Consumers sampled", "number", [], True, details),
                ("Store traffic", "select", ["Busy", "Slow"], True, details),
                ("Anything else?", "text", [], False, details),
                ("Emails collected", "number", [], False, emails),
            ],
        )
        self.http = DjangoClient()

    def _get(self, code, **params):
        return self.http.get(
            reverse("events.public_checkin_recap_preview", kwargs={"code": code}),
            params,
        )

    # -- template resolution -------------------------------------------------

    def test_multi_program_link_asks_for_the_program_first(self):
        res = self._get(self.tenant.checkin_code)
        assert res.status_code == 200
        body = res.json()
        assert body["needsProgram"] is True
        assert body["template"] is None
        assert {p["id"] for p in body["programs"]} == {
            str(self.retail.id),
            str(self.guerilla.id),
        }

    def test_picked_program_gets_its_own_form_and_photo_buckets(self):
        body = self._get(self.tenant.checkin_code, program=self.retail.id).json()
        assert body["needsProgram"] is False
        assert body["program"]["id"] == str(self.retail.id)
        assert body["template"]["name"] == "Retail recap"
        details = body["template"]["sections"][0]
        assert details["name"] == "Details"
        fields = {f["name"]: f for f in details["fields"]}
        assert fields["Consumers sampled"]["type"] == "number"
        assert fields["Consumers sampled"]["required"] is True
        assert fields["Store traffic"]["options"] == ["Busy", "Slow"]
        assert fields["Anything else?"]["required"] is False
        assert body["photoBuckets"] == [
            {"name": "Table Setup", "helper": "Whole table in frame", "min": 1},
            {"name": "Shelf", "helper": "", "min": 0},
        ]
        assert body["minPhotos"] == 1

        g = self._get(self.tenant.checkin_code, program=self.guerilla.id).json()
        assert g["template"]["name"] == "Guerilla recap"
        assert [b["name"] for b in g["photoBuckets"]] == ["Crowd"]

    def test_matches_what_a_real_walkin_event_would_get(self):
        """Same resolution as ``resolve_template_for_event`` on a fresh event."""
        from ambassadors import checkin_web

        for et in (self.retail, self.guerilla):
            ev = Event(tenant=self.tenant, event_type=et)
            ev.id = 10**9  # no recap filed for this id
            assert template_for_program(self.tenant, et) == (
                checkin_web.resolve_template_for_event(ev)
            )

    def test_single_program_link_previews_from_the_start(self):
        self.tenant.checkin_event_types.set([self.retail])
        body = self._get(self.tenant.checkin_code).json()
        assert body["needsProgram"] is False
        assert body["programs"] == []
        assert body["template"]["name"] == "Retail recap"

    def test_another_brands_program_id_is_ignored(self):
        other = self.create_tenant(name=f"Other {uuid.uuid4().hex[:6]}")
        foreign = self.create_event_type("Retail Sampling", other)
        body = self._get(self.tenant.checkin_code, program=foreign.id).json()
        assert body["needsProgram"] is True
        assert body["template"] is None

    def test_agency_link_hides_email_collection_and_stays_on_default(self):
        body = self._get(self.tenant.checkin_recap_code, program=self.guerilla.id).json()
        assert body["programs"] == []
        assert body["template"]["name"] == "Retail recap"
        names = [s["name"] for s in body["template"]["sections"]]
        assert "Email Data Collection" not in names

    # -- privacy -------------------------------------------------------------

    def test_event_code_exposes_form_structure_only(self):
        Event.objects.create(
            tenant=self.tenant,
            name="Secret Store #42",
            address="1 Private Way, Nowhere",
            notes="Gate code 9911",
            event_type=self.retail,
            walkup_code="PVEVT001",
            created_by=self.system_user,
        )
        res = self._get("PVEVT001")
        assert res.status_code == 200
        raw = res.content.decode()
        for secret in ("Secret Store", "Private Way", "Gate code", "9911"):
            assert secret not in raw
        body = res.json()
        assert set(body) == {
            "brand",
            "programs",
            "programPickerTitle",
            "program",
            "needsProgram",
            "template",
            "photoBuckets",
            "minPhotos",
        }
        assert body["template"]["name"] == "Retail recap"
        assert "id" not in body["template"]
        for bucket in body["photoBuckets"]:
            assert set(bucket) == {"name", "helper", "min"}
        for section in body["template"]["sections"]:
            assert set(section) == {"name", "fields"}

    # -- no side effects -----------------------------------------------------

    def test_opening_the_preview_writes_nothing(self):
        before = (
            Event.objects.count(),
            AmbassadorEvent.objects.count(),
            Ambassador.objects.count(),
            Attendance.objects.count(),
            get_user_model().objects.count(),
        )
        for params in ({}, {"program": self.retail.id}, {"program": self.guerilla.id}):
            assert self._get(self.tenant.checkin_code, **params).status_code == 200
        after = (
            Event.objects.count(),
            AmbassadorEvent.objects.count(),
            Ambassador.objects.count(),
            Attendance.objects.count(),
            get_user_model().objects.count(),
        )
        assert before == after

    # -- refusals ------------------------------------------------------------

    def test_unknown_code_is_refused(self):
        res = self._get("NOPE-NOT-A-CODE")
        assert res.status_code == 404
        assert res.json()["error"] == "not_found"

    def test_time_sheet_only_brand_has_no_preview(self):
        self.tenant.checkin_timesheet_only = True
        self.tenant.save(update_fields=["checkin_timesheet_only"])
        res = self._get(self.tenant.checkin_code)
        assert res.status_code == 404
        assert res.json()["error"] == "timesheet_only"
        assert "template" not in json.loads(res.content)

    def test_rate_limited_per_network(self):
        with patch("events.checkin_views.PREVIEW_IP_LIMIT", 2):
            assert self._get(self.tenant.checkin_code).status_code == 200
            assert self._get(self.tenant.checkin_code).status_code == 200
            res = self._get(self.tenant.checkin_code)
        assert res.status_code == 429
        assert res.json()["error"] == "rate_limited"
