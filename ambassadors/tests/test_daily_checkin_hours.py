"""The nightly clock-in/out summary.

Two properties matter more than the formatting:

  * A day with no activity sends NOTHING. A daily email that's usually empty
    is one people stop opening, and then the one that matters gets missed too.
  * "Today" is the local calendar day, not the UTC one. A 10pm PT run reading
    UTC dates would report a day that ended at 5pm and silently drop the
    evening's shifts — the exact hours most likely to need attention.
"""
import datetime as _dt
import uuid
import zoneinfo

import pytest
from django.core.management import call_command
from django.utils import timezone as dj_tz

from ambassadors.models import Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event

PT = zoneinfo.ZoneInfo("America/Los_Angeles")


@pytest.mark.django_db(transaction=True)
class TestDailyCheckinHours(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Hours Digest {uid}")
        self.event = Event.objects.create(
            tenant=self.tenant, name="8/1/2026 - 1 Test Way",
            address="1 Test Way", created_by=self.system_user,
        )
        ba_user = self.create_user(
            username=f"ba-{uid}", email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"], first_name="Dana", last_name="Reed",
        )
        self.ambassador = self.create_ambassador(ba_user)

    def _punch(self, kind: str, when):
        source, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            ambassador=self.ambassador, event=self.event,
            source=source, clock_time=when,
        )

    def _run(self, **kw):
        from io import StringIO

        out = StringIO()
        call_command("email_daily_checkin_hours", stdout=out, dry_run=True, **kw)
        return out.getvalue()

    def test_a_quiet_day_sends_nothing(self):
        out = self._run(date="2026-08-01")
        assert '"sent": false' in out
        assert "no_activity" in out

    def test_a_worked_shift_is_summarised(self):
        day = _dt.date(2026, 8, 1)
        self._punch("clock_in", _dt.datetime(2026, 8, 1, 10, 0, tzinfo=PT))
        self._punch("clock_out", _dt.datetime(2026, 8, 1, 14, 30, tzinfo=PT))
        out = self._run(date=day.isoformat())
        assert "1 shift(s)" in out
        assert "4h 30m" in out

    def test_an_evening_shift_is_not_lost_to_utc(self):
        """9pm PT on Aug 1 is Aug 2 in UTC. Bucketing on the UTC date would
        drop it from the Aug 1 email entirely."""
        self._punch("clock_in", _dt.datetime(2026, 8, 1, 21, 0, tzinfo=PT))
        self._punch("clock_out", _dt.datetime(2026, 8, 1, 23, 0, tzinfo=PT))
        out = self._run(date="2026-08-01")
        assert "1 shift(s)" in out, "evening shift fell out of its own day"
        assert "2h 00m" in out

    def test_a_missing_clock_out_is_flagged_not_hidden(self):
        """These are the rows that need action tonight, so they must survive
        into the email rather than being dropped for lacking a pair."""
        self._punch("clock_in", _dt.datetime(2026, 8, 1, 10, 0, tzinfo=PT))
        out = self._run(date="2026-08-01")
        assert "1 shift(s)" in out
        assert "1 open" in out

    def test_multiple_punches_collapse_to_first_in_last_out(self):
        for h in (9, 10):
            self._punch("clock_in", _dt.datetime(2026, 8, 1, h, 0, tzinfo=PT))
        for h in (15, 16):
            self._punch("clock_out", _dt.datetime(2026, 8, 1, h, 0, tzinfo=PT))
        out = self._run(date="2026-08-01")
        assert "1 shift(s)" in out
        assert "7h 00m" in out, "should span first in (9) to last out (16)"

    def test_defaults_to_today_without_a_date(self):
        now = dj_tz.now().astimezone(PT)
        self._punch("clock_in", now - _dt.timedelta(hours=2))
        self._punch("clock_out", now - _dt.timedelta(hours=1))
        out = self._run()
        assert "1 shift(s)" in out
