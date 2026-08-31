"""Walk-up clock-in accepts the time the BA meant.

A BA in a stockroom with no bars taps Clock in at 3pm. The page queues that
tap and flushes when they're back online. Without a client timestamp the
server would stamp 5pm and Sabeen's "I've been here since 3" would be lost.

These pin:

* ``parse_client_clock_time`` — ISO in, future/too-old/junk out
* the public clock endpoint honors ``clockedInAt``
* a second flush (same session, or the same idempotency key) does not
  insert a second ``clock_in``
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import Attendance
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestParseClientClockTime:
    def test_blank_means_use_server_now(self):
        assert checkin_web.parse_client_clock_time(None) is None
        assert checkin_web.parse_client_clock_time("") is None
        assert checkin_web.parse_client_clock_time("   ") is None

    def test_accepts_iso_from_the_browser(self):
        now = dj_tz.now()
        when = now - _dt.timedelta(hours=2)
        got = checkin_web.parse_client_clock_time(when.isoformat(), now=now)
        assert got is not None
        assert abs((got - when).total_seconds()) < 1

    def test_accepts_javascript_toisostring(self):
        now = dj_tz.now()
        when = now - _dt.timedelta(hours=2)
        js = when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        got = checkin_web.parse_client_clock_time(js, now=now)
        assert got is not None
        assert abs((got - when).total_seconds()) < 1

    def test_rejects_a_future_stamp(self):
        now = dj_tz.now()
        future = (now + _dt.timedelta(minutes=10)).isoformat()
        with pytest.raises(checkin_web.ClientClockTimeError) as exc:
            checkin_web.parse_client_clock_time(future, now=now)
        assert exc.value.reason == "future"

    def test_rejects_a_stamp_older_than_24h(self):
        now = dj_tz.now()
        old = (now - _dt.timedelta(hours=25)).isoformat()
        with pytest.raises(checkin_web.ClientClockTimeError) as exc:
            checkin_web.parse_client_clock_time(old, now=now)
        assert exc.value.reason == "too_old"

    def test_rejects_junk(self):
        with pytest.raises(checkin_web.ClientClockTimeError) as exc:
            checkin_web.parse_client_clock_time("yesterday", now=dj_tz.now())
        assert exc.value.reason == "invalid"


@pytest.mark.django_db(transaction=True)
class TestCheckinClientClockedInAt(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Clock Time {uid}")
        self.event = Event.objects.create(
            tenant=self.tenant,
            name="Sabeen shift",
            address="1 Test Way",
            walkup_code=f"FF-{uid.upper()}",
            created_by=self.system_user,
        )
        ba_user = self.create_user(
            username=f"ba-{uid}",
            email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"],
            first_name="Sabeen",
        )
        self.ambassador = self.create_ambassador(ba_user)
        self.token = make_checkin_session_token(self.event.id, self.ambassador.id)
        self.http = DjangoClient()

    def _clock(self, **extra):
        return self.http.post(
            reverse(
                "events.public_checkin_clock",
                kwargs={"code": self.event.walkup_code},
            ),
            data={"session": self.token, "kind": "in", **extra},
            content_type="application/json",
        )

    def _punches(self):
        return Attendance.objects.filter(
            ambassador=self.ambassador,
            event=self.event,
            source__name="clock_in",
        )

    def test_clock_in_uses_client_timestamp(self):
        when = dj_tz.now() - _dt.timedelta(hours=2)
        res = self._clock(clockedInAt=when.isoformat())
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["clock"]["state"] == "clocked_in"
        punch = self._punches().get()
        assert abs((punch.clock_time - when).total_seconds()) < 2
        # The public payload echoes the client time, not server now.
        echoed = _dt.datetime.fromisoformat(body["clock"]["clockInAt"])
        assert abs((echoed - when).total_seconds()) < 2

    def test_future_clocked_in_at_is_refused(self):
        future = (dj_tz.now() + _dt.timedelta(hours=1)).isoformat()
        res = self._clock(clockedInAt=future)
        assert res.status_code == 400
        assert res.json()["error"] == "future"
        assert self._punches().count() == 0

    def test_too_old_clocked_in_at_is_refused(self):
        old = (dj_tz.now() - _dt.timedelta(hours=25)).isoformat()
        res = self._clock(clockedInAt=old)
        assert res.status_code == 400
        assert res.json()["error"] == "too_old"
        assert self._punches().count() == 0

    def test_second_clock_in_does_not_double_punch(self):
        when = dj_tz.now() - _dt.timedelta(hours=1)
        first = self._clock(clockedInAt=when.isoformat(), idempotencyKey="sabeen-3pm")
        assert first.status_code == 200
        second = self._clock(
            clockedInAt=when.isoformat(), idempotencyKey="sabeen-3pm"
        )
        assert second.status_code == 200
        assert second.json().get("alreadyIn") is True
        assert self._punches().count() == 1

    def test_idempotency_key_does_not_block_second_shift_after_clock_out(self):
        """Feel Free morning→afternoon: first shift's offline key must not
        no-op the afternoon clock-in after they clocked out."""
        morning = dj_tz.now() - _dt.timedelta(hours=3)
        first = self._clock(
            clockedInAt=morning.isoformat(), idempotencyKey="shift1-offline"
        )
        assert first.status_code == 200
        assert first.json()["clock"]["state"] == "clocked_in"

        out = self.http.post(
            reverse(
                "events.public_checkin_clock",
                kwargs={"code": self.event.walkup_code},
            ),
            data={"session": self.token, "kind": "out"},
            content_type="application/json",
        )
        assert out.status_code == 200
        assert out.json()["clock"]["state"] == "clocked_out"

        # Clock-out stamps server now. Afternoon must be *after* that punch —
        # clock_state orders by clock_time, so an earlier second-shift stamp
        # would leave the BA looking clocked_out even after a successful in.
        afternoon = dj_tz.now() + _dt.timedelta(seconds=5)
        second = self._clock(
            clockedInAt=afternoon.isoformat(),
            idempotencyKey="shift1-offline",  # stale key from morning queue
        )
        assert second.status_code == 200, second.content
        assert second.json().get("alreadyIn") is not True
        assert second.json()["clock"]["state"] == "clocked_in"
        assert self._punches().count() == 2

    def test_omitting_clocked_in_at_still_uses_server_now(self):
        before = dj_tz.now()
        res = self._clock()
        after = dj_tz.now()
        assert res.status_code == 200
        punch = self._punches().get()
        assert before - _dt.timedelta(seconds=2) <= punch.clock_time <= after + _dt.timedelta(seconds=2)
