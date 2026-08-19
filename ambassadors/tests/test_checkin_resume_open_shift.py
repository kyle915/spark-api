"""Re-identifying while on the clock must return you to the SAME shift.

From the field: a BA clocked in at 3:55, lost her session, re-identified, and
the standing link put her on a NEW event because she typed the store address
slightly differently the second time. "It's not letting me go to where I
clocked in before." She was stuck on the clock with no way to clock out and
her hours were stranded on an event she couldn't reach.

Find-or-create keys on the typed address — right for "which activation is
this", wrong for "where am I already working".
"""
import datetime as _dt
import uuid

import pytest
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestResumeOpenShift(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Resume {uid}")
        ba_user = self.create_user(
            username=f"ba-{uid}", email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"], first_name="Joy", last_name="H",
        )
        self.ambassador = self.create_ambassador(ba_user)

    def _event(self, name, address):
        return Event.objects.create(
            tenant=self.tenant, name=name, address=address,
            created_by=self.system_user,
        )

    def _punch(self, event, kind, when=None):
        source, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            ambassador=self.ambassador, event=event, source=source,
            clock_time=when or dj_tz.now(),
        )

    def test_an_open_shift_is_resumed(self):
        ev = self._event("8/1 - 100 Main St", "100 Main St")
        self._punch(ev, "clock_in", dj_tz.now() - _dt.timedelta(hours=2))
        found = checkin_web.open_shift_event_for(
            ambassador=self.ambassador, tenant=self.tenant
        )
        assert found is not None and found.id == ev.id

    def test_a_closed_shift_is_not_resumed(self):
        """Clocked out already — the next check-in is a NEW shift."""
        ev = self._event("8/1 - 100 Main St", "100 Main St")
        t0 = dj_tz.now() - _dt.timedelta(hours=4)
        self._punch(ev, "clock_in", t0)
        self._punch(ev, "clock_out", t0 + _dt.timedelta(hours=2))
        assert checkin_web.open_shift_event_for(
            ambassador=self.ambassador, tenant=self.tenant
        ) is None

    def test_a_stale_open_punch_is_not_resumed(self):
        """A missed clock-out from days ago must not hijack today's shift."""
        ev = self._event("old", "1 Old Rd")
        self._punch(ev, "clock_in", dj_tz.now() - _dt.timedelta(hours=40))
        assert checkin_web.open_shift_event_for(
            ambassador=self.ambassador, tenant=self.tenant
        ) is None

    def test_another_tenants_open_shift_is_never_resumed(self):
        other = self.create_tenant(name=f"Other {uuid.uuid4().hex[:6]}")
        ev = Event.objects.create(
            tenant=other, name="theirs", created_by=self.system_user,
        )
        self._punch(ev, "clock_in", dj_tz.now() - _dt.timedelta(hours=1))
        assert checkin_web.open_shift_event_for(
            ambassador=self.ambassador, tenant=self.tenant
        ) is None

    def test_the_newest_open_shift_wins(self):
        older = self._event("older", "1 A St")
        newer = self._event("newer", "2 B St")
        self._punch(older, "clock_in", dj_tz.now() - _dt.timedelta(hours=6))
        self._punch(newer, "clock_in", dj_tz.now() - _dt.timedelta(hours=1))
        found = checkin_web.open_shift_event_for(
            ambassador=self.ambassador, tenant=self.tenant
        )
        assert found is not None and found.id == newer.id

    def test_a_ba_with_no_punches_gets_nothing(self):
        self._event("somewhere", "9 Nowhere")
        assert checkin_web.open_shift_event_for(
            ambassador=self.ambassador, tenant=self.tenant
        ) is None

    def test_open_shift_on_another_calendar_day_is_not_resumed(self):
        """Alicia: leftover Sunday clock-in must not steal Wednesday."""
        sunday = dj_tz.localdate() - _dt.timedelta(days=3)
        today = dj_tz.localdate()
        ev = Event.objects.create(
            tenant=self.tenant, name="Sunday Miami", address="Miami, FL",
            date=checkin_web._event_date_utc(sunday),
            created_by=self.system_user,
        )
        self._punch(ev, "clock_in", dj_tz.now() - _dt.timedelta(hours=2))
        assert checkin_web.open_shift_event_for(
            ambassador=self.ambassador, tenant=self.tenant, on_date=today
        ) is None
        found = checkin_web.open_shift_event_for(
            ambassador=self.ambassador, tenant=self.tenant, on_date=sunday
        )
        assert found is not None and found.id == ev.id

    def test_existing_shift_does_not_bind_today_to_a_sunday_event(self):
        """A Wednesday-morning punch on Sunday's event must not attach today."""
        sunday = dj_tz.localdate() - _dt.timedelta(days=3)
        today = dj_tz.localdate()
        ev = Event.objects.create(
            tenant=self.tenant, name="Sunday Miami", address="Miami, FL",
            date=checkin_web._event_date_utc(sunday),
            created_by=self.system_user,
        )
        self._punch(ev, "clock_in", dj_tz.now() - _dt.timedelta(hours=1))
        assert checkin_web.existing_shift_event_for(
            ambassador=self.ambassador,
            tenant=self.tenant,
            on_date=today,
            address="Miami, FL",
        ) is None
