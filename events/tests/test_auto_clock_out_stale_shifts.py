"""
Tests for the hourly `auto_clock_out_stale_shifts` command.

It closes a shift whose BA clocked in and never clocked out, once the event
has been over for a while. A shift is "still open" when its latest punch is
a clock-in, so the clock-out it inserts has to land AFTER that clock-in —
including for a BA who clocked in after the scheduled end. Otherwise the
shift still reads open and every hourly run closes it (and pushes) again.
"""

import io
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ambassadors.attendance_hours import clock_facts, worked_hours
from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestAutoClockOutStaleShifts(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Auto Clock-out Tenant")
        self.admin = self.create_user(
            username="admin-autoclock",
            email="admin-autoclock@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-autoclock",
            email="ba-autoclock@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.end = datetime.now(_tz.utc) - timedelta(hours=5)
        self.start = self.end - timedelta(hours=4)
        self.event = self.create_event(
            name="Forgot to clock out",
            tenant=self.tenant,
            date=self.start,
            start_time=self.start,
            end_time=self.end,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.admin,
        )
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_send:
            self.mock_send = mock_send
            yield

    def _punch(self, kind, when):
        source, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            ambassador=self.ambassador,
            event=self.event,
            source=source,
            clock_time=when,
        )

    def _run(self, *args):
        self.mock_send.reset_mock()
        out = io.StringIO()
        call_command("auto_clock_out_stale_shifts", *args, stdout=out)
        return out.getvalue()

    def _clock_outs(self):
        return list(
            Attendance.objects.filter(
                event=self.event, source__name="clock_out"
            ).order_by("clock_time")
        )

    def _hours(self):
        facts = clock_facts([self.event.id]).get(
            (self.event.id, self.ambassador.id)
        )
        return worked_hours(facts, 4.0)

    def test_on_time_clock_in_is_closed_at_the_scheduled_end_once(self):
        self._punch("clock_in", self.start)
        assert "closed 1 of 1" in self._run()
        (out,) = self._clock_outs()
        assert out.clock_time == self.end
        self.mock_send.assert_called_once()
        assert "scheduled end time" in self.mock_send.call_args.kwargs["body"]
        assert self._hours() == (4.0, False)

        assert "none left clocked-in" in self._run()
        assert len(self._clock_outs()) == 1
        self.mock_send.assert_not_called()

    def test_clock_in_after_the_scheduled_end_is_closed_once_not_every_hour(self):
        late_in = self.end + timedelta(minutes=40)
        self._punch("clock_in", late_in)

        assert "closed 1 of 1" in self._run()
        (out,) = self._clock_outs()
        assert out.clock_time == late_in + timedelta(minutes=1)
        self.mock_send.assert_called_once()
        assert "scheduled end time" not in self.mock_send.call_args.kwargs["body"]
        assert clock_facts([self.event.id])[
            (self.event.id, self.ambassador.id)
        ]["latest_kind"] == "clock_out"

        # Next hourly run: the shift reads closed, nothing is added or pushed.
        assert "none left clocked-in" in self._run()
        assert len(self._clock_outs()) == 1
        self.mock_send.assert_not_called()

        # One minute on the clock, recorded as real (not estimated) hours.
        assert self._hours() == (0.02, False)

    def test_dry_run_writes_nothing(self):
        self._punch("clock_in", self.end + timedelta(minutes=40))
        assert "[dry-run]" in self._run("--dry-run")
        assert self._clock_outs() == []
        self.mock_send.assert_not_called()
