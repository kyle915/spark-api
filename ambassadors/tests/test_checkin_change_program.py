"""A BA fixes the program they picked on a multi-program standing link.

Torch, Nov 2026: a BA picked "G7 Guerilla Sampling" for a Retail Sampling
shift and the page kept handing her the guerilla recap form. Staff had to
change the event by hand. ``POST checkin/<code>/change-program`` lets the BA
switch it herself — only on a walk-in event her own check-in opened that
nobody else is on, and never after her recap is filed.
"""
from __future__ import annotations

import json
import uuid

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import (
    AmbassadorEvent,
    Attendance,
    CheckinProgramChange,
    WalkupRecapDraft,
)
from ambassadors.program_change import (
    build_field_map,
    carry_over_draft,
    renamed_event,
)
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestCheckinChangeProgram(AmbassadorsGraphQLTestCase):
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
        self.tenant = self.create_tenant(name=f"Torch Test {uid}")
        self.tenant.checkin_code = f"TH{uid.upper()}"
        self.guerilla = self.create_event_type("G7 Guerilla Sampling", self.tenant)
        self.retail = self.create_event_type("Retail Sampling", self.tenant)
        self.tenant.checkin_event_type = self.retail
        self.tenant.checkin_photo_buckets = {
            "G7 Guerilla Sampling": [{"name": "Crowd"}, {"name": "Setup"}],
            "Retail Sampling": [{"name": "Setup"}, {"name": "Shelf"}],
        }
        self.tenant.save(
            update_fields=["checkin_code", "checkin_event_type", "checkin_photo_buckets"]
        )
        self.tenant.checkin_event_types.set([self.retail, self.guerilla])

        self.cat = {
            name: FileRecapCategory.objects.create(
                name=name,
                tenant=self.tenant,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            for name in ("Crowd", "Setup", "Shelf")
        }

        types = {
            n: CustomRecapFieldType.objects.get_or_create(
                name=n, defaults={"created_by": self.system_user}
            )[0]
            for n in ("number", "text", "select")
        }
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Details", order=0, created_by=self.system_user
        )

        def template(name, etype, fields):
            tpl = CustomRecapTemplate.objects.create(
                tenant=self.tenant, name=name, event_type=etype, created_by=self.system_user
            )
            out = {}
            for order, (fname, ftype, options) in enumerate(fields):
                out[fname] = CustomField.objects.create(
                    custom_recap_template=tpl,
                    recap_section=section,
                    name=fname,
                    custom_field_type=types[ftype],
                    options=options,
                    order=order,
                    created_by=self.system_user,
                )
            return tpl, out

        self.g_tpl, self.g_fields = template(
            "Guerilla recap",
            self.guerilla,
            [
                ("Consumers sampled", "number", []),
                ("Venue notes", "text", []),
                ("Crowd vibe", "select", ["Hot", "Cold"]),
            ],
        )
        self.r_tpl, self.r_fields = template(
            "Retail recap",
            self.retail,
            [
                ("Consumers Sampled:", "number", []),
                ("Store manager name", "text", []),
                ("Crowd vibe", "select", ["Hot", "Warm"]),
            ],
        )
        self.on = dj_tz.localdate()
        self.addr = "9703 Barker Cypress Rd, Cypress, TX 77433"
        self.http = DjangoClient()

    # -- helpers -------------------------------------------------------------

    def _post(self, name, body):
        return self.http.post(
            reverse(name, kwargs={"code": self.tenant.checkin_code}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def _identify(self, etype, phone="5055550101", **extra):
        body = {
            "firstName": "Test",
            "lastName": "Ambassador",
            "phone": phone,
            "eventDate": self.on.isoformat(),
            "address": self.addr,
            "eventTypeId": str(etype.id),
            "shiftIsToday": True,
            "planEventId": "none",
        }
        body.update(extra)
        res = self._post("events.public_checkin_identify", body)
        assert res.status_code == 200, res.content
        payload = res.json()
        event = Event.objects.get(uuid=payload["event"]["uuid"])
        amb_event = AmbassadorEvent.objects.get(uuid=payload["ambassadorEventUuid"])
        return payload["sessionToken"], event, amb_event.ambassador

    def _change(self, token, etype, **extra):
        body = {"session": token, "eventTypeId": str(etype.id)}
        body.update(extra)
        return self._post("events.public_checkin_change_program", body)

    def _clock_in(self, event, ambassador):
        amb_event = AmbassadorEvent.objects.get(event=event, ambassador=ambassador)
        checkin_web.record_attendance(
            amb_event=amb_event,
            kind="clock_in",
            coordinates=[29.9, -95.6],
            actor=ambassador.user,
        )

    # -- allowed -------------------------------------------------------------

    def test_switch_changes_the_event_in_place_and_keeps_the_clock(self):
        token, event, amb = self._identify(self.guerilla)
        self._clock_in(event, amb)
        booking = AmbassadorEvent.objects.get(event=event, ambassador=amb)
        punches = list(
            Attendance.objects.filter(event=event, ambassador=amb).values_list(
                "id", "clock_time", "coordinates"
            )
        )
        assert event.name.endswith(" · G7 Guerilla Sampling")

        res = self._change(token, self.retail, storeName="Kroger")
        assert res.status_code == 200, res.content
        body = res.json()

        event.refresh_from_db()
        assert event.event_type_id == self.retail.id
        assert event.name.endswith(" (Kroger) · Retail Sampling")
        assert "G7 Guerilla" not in event.name
        # Same event, same booking, same punches.
        assert body["event"]["uuid"] == str(event.uuid)
        assert AmbassadorEvent.objects.get(pk=booking.pk).is_approved == booking.is_approved
        assert AmbassadorEvent.objects.filter(event=event).count() == 1
        assert (
            list(
                Attendance.objects.filter(event=event, ambassador=amb).values_list(
                    "id", "clock_time", "coordinates"
                )
            )
            == punches
        )
        # The page gets the new form and the new photo buckets.
        assert body["template"]["id"] == str(self.r_tpl.id)
        assert [b["name"] for b in body["photoBuckets"]] == ["Setup", "Shelf"]
        assert body["event"]["eventType"]["name"] == "Retail Sampling"
        assert body["programChange"]["from"] == "G7 Guerilla Sampling"
        assert body["programChange"]["to"] == "Retail Sampling"
        # A retail event names its store.
        assert event.retailer_id is not None

        audit = CheckinProgramChange.objects.get(event=event)
        assert audit.ambassador_id == amb.id
        assert audit.from_event_type_id == self.guerilla.id
        assert audit.to_event_type_id == self.retail.id
        assert audit.from_template_id == self.g_tpl.id
        assert audit.to_template_id == self.r_tpl.id
        assert audit.store_name == "Kroger"
        assert audit.new_event_name == event.name

    def test_retail_needs_a_store_name_in_the_same_step(self):
        token, event, _amb = self._identify(self.guerilla)
        before = event.name
        res = self._change(token, self.retail)
        assert res.status_code == 400
        assert res.json()["error"] == "store_name_required"
        event.refresh_from_db()
        assert event.event_type_id == self.guerilla.id
        assert event.name == before
        assert not CheckinProgramChange.objects.exists()

    def test_switch_away_from_retail_needs_no_store(self):
        token, event, _amb = self._identify(self.retail, storeName="HEB")
        res = self._change(token, self.guerilla)
        assert res.status_code == 200, res.content
        event.refresh_from_db()
        assert event.event_type_id == self.guerilla.id
        assert event.name.endswith(" · G7 Guerilla Sampling")
        assert "Retail Sampling" not in event.name

    def test_context_offers_the_switch(self):
        token, event, _amb = self._identify(self.guerilla)
        res = self.http.get(
            reverse("events.public_checkin_context", kwargs={"code": self.tenant.checkin_code}),
            HTTP_X_CHECKIN_SESSION=token,
        )
        body = res.json()
        assert {p["name"] for p in body["eventTypes"]} == {
            "G7 Guerilla Sampling",
            "Retail Sampling",
        }
        assert body["session"]["programChange"]["allowed"] is True

    def test_draft_answers_and_photos_follow_the_ba(self):
        token, event, amb = self._identify(self.guerilla)
        blob = f"recap_files/checkin/{event.uuid}/a.jpg"
        draft = {
            "v": 1,
            "savedAt": 1,
            "values": {
                str(self.g_fields["Consumers sampled"].id): "40",
                str(self.g_fields["Venue notes"].id): "Busy park",
                str(self.g_fields["Crowd vibe"].id): "Cold",
            },
            "samples": {},
            "engagements": "40",
            "photos": [
                {"id": "p1", "blobName": blob, "category": str(self.cat["Crowd"].id)},
                {"id": "p2", "blobName": None, "category": str(self.cat["Setup"].id)},
            ],
        }
        res = self._change(token, self.retail, storeName="Kroger", draft=draft)
        assert res.status_code == 200, res.content
        body = res.json()
        carried = body["session"]["recapDraft"]
        # Same name (case / colon aside) and type → carried to the new field id.
        assert carried["values"] == {str(self.r_fields["Consumers Sampled:"].id): "40"}
        assert carried["engagements"] == "40"
        # Photos are never dropped: a bucket the new program lacks → generic.
        by_id = {p["id"]: p for p in carried["photos"]}
        assert by_id["p1"]["category"] is None
        assert by_id["p1"]["blobName"] == blob
        assert by_id["p2"]["category"] == str(self.cat["Setup"].id)
        assert body["programChange"]["carried"] == 1
        assert set(body["programChange"]["dropped"]) == {"Venue notes", "Crowd vibe"}
        assert body["programChange"]["photosMoved"] == 1
        row = WalkupRecapDraft.objects.get(event=event, ambassador=amb, scope="shift")
        assert row.payload["values"] == carried["values"]

    def test_clock_out_stub_moves_to_the_new_form(self):
        from recaps.models import CustomRecap

        token, event, amb = self._identify(self.guerilla)
        stub = CustomRecap.objects.create(
            name=event.name,
            event=event,
            ambassador=amb,
            tenant=self.tenant,
            custom_recap_template=self.g_tpl,
            created_by=amb.user,
            updated_by=amb.user,
            approved=False,
        )
        res = self._change(token, self.retail, storeName="Kroger")
        assert res.status_code == 200, res.content
        stub.refresh_from_db()
        assert stub.custom_recap_template_id == self.r_tpl.id
        event.refresh_from_db()
        assert checkin_web.resolve_template_for_event(event).id == self.r_tpl.id

    def test_stale_old_form_submit_is_refused(self):
        token, event, amb = self._identify(self.guerilla)
        assert self._change(token, self.retail, storeName="Kroger").status_code == 200
        res = self._post(
            "events.public_checkin_recap",
            {
                "session": token,
                "templateId": str(self.g_tpl.id),
                "fieldValues": [
                    {"customFieldId": str(self.g_fields["Venue notes"].id), "value": "x"}
                ],
                "files": [],
                "idempotencyKey": "k-old",
            },
        )
        assert res.status_code == 422
        assert res.json()["error"] == "program_changed"
        assert not checkin_web.has_recap(ambassador_id=amb.id, event_id=event.id)
        draft = self._post(
            "events.public_checkin_recap_draft",
            {"session": token, "templateId": str(self.g_tpl.id), "draft": {"values": {}}},
        )
        assert draft.status_code == 422

    # -- refused -------------------------------------------------------------

    def test_refused_when_another_ba_is_on_the_event(self):
        token, event, _amb = self._identify(self.guerilla)
        # A second BA at the same place, same program, same day joins it.
        _t2, event2, _amb2 = self._identify(self.guerilla, phone="5055550102", firstName="Other")
        assert event2.id == event.id
        res = self._change(token, self.retail, storeName="Kroger")
        assert res.status_code == 409
        assert res.json()["error"] == "shared_event"
        assert "lead" in res.json()["message"]
        event.refresh_from_db()
        assert event.event_type_id == self.guerilla.id
        assert not CheckinProgramChange.objects.exists()

    def test_refused_on_a_scheduled_event(self):
        token, event, amb = self._identify(self.guerilla)
        request = self.create_request(
            name="Scheduled demo",
            date=self.on,
            address=self.addr,
            client=None,
            distributor=None,
            retailer=None,
            request_type=self.create_request_type("Retail Sampling", self.tenant),
            tenant=self.tenant,
        )
        Event.objects.filter(pk=event.pk).update(request=request)
        res = self._change(token, self.retail, storeName="Kroger")
        assert res.status_code == 409
        assert res.json()["error"] == "shared_event"

    def test_refused_when_someone_else_opened_the_event(self):
        token, event, _amb = self._identify(self.guerilla)
        Event.objects.filter(pk=event.pk).update(created_by=self.system_user)
        res = self._change(token, self.retail, storeName="Kroger")
        assert res.status_code == 409
        assert res.json()["error"] == "shared_event"

    def test_refused_after_the_recap_is_filed(self):
        from recaps.models import CustomRecap

        token, event, amb = self._identify(self.guerilla)
        CustomRecap.objects.create(
            name=event.name,
            event=event,
            ambassador=amb,
            tenant=self.tenant,
            custom_recap_template=self.g_tpl,
            submitted_at=dj_tz.now(),
            created_by=amb.user,
            updated_by=amb.user,
        )
        res = self._change(token, self.retail, storeName="Kroger")
        assert res.status_code == 409
        assert res.json()["error"] == "recap_filed"
        event.refresh_from_db()
        assert event.event_type_id == self.guerilla.id

    def test_refused_on_a_time_sheet_only_tenant(self):
        token, event, amb = self._identify(self.guerilla)
        self.tenant.checkin_timesheet_only = True
        self.tenant.save(update_fields=["checkin_timesheet_only"])
        res = self._change(token, self.retail, storeName="Kroger")
        assert res.status_code == 409
        assert res.json()["error"] == "timesheet_only"

    def test_a_program_outside_the_link_is_refused(self):
        token, _event, _amb = self._identify(self.guerilla)
        other = self.create_event_type("Event Activation", self.tenant)  # not offered
        res = self._change(token, other)
        assert res.status_code == 400
        assert res.json()["error"] == "unknown_program"
        res = self._change(token, self.guerilla)
        assert res.status_code == 400
        assert res.json()["error"] == "same_program"

    def test_bad_session_is_refused(self):
        res = self._change("nope", self.retail, storeName="Kroger")
        assert res.status_code == 401

    # -- staff ---------------------------------------------------------------

    def test_walkup_row_shows_the_change(self):
        from ambassadors.walkup import _build_walkup_row

        token, event, amb = self._identify(self.guerilla)
        assert self._change(token, self.retail, storeName="Kroger").status_code == 200
        row = _build_walkup_row(
            AmbassadorEvent.objects.select_related("event__tenant", "ambassador__user").get(
                event=event, ambassador=amb
            )
        )
        assert row.program_changed_from == "G7 Guerilla Sampling"
        assert row.program_changed_to == "Retail Sampling"
        assert row.program_changed_at


# -- pure helpers --------------------------------------------------------------


def test_renamed_event_swaps_the_suffix():
    assert (
        renamed_event(
            "11/20/2026 - 1 Main St · G7 Guerilla Sampling",
            old_program="G7 Guerilla Sampling",
            new_program="Retail Sampling",
            store_name="Kroger",
        )
        == "11/20/2026 - 1 Main St (Kroger) · Retail Sampling"
    )
    # Staff renamed it: keep their text, add the new program once.
    assert (
        renamed_event("Park day", old_program="X", new_program="Retail Sampling")
        == "Park day · Retail Sampling"
    )
    # Store already in the title is not repeated.
    assert (
        renamed_event(
            "1 Main St (Kroger) · A", old_program="A", new_program="B", store_name="kroger"
        )
        == "1 Main St (Kroger) · B"
    )


def test_field_map_needs_same_name_and_type_and_one_match():
    old = [
        {"id": "1", "name": "Notes", "type": "text"},
        {"id": "2", "name": "Count", "type": "number"},
        {"id": "3", "name": "Dup", "type": "text"},
    ]
    new = [
        {"id": "10", "name": "notes:", "type": "text"},
        {"id": "20", "name": "Count", "type": "text"},
        {"id": "30", "name": "Dup", "type": "text"},
        {"id": "31", "name": "dup", "type": "text"},
    ]
    assert build_field_map(old, new) == {"1": "10"}


def test_same_template_keeps_everything():
    draft = {"values": {"1": "a"}, "photos": [{"id": "p", "category": "9"}], "coBrands": {"x": 1}}
    out, summary = carry_over_draft(
        draft, old_fields=[], new_fields=[], new_bucket_ids=set(),
        new_product_ids=set(), same_template=True,
    )
    assert out == draft
    assert summary["carried"] == 1
