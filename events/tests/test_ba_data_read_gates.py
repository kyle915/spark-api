"""
BA operational reads that brand/client users must not see.

- payrollTimesheet / payrollTimesheetShifts: Spark admins only (was
  clients-in-tenant too: every BA's hours, pay and email).
- recentLocationPings: admins see the live map; an ambassador gets only
  their own latest ping; a client gets an empty list (the shared Today page
  still issues the query for clients, so it must not error); anonymous is
  refused.

Run through the real clients schema (the web app's endpoint).
"""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.models import Ambassador, AmbassadorEvent, LocationPing
from events import models as em
from events.tests.base import EventsGraphQLTestCase

TIMESHEET = """
query Sheet($s: String!, $e: String!, $tid: ID) {
  payrollTimesheet(startDate: $s, endDate: $e, tenantId: $tid) {
    rows { ambassadorUuid email }
  }
}
"""

SHIFTS = """
query Shifts($s: String!, $e: String!, $amb: String!, $tid: ID) {
  payrollTimesheetShifts(startDate: $s, endDate: $e, ambassadorUuid: $amb, tenantId: $tid) {
    eventUuid
  }
}
"""

PINGS = """
query Pings($tid: ID) {
  recentLocationPings(withinMinutes: 60, tenantId: $tid) { ambassadorUuid }
}
"""


@pytest.mark.django_db(transaction=True)
class TestBaDataReadGates(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Reads Brand")

        self.admin = self.create_user(
            username="reads-admin", email="reads-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="reads-client", email="reads-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.ba_user = self.create_user(
            username="reads-ba", email="reads-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant)
        self.ba = Ambassador.objects.create(user=self.ba_user, created_by=self.sys)
        other_user = self.create_user(
            username="reads-ba2", email="reads-ba2@test.com",
            role=self.roles["ambassador"],
        )
        self.other_ba = Ambassador.objects.create(user=other_user, created_by=self.sys)

        now = timezone.now()
        self.day = now.date()
        self.event = em.Event.objects.create(
            name="Today Shift", tenant=self.tenant, date=now, created_by=self.sys,
        )
        for amb in (self.ba, self.other_ba):
            AmbassadorEvent.objects.create(
                ambassador=amb, event=self.event, tenant=self.tenant,
                is_approved=True, created_by=self.sys,
            )
            LocationPing.objects.create(
                ambassador=amb, event=self.event, lat=40.0, lng=-75.0,
                recorded_at=now - timedelta(minutes=5),
            )

    def _who(self, who):
        return {
            "anon": None,
            "client": self.client_user,
            "ba": self.ba_user,
            "admin": self.admin,
        }[who]

    async def _run(self, query, variables, who):
        return await self._execute_mutation(
            query, variables, self.endpoint_path, user=self._who(who)
        )

    def _window(self, **extra):
        v = {
            "s": (self.day - timedelta(days=3)).isoformat(),
            "e": (self.day + timedelta(days=3)).isoformat(),
            "tid": str(self.tenant.id),
        }
        v.update(extra)
        return v

    # ── payrollTimesheet / payrollTimesheetShifts ────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client", "ba"])
    async def test_timesheet_refused_for_non_admins(self, who):
        result = await self._run(TIMESHEET, self._window(), who)
        assert result.errors
        assert result.data is None or result.data.get("payrollTimesheet") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client", "ba"])
    async def test_timesheet_shifts_refused_for_non_admins(self, who):
        result = await self._run(
            SHIFTS, self._window(amb=str(self.ba.uuid)), who
        )
        assert result.errors
        assert result.data is None or result.data.get("payrollTimesheetShifts") is None

    @pytest.mark.asyncio
    async def test_admin_reads_timesheet(self):
        result = await self._run(TIMESHEET, self._window(), "admin")
        assert result.errors is None, result.errors
        uuids = {r["ambassadorUuid"] for r in result.data["payrollTimesheet"]["rows"]}
        assert {str(self.ba.uuid), str(self.other_ba.uuid)} <= uuids

    @pytest.mark.asyncio
    async def test_admin_with_unhydrated_role_reads_timesheet(self):
        self.admin.role = None
        result = await self._run(TIMESHEET, self._window(), "admin")
        assert result.errors is None, result.errors

    @pytest.mark.asyncio
    async def test_admin_reads_timesheet_shifts(self):
        result = await self._run(
            SHIFTS, self._window(amb=str(self.ba.uuid)), "admin"
        )
        assert result.errors is None, result.errors
        assert [r["eventUuid"] for r in result.data["payrollTimesheetShifts"]] == [
            str(self.event.uuid)
        ]

    # ── recentLocationPings ──────────────────────────────────────────

    def _ping_uuids(self, result):
        return {p["ambassadorUuid"] for p in result.data["recentLocationPings"]}

    @pytest.mark.asyncio
    async def test_pings_refused_for_anon(self):
        result = await self._run(PINGS, {"tid": str(self.tenant.id)}, "anon")
        assert result.errors

    @pytest.mark.asyncio
    async def test_client_gets_no_pings_but_no_error(self):
        result = await self._run(PINGS, {"tid": str(self.tenant.id)}, "client")
        assert result.errors is None, result.errors
        assert result.data["recentLocationPings"] == []

    @pytest.mark.asyncio
    async def test_ba_gets_only_own_ping(self):
        result = await self._run(PINGS, {"tid": None}, "ba")
        assert result.errors is None, result.errors
        assert self._ping_uuids(result) == {str(self.ba.uuid)}

    @pytest.mark.asyncio
    async def test_admin_gets_every_ping(self):
        result = await self._run(PINGS, {"tid": str(self.tenant.id)}, "admin")
        assert result.errors is None, result.errors
        assert self._ping_uuids(result) == {str(self.ba.uuid), str(self.other_ba.uuid)}
