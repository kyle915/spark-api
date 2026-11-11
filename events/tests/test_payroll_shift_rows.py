"""payrollTimesheetShifts — the shifts behind one BA's timesheet row."""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from events import models as em
from events.tests.base import EventsGraphQLTestCase

SHIFTS_Q = """
query Shifts($s: String!, $e: String!, $amb: String!, $tid: ID) {
  payrollTimesheetShifts(startDate: $s, endDate: $e, ambassadorUuid: $amb, tenantId: $tid) {
    eventUuid eventName date clockInAt clockOutAt
    clockInAttendanceUuid clockOutAttendanceUuid
    onTheClock scheduledHours hours estimated rate pay
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestPayrollShiftRows(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from ambassadors.models import Ambassador
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Shift Rows Tenant")
        self.other = self.create_tenant(name="Shift Rows Other")
        self.admin = self.create_user(
            username="shiftrows-admin",
            email="shiftrows@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        ba_user = self.create_user(
            username="shiftrows-ba", email="shiftrows-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = Ambassador.objects.create(user=ba_user, created_by=self.sys)
        self.day = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)

    def _shift(self, *, tenant, days_ago, approved=True, punches=()):
        from ambassadors.models import AmbassadorEvent, Attendance, Source

        when = self.day - timedelta(days=days_ago)
        ev = em.Event.objects.create(
            name=f"Store {days_ago}", tenant=tenant, date=when, created_by=self.sys,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.ba, event=ev, tenant=tenant,
            is_approved=approved, created_by=self.sys,
        )
        for kind, offset_h in punches:
            src, _ = Source.objects.get_or_create(name=kind)
            Attendance.objects.create(
                ambassador=self.ba, event=ev, source=src,
                clock_time=when + timedelta(hours=offset_h),
            )
        return ev

    async def _run(self, tenant=None, days=30):
        res = await self._execute_mutation(
            SHIFTS_Q,
            {
                "s": (self.day - timedelta(days=days)).date().isoformat(),
                "e": self.day.date().isoformat(),
                "amb": str(self.ba.uuid),
                "tid": str(tenant.id) if tenant else None,
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        return res.data["payrollTimesheetShifts"]

    @pytest.mark.asyncio
    async def test_rows_carry_real_hours_and_punch_ids(self):
        await sync_to_async(self._shift)(
            tenant=self.tenant, days_ago=5,
            punches=(("clock_in", 0), ("clock_out", 3.5)),
        )
        rows = await self._run(self.tenant)
        assert len(rows) == 1
        r = rows[0]
        assert r["hours"] == 3.5 and r["estimated"] is False
        assert r["clockInAttendanceUuid"] and r["clockOutAttendanceUuid"]
        assert r["onTheClock"] is False
        assert r["pay"] is None and r["rate"] is None  # no booked rate

    @pytest.mark.asyncio
    async def test_open_punch_and_unapproved_and_other_tenant(self):
        await sync_to_async(self._shift)(
            tenant=self.tenant, days_ago=1, punches=(("clock_in", 0),)
        )
        await sync_to_async(self._shift)(
            tenant=self.tenant, days_ago=2, approved=False,
            punches=(("clock_in", 0), ("clock_out", 2)),
        )
        await sync_to_async(self._shift)(
            tenant=self.other, days_ago=3,
            punches=(("clock_in", 0), ("clock_out", 2)),
        )
        scoped = await self._run(self.tenant)
        assert len(scoped) == 1  # unapproved booking excluded, other tenant excluded
        assert scoped[0]["onTheClock"] is True
        assert scoped[0]["clockOutAt"] is None
        assert scoped[0]["clockOutAttendanceUuid"] is None
        everything = await self._run(None)
        assert len(everything) == 2  # admin, all tenants

    @pytest.mark.asyncio
    async def test_bad_dates_return_empty(self):
        res = await self._execute_mutation(
            SHIFTS_Q,
            {"s": "nope", "e": "nope", "amb": str(self.ba.uuid), "tid": None},
            user=self.admin,
        )
        assert res.errors is None, res.errors
        assert res.data["payrollTimesheetShifts"] == []
