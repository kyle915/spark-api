"""Clear leftover clock-in so a BA can start today's standing-link shift.

Heather (Feel Free, Aug 2026): the 90-day tenant session in localStorage
restored Saturday's event. Clock in registered Aug 15. The Alicia date-gate
on identify never ran because she never re-identified.

Clearing clocks out the leftover punch without deleting a filed recap, so
the next identify mints/finds today's event.
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event
from recaps.models import CustomRecap, CustomRecapTemplate, FileType


@pytest.mark.django_db(transaction=True)
class TestClearLeftoverClock(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Feel Free {uid}")
        self.tenant.checkin_code = f"FF-{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])
        ba_user = self.create_user(
            username=f"ba-ff-{uid}@test.com",
            email=f"ba-ff-{uid}@test.com",
            role=self.roles["ambassador"],
            first_name="Heather",
            last_name="K",
        )
        self.ba = self.create_ambassador(ba_user)
        self.http = DjangoClient()

    def _event(self, *, name, address, on_date):
        return Event.objects.create(
            tenant=self.tenant,
            name=name,
            address=address,
            date=checkin_web._event_date_utc(on_date),
            created_by=self.system_user,
        )

    def _punch(self, event, kind, when=None):
        source, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            ambassador=self.ba,
            event=event,
            source=source,
            clock_time=when or dj_tz.now(),
        )

    def _identify(self, *, event_date, address, phone="5550100737"):
        return self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={
                "firstName": "Heather",
                "lastName": "K",
                "phone": phone,
                "eventDate": event_date.isoformat(),
                "address": address,
            },
            content_type="application/json",
        )

    def test_abandon_clocks_out_an_open_punch(self):
        saturday = dj_tz.localdate() - _dt.timedelta(days=4)
        event = self._event(name="Saturday Austin", address="Austin, TX", on_date=saturday)
        self._punch(event, "clock_in", dj_tz.now() - _dt.timedelta(hours=2))
        result = checkin_web.abandon_open_clock(ambassador=self.ba, event=event)
        assert result["cleared"] is True
        assert result["clockedOut"] is True
        assert result["clock"]["state"] == "clocked_out"

    def test_abandon_is_noop_when_already_clocked_out(self):
        saturday = dj_tz.localdate() - _dt.timedelta(days=4)
        event = self._event(name="Saturday Austin", address="Austin, TX", on_date=saturday)
        t0 = dj_tz.now() - _dt.timedelta(hours=4)
        self._punch(event, "clock_in", t0)
        self._punch(event, "clock_out", t0 + _dt.timedelta(hours=2))
        result = checkin_web.abandon_open_clock(ambassador=self.ba, event=event)
        assert result["cleared"] is True
        assert result["clockedOut"] is False
        assert result["clock"]["state"] == "clocked_out"
        assert Attendance.objects.filter(
            ambassador=self.ba, event=event, source__name="clock_out"
        ).count() == 1

    def test_clear_clock_endpoint_closes_leftover_and_today_identify_is_fresh(self):
        saturday = dj_tz.localdate() - _dt.timedelta(days=4)
        today = dj_tz.localdate()
        phone = "5550100737"

        sat = self._identify(event_date=saturday, address="Austin, TX", phone=phone)
        assert sat.status_code == 200, sat.content
        leftover_uuid = sat.json()["event"]["uuid"]
        token = sat.json()["sessionToken"]

        clocked = self.http.post(
            reverse(
                "events.public_checkin_clock",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={"session": token, "kind": "in"},
            content_type="application/json",
        )
        assert clocked.status_code == 200, clocked.content
        assert clocked.json()["clock"]["state"] == "clocked_in"

        res = self.http.post(
            reverse(
                "events.public_checkin_clear_clock",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={"session": token},
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["cleared"] is True
        assert body["clockedOut"] is True
        assert body["clock"]["state"] == "clocked_out"

        nxt = self._identify(event_date=today, address="Austin, TX", phone=phone)
        assert nxt.status_code == 200, nxt.content
        fresh = nxt.json()
        assert fresh["event"]["uuid"] != leftover_uuid
        assert fresh["event"]["date"].startswith(today.isoformat())
        assert fresh["session"]["clock"]["state"] == "not_started"

    def test_clear_clock_does_not_delete_a_filed_recap(self):
        saturday = dj_tz.localdate() - _dt.timedelta(days=4)
        event = self._event(name="Saturday Austin", address="Austin, TX", on_date=saturday)
        self._punch(event, "clock_in", dj_tz.now() - _dt.timedelta(hours=2))
        FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.system_user}
        )
        etype = self.create_event_type("Field Sampling", self.tenant)
        template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="FF Sampling",
            event_type=etype,
            created_by=self.system_user,
        )
        recap = CustomRecap.objects.create(
            name="filed",
            event=event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=template,
            submitted_at=dj_tz.now(),
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        token = make_checkin_session_token(event.id, self.ba.id)
        res = self.http.post(
            reverse(
                "events.public_checkin_clear_clock",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={"session": token},
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        recap.refresh_from_db()
        assert CustomRecap.objects.filter(id=recap.id).exists()
        assert recap.submitted_at is not None

    def test_clear_clock_requires_a_session(self):
        res = self.http.post(
            reverse(
                "events.public_checkin_clear_clock",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data={},
            content_type="application/json",
        )
        assert res.status_code == 401
