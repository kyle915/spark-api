"""Check-in shift history — a BA sees their own hours and where they stand.

Covers the service (status ladder, tenant scoping, no dollar amounts) and the
public endpoint (session-gated).
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_history, checkin_web
from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event, PayrollApproval


@pytest.mark.django_db(transaction=True)
class TestCheckinMyShifts(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Brand {uid}")
        self.tenant.checkin_code = f"BR-{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])
        self.other_tenant = self.create_tenant(name=f"Other {uid}")
        ba_user = self.create_user(
            username=f"ba-{uid}@test.com",
            email=f"ba-{uid}@test.com",
            role=self.roles["ambassador"],
            first_name="Sam",
            last_name="Rivera",
        )
        self.ba = self.create_ambassador(ba_user)
        self.http = DjangoClient()

    def _shift(self, *, tenant, days_ago, hours=3.0, approved=False, clock_out=True):
        on_date = dj_tz.localdate() - _dt.timedelta(days=days_ago)
        event = Event.objects.create(
            tenant=tenant,
            name=f"Store {days_ago}",
            address="1 Main St, Austin, TX",
            date=checkin_web._event_date_utc(on_date),
            created_by=self.system_user,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.ba,
            event=event,
            tenant=tenant,
            is_approved=approved,
            created_by=self.system_user,
        )
        t_in = dj_tz.now() - _dt.timedelta(days=days_ago, hours=6)
        src_in, _ = Source.objects.get_or_create(name="clock_in")
        Attendance.objects.create(
            ambassador=self.ba, event=event, source=src_in, clock_time=t_in
        )
        if clock_out:
            src_out, _ = Source.objects.get_or_create(name="clock_out")
            Attendance.objects.create(
                ambassador=self.ba,
                event=event,
                source=src_out,
                clock_time=t_in + _dt.timedelta(hours=hours),
            )
        return event, on_date

    def test_status_ladder_and_hours(self):
        self._shift(tenant=self.tenant, days_ago=1, hours=2.5, approved=False)
        _ev2, d2 = self._shift(tenant=self.tenant, days_ago=3, hours=4.0, approved=True)
        _ev3, d3 = self._shift(tenant=self.tenant, days_ago=10, hours=3.0, approved=True)
        _ev4, d4 = self._shift(tenant=self.tenant, days_ago=20, hours=5.0, approved=True)
        PayrollApproval.objects.create(
            tenant=self.tenant, ambassador=self.ba,
            period_start=d3 - _dt.timedelta(days=2), period_end=d3 + _dt.timedelta(days=2),
            approved_by=self.system_user,
        )
        PayrollApproval.objects.create(
            tenant=self.tenant, ambassador=self.ba,
            period_start=d4 - _dt.timedelta(days=2), period_end=d4 + _dt.timedelta(days=2),
            approved_by=self.system_user, paid_at=dj_tz.now(),
        )

        out = checkin_history.shift_history_for(ambassador=self.ba, tenant=self.tenant)
        by_date = {s["eventDate"]: s for s in out["shifts"]}
        assert len(out["shifts"]) == 4
        newest = out["shifts"][0]
        assert newest["status"] == checkin_history.STATUS_PENDING
        assert newest["hours"] == 2.5
        assert by_date[d2.isoformat()]["status"] == checkin_history.STATUS_CONFIRMED
        assert by_date[d3.isoformat()]["status"] == checkin_history.STATUS_HOURS_APPROVED
        assert by_date[d3.isoformat()]["paidAt"] is None
        assert by_date[d4.isoformat()]["status"] == checkin_history.STATUS_PAID
        assert by_date[d4.isoformat()]["paidAt"]
        assert out["totalHours"] == 14.5

    def test_never_returns_money_and_skips_other_tenants(self):
        self._shift(tenant=self.tenant, days_ago=2, approved=True)
        self._shift(tenant=self.other_tenant, days_ago=2, approved=True)
        out = checkin_history.shift_history_for(ambassador=self.ba, tenant=self.tenant)
        assert len(out["shifts"]) == 1
        flat = repr(out).lower()
        for banned in ("rate", "pay\'", "estimated", "amount", "email", "phone"):
            assert banned not in flat

    def test_open_punch_reports_on_the_clock_without_hours(self):
        self._shift(tenant=self.tenant, days_ago=0, clock_out=False)
        out = checkin_history.shift_history_for(ambassador=self.ba, tenant=self.tenant)
        s = out["shifts"][0]
        assert s["onTheClock"] is True
        assert s["hours"] is None and s["clockOutAt"] is None
        assert out["totalHours"] == 0

    def test_endpoint_requires_a_valid_session(self):
        event, _d = self._shift(tenant=self.tenant, days_ago=1, approved=True)
        url = reverse(
            "events.public_checkin_my_shifts",
            kwargs={"code": self.tenant.checkin_code},
        )
        bad = self.http.post(url, data={"session": "nope"}, content_type="application/json")
        assert bad.status_code == 401
        missing = self.http.post(url, data={}, content_type="application/json")
        assert missing.status_code == 401
        token = make_checkin_session_token(event.id, self.ba.id)
        ok = self.http.post(url, data={"session": token}, content_type="application/json")
        assert ok.status_code == 200
        assert len(ok.json()["shifts"]) == 1

    def test_session_for_another_tenant_event_is_rejected(self):
        other_event, _d = self._shift(tenant=self.other_tenant, days_ago=1)
        url = reverse(
            "events.public_checkin_my_shifts",
            kwargs={"code": self.tenant.checkin_code},
        )
        token = make_checkin_session_token(other_event.id, self.ba.id)
        res = self.http.post(url, data={"session": token}, content_type="application/json")
        assert res.status_code == 401
