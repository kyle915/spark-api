"""No live clock-in onto a day that is already over.

Weni (Torch, Sat Oct 3 2026) picked Thursday on the standing link to file
Thursday's recap. The page only offered File recap after Clock in, so she
clocked in Saturday morning onto Thursday's event and the punch ran open all
day. Nevena's test punch landed on a 9/26 event the same way.

These pin:

* ``clock_in_is_for_a_past_day`` — past dated events refuse, today and a
  shift that crossed midnight allow
* the public walk-up clock refuses with 409 ``past_day`` and writes nothing
* clock-OUT of a punch already open on a past day still works
* the BA app ``clockInToShift`` path refuses the same way
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import AmbassadorEvent, Attendance
from ambassadors.mutations import _do_attendance
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event


def _noon_utc(day: _dt.date) -> _dt.datetime:
    return _dt.datetime(day.year, day.month, day.day, 12, tzinfo=_dt.timezone.utc)


def _hawaii_today(now: _dt.datetime) -> _dt.date:
    return now.astimezone(ZoneInfo("Pacific/Honolulu")).date()


class TestClockInIsForAPastDay:
    NOW = _dt.datetime(2026, 10, 3, 15, 58, tzinfo=_dt.timezone.utc)  # Weni's punch

    def _event(self, day, start=None):
        return SimpleNamespace(date=_noon_utc(day), start_time=start)

    def test_thursday_event_on_saturday_is_refused(self):
        ev = self._event(_dt.date(2026, 10, 1))
        assert checkin_web.clock_in_is_for_a_past_day(ev, when=self.NOW)

    def test_today_is_allowed(self):
        ev = self._event(_dt.date(2026, 10, 3))
        assert not checkin_web.clock_in_is_for_a_past_day(ev, when=self.NOW)

    def test_east_coast_after_midnight_utc_is_still_today(self):
        # 9pm ET Sat = 01:00Z Sun; Saturday's event is still today in the US.
        late = _dt.datetime(2026, 10, 4, 1, 0, tzinfo=_dt.timezone.utc)
        ev = self._event(_dt.date(2026, 10, 3))
        assert not checkin_web.clock_in_is_for_a_past_day(ev, when=late)

    def test_shift_that_crossed_midnight_is_allowed(self):
        start = self.NOW - _dt.timedelta(hours=6)
        ev = self._event(_dt.date(2026, 10, 2), start=start)
        assert not checkin_web.clock_in_is_for_a_past_day(ev, when=self.NOW)

    def test_yesterdays_afternoon_start_is_refused_next_day(self):
        start = self.NOW - _dt.timedelta(hours=40)
        ev = self._event(_dt.date(2026, 10, 1), start=start)
        assert checkin_web.clock_in_is_for_a_past_day(ev, when=self.NOW)

    def test_undated_event_is_allowed(self):
        ev = SimpleNamespace(date=None, start_time=None)
        assert not checkin_web.clock_in_is_for_a_past_day(ev, when=self.NOW)


@pytest.mark.django_db(transaction=True)
class TestPublicClockRefusesPastDay(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.uid = uid
        self.tenant = self.create_tenant(name=f"Past Day {uid}")
        self.ba_user = self.create_user(
            username=f"ba-{uid}",
            email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"],
            first_name="Weni",
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.http = DjangoClient()

    def _event(self, day, start=None):
        return Event.objects.create(
            tenant=self.tenant,
            name="Lakeline",
            address="11200 Lakeline Mall Dr",
            walkup_code=f"TH-{uuid.uuid4().hex[:6].upper()}",
            date=_noon_utc(day),
            start_time=start,
            created_by=self.system_user,
        )

    def _clock(self, event, kind="in"):
        token = make_checkin_session_token(event.id, self.ambassador.id)
        return self.http.post(
            reverse("events.public_checkin_clock", kwargs={"code": event.walkup_code}),
            data={"session": token, "kind": kind},
            content_type="application/json",
        )

    def _punches(self, event, source="clock_in"):
        return Attendance.objects.filter(
            ambassador=self.ambassador, event=event, source__name=source
        )

    def test_clock_in_on_a_past_day_is_refused(self):
        event = self._event(_hawaii_today(dj_tz.now()) - _dt.timedelta(days=2))
        res = self._clock(event)
        assert res.status_code == 409, res.content
        assert res.json()["error"] == "past_day"
        assert self._punches(event).count() == 0

    def test_clock_in_today_still_works(self):
        event = self._event(_hawaii_today(dj_tz.now()))
        res = self._clock(event)
        assert res.status_code == 200, res.content
        assert res.json()["clock"]["state"] == "clocked_in"

    def test_overnight_shift_from_yesterday_can_clock_in(self):
        now = dj_tz.now()
        event = self._event(
            _hawaii_today(now) - _dt.timedelta(days=1),
            start=now - _dt.timedelta(hours=4),
        )
        res = self._clock(event)
        assert res.status_code == 200, res.content

    def test_clock_out_of_a_punch_left_open_on_a_past_day_still_works(self):
        event = self._event(_hawaii_today(dj_tz.now()))
        assert self._clock(event).status_code == 200
        event.date = _noon_utc(_hawaii_today(dj_tz.now()) - _dt.timedelta(days=2))
        event.save(update_fields=["date"])
        res = self._clock(event, kind="out")
        assert res.status_code == 200, res.content
        assert res.json()["clock"]["state"] == "clocked_out"

    def test_ba_app_clock_in_on_a_past_day_is_refused(self):
        event = self._event(_hawaii_today(dj_tz.now()) - _dt.timedelta(days=2))
        booking = AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            tenant=self.tenant,
            event=event,
            is_approved=True,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        info = SimpleNamespace(
            context=SimpleNamespace(request=SimpleNamespace(user=self.ba_user))
        )
        payload = SimpleNamespace(
            ambassador_event_uuid=booking.uuid,
            event_uuid=None,
            latitude=None,
            longitude=None,
        )
        res = asyncio.run(_do_attendance(info, payload, kind="clock_in"))
        assert res.success is False
        assert "over" in res.message
        assert self._punches(event).count() == 0
