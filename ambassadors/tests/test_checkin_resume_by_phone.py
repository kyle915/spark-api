"""A BA who reopens the standing link in another browser must get her shift back.

From the field (Torch, Annjolie, Oct 2026): clocked in at 9:31 on TH-2HRV3D,
looked at the page again three hours later and saw a blank "Who's working?"
form "as if I never clocked in". Her punch was fine on the server; the page
only remembers a BA through browser storage, and a texting app's in-app
browser, a private tab, or a second phone has none.

The fix is server-backed: typing the same phone finds the open shift
(``openShift`` on the phone lookup, ``resumeOnly`` on identify) without
picking a program or retyping the store, and never mints a stub or an event.
"""
import datetime as _dt
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestResumeByPhone(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:6].upper()
        self.tenant = self.create_tenant(name=f"Torch {uid}")
        self.tenant.checkin_code = f"TH-{uid}"
        self.tenant.save(update_fields=["checkin_code"])
        self.code = self.tenant.checkin_code
        self.http = DjangoClient()
        self.today = dj_tz.localdate()

    def _event(self, on_date, address="11000 S Red Road, Pinecrest, FL 33156"):
        return Event.objects.create(
            tenant=self.tenant,
            name=f"{on_date:%-m/%-d/%Y} - {address}",
            address=address,
            date=checkin_web._event_date_utc(on_date),
            created_by=self.system_user,
        )

    def _punch(self, ambassador, event, kind, hours_ago):
        source, _ = Source.objects.get_or_create(name=kind)
        Attendance.objects.create(
            ambassador=ambassador,
            event=event,
            source=source,
            clock_time=dj_tz.now() - _dt.timedelta(hours=hours_ago),
        )

    def _clocked_in_stub(self, phone="3055550142", hours_ago=3):
        stub, _ = checkin_web.get_or_create_checkin_ambassador(
            first_name="Annjolie", last_name="A", phone=phone, email=None
        )
        event = self._event(self.today)
        self._punch(stub, event, "clock_in", hours_ago)
        return stub, event

    def _identify(self, **body):
        return self.http.post(
            reverse("events.public_checkin_identify", kwargs={"code": self.code}),
            data=body,
            content_type="application/json",
        )

    def _lookup(self, **body):
        return self.http.post(
            reverse(
                "events.public_checkin_unfiled_recaps", kwargs={"code": self.code}
            ),
            data=body,
            content_type="application/json",
        )

    # -- phone lookup: "You're on the clock" ------------------------------

    def test_lookup_reports_the_open_shift(self):
        _, event = self._clocked_in_stub()
        res = self._lookup(phone="(305) 555-0142", eventDate=self.today.isoformat())
        assert res.status_code == 200, res.content
        shift = res.json()["openShift"]
        assert shift is not None
        assert shift["address"] == event.address
        assert shift["eventDate"] == self.today.isoformat()
        assert shift["clockInAt"]

    def test_lookup_has_no_open_shift_after_clock_out(self):
        stub, event = self._clocked_in_stub()
        self._punch(stub, event, "clock_out", 1)
        res = self._lookup(phone="3055550142", eventDate=self.today.isoformat())
        assert res.json()["openShift"] is None

    def test_lookup_ignores_yesterdays_leftover_punch(self):
        stub, _ = checkin_web.get_or_create_checkin_ambassador(
            first_name="Annjolie", last_name="A", phone="3055550142", email=None
        )
        yesterday = self.today - _dt.timedelta(days=1)
        self._punch(stub, self._event(yesterday), "clock_in", 10)
        res = self._lookup(phone="3055550142", eventDate=self.today.isoformat())
        assert res.json()["openShift"] is None

    def test_lookup_for_an_unknown_phone_mints_nothing(self):
        before = User.objects.count()
        res = self._lookup(phone="3055550199", eventDate=self.today.isoformat())
        assert res.json() == {"shifts": [], "openShift": None}
        assert User.objects.count() == before

    # -- identify resumeOnly: "Continue my shift" -------------------------

    def test_resume_only_returns_the_open_shift_session(self):
        stub, event = self._clocked_in_stub()
        res = self._identify(
            phone="305-555-0142", eventDate=self.today.isoformat(), resumeOnly=True
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["sessionToken"]
        assert body["event"]["uuid"] == str(event.uuid)
        assert body["session"]["clock"]["state"] == "clocked_in"

    def test_resume_only_needs_no_name_program_or_store(self):
        self._clocked_in_stub()
        res = self._identify(phone="3055550142", resumeOnly=True,
                             eventDate=self.today.isoformat())
        assert res.status_code == 200, res.content

    def test_resume_only_without_an_open_shift_creates_nothing(self):
        users, events, bookings = (
            User.objects.count(), Event.objects.count(),
            AmbassadorEvent.objects.count(),
        )
        res = self._identify(
            firstName="New", lastName="BA", phone="3055550177",
            eventDate=self.today.isoformat(), resumeOnly=True,
            address="1 Somewhere St",
        )
        assert res.status_code == 404
        assert res.json()["error"] == "no_open_shift"
        assert User.objects.count() == users
        assert Event.objects.count() == events
        assert AmbassadorEvent.objects.count() == bookings

    def test_resume_only_after_clock_out_is_refused(self):
        stub, event = self._clocked_in_stub()
        self._punch(stub, event, "clock_out", 1)
        res = self._identify(phone="3055550142", resumeOnly=True,
                             eventDate=self.today.isoformat())
        assert res.status_code == 404

    # -- no duplicate sessions --------------------------------------------

    def test_full_identify_with_another_activation_and_store_resumes(self):
        """Re-entering everything from scratch still lands on the 9:31 shift."""
        stub, event = self._clocked_in_stub()
        events_before = Event.objects.count()
        res = self._identify(
            firstName="Annjolie", lastName="A", phone="3055550142",
            eventDate=self.today.isoformat(), address="HER Bazar market place",
        )
        assert res.status_code == 200, res.content
        assert res.json()["event"]["uuid"] == str(event.uuid)
        assert Event.objects.count() == events_before
        assert Attendance.objects.filter(
            ambassador=stub, source__name="clock_in"
        ).count() == 1

    def test_country_code_is_the_same_ba(self):
        """'+1 305…' and '305…' are one person, not two stubs."""
        stub, event = self._clocked_in_stub(phone="3055550142")
        res = self._identify(
            firstName="Annjolie", lastName="A", phone="+1 (305) 555-0142",
            eventDate=self.today.isoformat(), address="somewhere else",
        )
        assert res.status_code == 200, res.content
        assert res.json()["event"]["uuid"] == str(event.uuid)
        assert User.objects.filter(email__startswith="checkin-").filter(
            email__contains="3055550142"
        ).count() == 1

    def test_legacy_eleven_digit_stub_still_resumes(self):
        """Stubs minted before the fold are keyed '1305…'; they must still match."""
        legacy = self.create_user(
            username="checkin-13055550142@walkup.spark",
            email="checkin-13055550142@walkup.spark",
            role=self.roles["ambassador"], first_name="Annjolie",
        )
        amb = self.create_ambassador(
            user=legacy, is_active=False, created_by=self.system_user
        )
        event = self._event(self.today)
        self._punch(amb, event, "clock_in", 2)

        res = self._identify(phone="305 555 0142", resumeOnly=True,
                             eventDate=self.today.isoformat())
        assert res.status_code == 200, res.content
        assert res.json()["event"]["uuid"] == str(event.uuid)

        found, _ = checkin_web.get_or_create_checkin_ambassador(
            first_name="Annjolie", last_name="", phone="13055550142", email=None
        )
        assert found.id == amb.id

    def test_normalize_phone_folds_us_country_code_only(self):
        assert checkin_web._normalize_phone("+1 (305) 555-0142") == "3055550142"
        assert checkin_web._normalize_phone("305.555.0142") == "3055550142"
        # Not a US 11-digit number — left alone.
        assert checkin_web._normalize_phone("+44 20 7946 0958") == "442079460958"
        assert checkin_web._normalize_phone("0001234567890") == "0001234567890"
