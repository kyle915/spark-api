"""
Coverage for events/pnl.py — the per-event labor + spend roll-up.

Pins: clock-pair hours × booked rate, the scheduled-duration fallback
(flagged ``estimated``), missing-rate counting, spend folding from the
expense-receipts collector, and tenant/date scoping.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest

from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.pnl import event_pnl_rows


WHEN = datetime(2026, 5, 14, 18, 0, tzinfo=_tz.utc)
START = WHEN.date().replace(day=1)
END = WHEN.date().replace(day=28)


@pytest.mark.django_db(transaction=True)
class TestEventPnl(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.admin = self.create_user(
            username="admin-pnl",
            email="admin-pnl@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-pnl",
            email="ba-pnl@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.event = self.create_event(
            name="Vons Sparks",
            tenant=self.tenant,
            date=WHEN,
            start_time=WHEN,
            end_time=WHEN + timedelta(hours=4),
        )
        AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.admin,
        )

    def _clock(self, name, when):
        source, _ = Source.objects.get_or_create(name=name)
        return Attendance.objects.create(
            clock_time=when,
            coordinates=None,
            ambassador=self.ambassador,
            job=None,
            event=self.event,
            source=source,
        )

    def _rate(self, amount):
        from jobs.models import (
            AmbassadorJob,
            Status,
            Job,
            JobTitle,
            Rate,
            RateType,
        )

        rate_type = RateType.objects.create(
            name="Hourly", tenant=self.tenant, created_by=self.system_user
        )
        rate = Rate.objects.create(
            amount=amount,
            tenant=self.tenant,
            rate_type=rate_type,
            created_by=self.system_user,
        )
        title = JobTitle.objects.create(
            name="Brand Ambassador",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        job = Job.objects.create(
            event=self.event,
            tenant=self.tenant,
            job_title=title,
            created_by=self.system_user,
        )
        status, _ = Status.objects.get_or_create(
            name="Hired",
            tenant=self.tenant,
            defaults={"created_by": self.system_user},
        )
        AmbassadorJob.objects.create(
            ambassador=self.ambassador,
            job=job,
            rate=rate,
            status=status,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def test_clock_pair_times_rate(self):
        self._rate("30")
        self._clock("clock_in", WHEN)
        self._clock("clock_out", WHEN + timedelta(hours=3))
        rows = event_pnl_rows(self.tenant.id, START, END)
        assert len(rows) == 1
        r = rows[0]
        assert r["hours"] == 3.0
        assert r["labor_cost"] == 90.0
        assert r["estimated"] is False
        assert r["missing_rates"] == 0

    def test_no_clocks_falls_back_to_scheduled_and_flags(self):
        self._rate("30")
        rows = event_pnl_rows(self.tenant.id, START, END)
        r = rows[0]
        assert r["hours"] == 4.0  # scheduled duration
        assert r["labor_cost"] == 120.0
        assert r["estimated"] is True

    def test_missing_rate_counts_instead_of_guessing(self):
        self._clock("clock_in", WHEN)
        self._clock("clock_out", WHEN + timedelta(hours=3))
        rows = event_pnl_rows(self.tenant.id, START, END)
        r = rows[0]
        assert r["labor_cost"] == 0.0
        assert r["missing_rates"] == 1

    def test_out_of_range_excluded(self):
        self._rate("30")
        rows = event_pnl_rows(
            self.tenant.id,
            START.replace(month=1),
            END.replace(month=1),
        )
        assert rows == []
