"""Standing-check-in recaps: more than one per day, and past dates.

Feel Free (and other standing / FF-* links) share one event per market per
day. The write path used to upsert that (event, BA) recap, so a second
filing overwrote the first, and a BA who forgot Friday's recap could not
land back on Friday's shift after the weekend.

Per-event codes (Liquid Death booked activations) stay one-per-event.
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event
from recaps.models import (
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileType,
)


@pytest.mark.django_db(transaction=True)
class TestStandingRecapRepeat(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.roles = self.setup_default_roles()
        self.actor = self.get_system_user()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Feel Free {uid}")
        self.tenant.checkin_code = f"FF-{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])

        ba_user = self.create_user(
            username=f"ba-ff-{uid}@test.com",
            email=f"ba-ff-{uid}@test.com",
            role=self.roles["ambassador"],
            first_name="Rocio",
            last_name="D",
        )
        self.ba = self.create_ambassador(ba_user)

        etype = self.create_event_type("Field Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="FF Sampling",
            event_type=etype,
            created_by=self.actor,
        )
        FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.actor}
        )
        CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )
        self.http = DjangoClient()

    def _event(self, *, name, address, on_date):
        return Event.objects.create(
            tenant=self.tenant,
            name=name,
            address=address,
            date=checkin_web._event_date_utc(on_date),
            event_type=self.template.event_type,
            created_by=self.actor,
        )

    def _punch(self, event, kind, when=None):
        source, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            ambassador=self.ba,
            event=event,
            source=source,
            clock_time=when or dj_tz.now(),
        )

    def _blob(self, event, name: str) -> str:
        return f"recap_files/checkin/{event.uuid}/{name}.jpg"

    def _submit(self, event, files, *, force_new=False):
        return checkin_web.submit_checkin_recap(
            event=event,
            ambassador=self.ba,
            template=self.template,
            field_values=[],
            files=files,
            total_engagements=None,
            force_new=force_new,
        )

    def test_second_recap_same_day_creates_another_row(self):
        today = dj_tz.localdate()
        event = self._event(name="Austin", address="Austin, TX", on_date=today)
        first = self._submit(event, [{"blobName": self._blob(event, "a")}])
        second = self._submit(
            event,
            [{"blobName": self._blob(event, "b")}],
            force_new=True,
        )
        assert second.id != first.id
        assert (
            CustomRecap.objects.filter(event=event, ambassador=self.ba).count() == 2
        )

    def test_edit_without_force_new_still_updates_the_same_row(self):
        today = dj_tz.localdate()
        event = self._event(name="Austin", address="Austin, TX", on_date=today)
        first = self._submit(event, [{"blobName": self._blob(event, "a")}])
        again = self._submit(event, [{"blobName": self._blob(event, "b")}])
        assert again.id == first.id
        assert (
            CustomRecap.objects.filter(event=event, ambassador=self.ba).count() == 1
        )

    def test_force_new_reuses_an_empty_clock_out_stub(self):
        """Don't leave a blank stub sitting next to a real recap."""
        today = dj_tz.localdate()
        event = self._event(name="Austin", address="Austin, TX", on_date=today)
        stub = CustomRecap.objects.create(
            name="stub",
            event=event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.actor,
            updated_by=self.actor,
        )
        filed = self._submit(
            event,
            [{"blobName": self._blob(event, "a")}],
            force_new=True,
        )
        assert filed.id == stub.id
        assert (
            CustomRecap.objects.filter(event=event, ambassador=self.ba).count() == 1
        )

    def test_recap_for_yesterday_is_accepted(self):
        yesterday = dj_tz.localdate() - _dt.timedelta(days=1)
        event = self._event(
            name="Friday Austin", address="Austin, TX", on_date=yesterday
        )
        recap = self._submit(event, [{"blobName": self._blob(event, "fri")}])
        assert recap.event_id == event.id
        assert recap.submitted_at is not None

    def test_existing_shift_ties_a_past_date_to_the_clocked_event(self):
        friday = dj_tz.localdate() - _dt.timedelta(days=2)
        event = self._event(name="Friday Austin", address="Austin, TX", on_date=friday)
        punched = checkin_web._event_date_utc(friday).replace(hour=18)
        self._punch(event, "clock_in", punched)
        self._punch(event, "clock_out", punched + _dt.timedelta(hours=4))

        found = checkin_web.existing_shift_event_for(
            ambassador=self.ba,
            tenant=self.tenant,
            on_date=friday,
            address="Austin, TX",
        )
        assert found is not None and found.id == event.id

    def test_unfiled_shifts_lists_a_clocked_day_without_a_recap(self):
        friday = dj_tz.localdate() - _dt.timedelta(days=2)
        event = self._event(name="Friday Austin", address="Austin, TX", on_date=friday)
        self._punch(event, "clock_in", checkin_web._event_date_utc(friday))
        self._punch(
            event,
            "clock_out",
            checkin_web._event_date_utc(friday) + _dt.timedelta(hours=5),
        )

        shifts = checkin_web.unfiled_shifts_for(
            ambassador=self.ba, tenant=self.tenant
        )
        assert any(s["eventDate"] == friday.isoformat() for s in shifts)

        self._submit(event, [{"blobName": self._blob(event, "fri")}])
        after = checkin_web.unfiled_shifts_for(
            ambassador=self.ba, tenant=self.tenant
        )
        assert not any(s["eventDate"] == friday.isoformat() for s in after)

    def test_identify_past_date_attaches_to_the_existing_shift(self):
        friday = dj_tz.localdate() - _dt.timedelta(days=2)
        event = self._event(name="Friday Austin", address="Austin, TX", on_date=friday)
        # Identify keys walk-up stubs on phone. Punch the stub that identify
        # will resolve, not the fixture Spark BA.
        stub, _ = checkin_web.get_or_create_checkin_ambassador(
            first_name="Rocio", last_name="D", phone="5550100123", email=None
        )
        source, _ = Source.objects.get_or_create(name="clock_in")
        Attendance.objects.create(
            ambassador=stub,
            event=event,
            source=source,
            clock_time=checkin_web._event_date_utc(friday),
        )
        source_out, _ = Source.objects.get_or_create(name="clock_out")
        Attendance.objects.create(
            ambassador=stub,
            event=event,
            source=source_out,
            clock_time=checkin_web._event_date_utc(friday) + _dt.timedelta(hours=5),
        )

        res = self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={
                "firstName": "Rocio",
                "lastName": "D",
                "phone": "5550100123",
                "eventDate": friday.isoformat(),
                "address": "Austin, TX",
            },
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["event"]["uuid"] == str(event.uuid)
        assert body["session"]["clock"]["state"] == "clocked_out"

    def test_identify_past_date_without_a_shift_mints_the_event(self):
        """Walk-up Start check-in is self-serve — no prior punch required.

        KKC-QC9Y58 hit this as a 400 ("No check-in found for that date")
        when a BA typed a Boston address for a day they had never clocked.
        Standing links mint/find-or-create from location + date instead.
        """
        friday = dj_tz.localdate() - _dt.timedelta(days=2)
        res = self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={
                "firstName": "Rocio",
                "lastName": "D",
                "phone": "5550100999",
                "eventDate": friday.isoformat(),
                "address": "Austin, TX",
            },
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body.get("sessionToken")
        assert body["event"]["date"].startswith(friday.isoformat())
        assert "austin" in (body["event"]["address"] or "").lower()

    def test_identify_today_without_a_shift_mints_from_typed_address(self):
        """KKC Start check-in: typed store, today, nobody clocked yet."""
        today = dj_tz.localdate()
        res = self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={
                "firstName": "Francisco",
                "lastName": "Calva Villalta",
                "phone": "5550100888",
                "eventDate": today.isoformat(),
                "address": "4 Jersey Street, Boston, MA 02115",
            },
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body.get("sessionToken")
        assert body["event"]["date"].startswith(today.isoformat())
        assert "jersey" in (body["event"]["address"] or "").lower()
        assert Event.objects.filter(
            tenant=self.tenant,
            address="4 Jersey Street, Boston, MA 02115",
        ).exists()

    def test_event_code_ignores_force_new(self):
        """Liquid Death booked activations stay one recap per event."""
        today = dj_tz.localdate()
        event = self._event(name="LD Activation", address="1 Main", on_date=today)
        event.walkup_code = f"LD-{uuid.uuid4().hex[:6].upper()}"
        event.save(update_fields=["walkup_code"])
        token = make_checkin_session_token(event.id, self.ba.id)

        first = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": event.walkup_code},
            ),
            data={
                "session": token,
                "forceNew": True,
                "fieldValues": [],
                "files": [{"blobName": self._blob(event, "a")}],
            },
            content_type="application/json",
        )
        second = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": event.walkup_code},
            ),
            data={
                "session": token,
                "forceNew": True,
                "fieldValues": [],
                "files": [{"blobName": self._blob(event, "b")}],
            },
            content_type="application/json",
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert (
            CustomRecap.objects.filter(event=event, ambassador=self.ba).count() == 1
        )

    def test_standing_endpoint_force_new_files_a_second_recap(self):
        today = dj_tz.localdate()
        event = self._event(name="Austin", address="Austin, TX", on_date=today)
        token = make_checkin_session_token(event.id, self.ba.id)

        first = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={
                "session": token,
                "fieldValues": [],
                "files": [{"blobName": self._blob(event, "a")}],
            },
            content_type="application/json",
        )
        second = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={
                "session": token,
                "forceNew": True,
                "fieldValues": [],
                "files": [{"blobName": self._blob(event, "b")}],
            },
            content_type="application/json",
        )
        assert first.status_code == 200, first.content
        assert second.status_code == 200, second.content
        assert (
            CustomRecap.objects.filter(event=event, ambassador=self.ba).count() == 2
        )

    def test_identify_today_does_not_resume_a_leftover_sunday_shift(self):
        """Alicia: eventDate=Wednesday must mint today, not Sunday's open punch."""
        sunday = dj_tz.localdate() - _dt.timedelta(days=3)
        today = dj_tz.localdate()
        leftover = self._event(
            name="Sunday Miami", address="Miami, FL", on_date=sunday
        )
        stub, _ = checkin_web.get_or_create_checkin_ambassador(
            first_name="Alicia", last_name="Archie", phone="3059007912", email=None
        )
        source, _ = Source.objects.get_or_create(name="clock_in")
        Attendance.objects.create(
            ambassador=stub,
            event=leftover,
            source=source,
            clock_time=dj_tz.now() - _dt.timedelta(hours=2),
        )
        res = self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={
                "firstName": "Alicia",
                "lastName": "Archie",
                "phone": "3059007912",
                "eventDate": today.isoformat(),
                "address": "Miami, FL",
            },
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["event"]["uuid"] != str(leftover.uuid)
        assert body["event"]["date"].startswith(today.isoformat())

    def test_feel_free_standing_recap_is_auto_approved(self):
        self.tenant.name = "Feel Free"
        self.tenant.request_url_name = "bl00-feel-free"
        self.tenant.save(update_fields=["name", "request_url_name"])
        today = dj_tz.localdate()
        event = self._event(name="Miami", address="Miami, FL", on_date=today)
        recap = self._submit(event, [{"blobName": self._blob(event, "ff")}])
        recap.refresh_from_db()
        assert recap.approved is True

    def test_other_brand_standing_recap_stays_unapproved(self):
        today = dj_tz.localdate()
        event = self._event(name="Austin", address="Austin, TX", on_date=today)
        recap = self._submit(event, [{"blobName": self._blob(event, "x")}])
        recap.refresh_from_db()
        assert recap.approved is False

    def test_identify_same_day_still_resumes_an_open_shift(self):
        """Lost session, same calendar day: still land on the open event."""
        today = dj_tz.localdate()
        ev = self._event(name="Today store", address="100 Main St", on_date=today)
        stub, _ = checkin_web.get_or_create_checkin_ambassador(
            first_name="Joy", last_name="H", phone="5550100444", email=None
        )
        source, _ = Source.objects.get_or_create(name="clock_in")
        Attendance.objects.create(
            ambassador=stub,
            event=ev,
            source=source,
            clock_time=dj_tz.now() - _dt.timedelta(hours=2),
        )
        res = self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={
                "firstName": "Joy",
                "lastName": "H",
                "phone": "5550100444",
                "eventDate": today.isoformat(),
                "address": "100 Main Street",
            },
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        assert res.json()["event"]["uuid"] == str(ev.uuid)


class TestFeelFreeTenantGate:
    def test_only_the_feel_free_brand_matches(self):
        from types import SimpleNamespace

        def t(**kw):
            base = {"slug": "", "name": "", "request_url_name": ""}
            base.update(kw)
            return SimpleNamespace(**base)

        assert checkin_web.is_feel_free_tenant(
            t(slug="feel-free", name="Feel Free", request_url_name="bl00-feel-free")
        )
        assert checkin_web.is_feel_free_tenant(t(request_url_name="bl00-feel-free"))
        assert not checkin_web.is_feel_free_tenant(
            t(slug="keee-torch-thc", name="Torch", request_url_name="keee-torch-thc")
        )
        assert not checkin_web.is_feel_free_tenant(
            t(slug="liquid-death", name="Liquid Death", request_url_name="ighn-liquid-death")
        )
        assert not checkin_web.is_feel_free_tenant(
            t(slug="krispy-krunchy", name="Krispy Krunchy Chicken")
        )
        assert not checkin_web.is_feel_free_tenant(
            t(slug="girl-beer", name="Girl Beer")
        )
