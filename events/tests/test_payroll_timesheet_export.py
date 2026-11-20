"""payrollTimesheetExport + phone on the timesheet — the time-sheet CSV.

A time-sheet-only brand (Seven Hills) wants one line per shift: who worked
(name, phone), the date, where (the GPS address on the walk-in event), clock
in / clock out and hours. Staff only; pending walk-ups stay off it exactly as
they stay out of the timesheet totals.
"""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from events import models as em
from events.tests.base import EventsGraphQLTestCase

EXPORT_Q = """
query Export($s: String!, $e: String!, $tid: ID) {
  payrollTimesheetExport(startDate: $s, endDate: $e, tenantId: $tid) {
    ambassadorUuid ambassadorName ambassadorPhone
    date address clockInAt clockOutAt hours onTheClock tenantName
  }
}
"""

SHEET_Q = """
query Sheet($s: String!, $e: String!, $tid: ID) {
  payrollTimesheet(startDate: $s, endDate: $e, tenantId: $tid) {
    rows { name phone hours shifts }
  }
}
"""

PHONE = "(555) 010-0177"


@pytest.mark.django_db(transaction=True)
class TestPayrollTimesheetExport(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from ambassadors.models import Ambassador
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Export Tenant")
        self.other = self.create_tenant(name="Export Other")
        self.admin = self.create_user(
            username="export-admin",
            email="export@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.client_user = self.create_user(
            username="export-client", email="export-client@brand.test",
            role=self.roles["client"],
        )
        ba_user = self.create_user(
            username="export-ba", email="export-ba@test.com",
            role=self.roles["ambassador"], first_name="Test", last_name="Worker",
        )
        self.ba = Ambassador.objects.create(user=ba_user, phone=PHONE, created_by=self.sys)
        self.day = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)

    def _shift(self, *, tenant, days_ago, approved=True, punches=(), address="1 Example St"):
        from ambassadors.models import AmbassadorEvent, Attendance, Source

        when = self.day - timedelta(days=days_ago)
        ev = em.Event.objects.create(
            name=f"Shift {days_ago}", tenant=tenant, date=when, address=address,
            created_by=self.sys,
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

    async def _run(self, query, tenant=None, user=None):
        res = await self._execute_mutation(
            query,
            {
                "s": (self.day - timedelta(days=30)).date().isoformat(),
                "e": self.day.date().isoformat(),
                "tid": str(tenant.id) if tenant else None,
            },
            user=user or self.admin,
        )
        return res

    @pytest.mark.asyncio
    async def test_export_has_name_phone_date_location_times_and_hours(self):
        await sync_to_async(self._shift)(
            tenant=self.tenant, days_ago=2, address="100 Example Ave, Springfield, IL",
            punches=(("clock_in", 0), ("clock_out", 4)),
        )
        # Pending walk-up and another tenant's shift stay off.
        await sync_to_async(self._shift)(
            tenant=self.tenant, days_ago=3, approved=False,
            punches=(("clock_in", 0), ("clock_out", 2)),
        )
        await sync_to_async(self._shift)(
            tenant=self.other, days_ago=4, punches=(("clock_in", 0), ("clock_out", 2)),
        )
        res = await self._run(EXPORT_Q, self.tenant)
        assert res.errors is None, res.errors
        rows = res.data["payrollTimesheetExport"]
        assert len(rows) == 1
        r = rows[0]
        assert r["ambassadorName"] == "Test Worker"
        assert r["ambassadorPhone"] == PHONE
        assert r["ambassadorUuid"] == str(self.ba.uuid)
        assert r["address"] == "100 Example Ave, Springfield, IL"
        assert r["date"] and r["clockInAt"] and r["clockOutAt"]
        assert r["hours"] == pytest.approx(4.0)
        assert r["onTheClock"] is False

    @pytest.mark.asyncio
    async def test_timesheet_rows_carry_phone(self):
        await sync_to_async(self._shift)(
            tenant=self.tenant, days_ago=1, punches=(("clock_in", 0), ("clock_out", 3)),
        )
        res = await self._run(SHEET_Q, self.tenant)
        assert res.errors is None, res.errors
        rows = res.data["payrollTimesheet"]["rows"]
        assert rows[0]["phone"] == PHONE
        assert rows[0]["hours"] == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_export_is_staff_only(self):
        res = await self._run(EXPORT_Q, self.tenant, user=self.client_user)
        assert res.errors is not None
