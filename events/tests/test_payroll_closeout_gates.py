"""
Payroll close-out writes — approvePayrollHours, markPayrollPaid,
unapprovePayrollHours — are Spark-admin only (IsSparkAdminAccess,
DB-backed).

Before: IsClientOrSparkAdmin let a client approve, un-approve or mark paid
any BA's hours in their tenant. The only screen that calls these is the
admin-only /timesheet page, and the timesheet read itself is already
admin-only (#1426).

Run through the real clients schema (the web app's endpoint).
"""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.models import Ambassador, AmbassadorEvent
from events import models as em
from events.tests.base import EventsGraphQLTestCase

APPROVE = """
mutation Approve($input: ApprovePayrollInput!) {
  approvePayrollHours(input: $input) { success message count }
}
"""

PAID = """
mutation Paid($input: ApprovePayrollInput!) {
  markPayrollPaid(input: $input) { success message count }
}
"""

UNAPPROVE = """
mutation Unapprove($input: ApprovePayrollInput!) {
  unapprovePayrollHours(input: $input) { success message count }
}
"""

FIELD = {
    APPROVE: "approvePayrollHours",
    PAID: "markPayrollPaid",
    UNAPPROVE: "unapprovePayrollHours",
}


@pytest.mark.django_db(transaction=True)
class TestPayrollCloseoutGates(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Payroll Brand")

        self.admin = self.create_user(
            username="pay-admin", email="pay-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="pay-client", email="pay-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.ba_user = self.create_user(
            username="pay-ba", email="pay-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant)
        self.ba = Ambassador.objects.create(user=self.ba_user, created_by=self.sys)

        now = timezone.now()
        self.day = now.date()
        event = em.Event.objects.create(
            name="Paid Shift", tenant=self.tenant, date=now, created_by=self.sys,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.ba, event=event, tenant=self.tenant,
            is_approved=True, created_by=self.sys,
        )
        self.start = self.day - timedelta(days=3)
        self.end = self.day + timedelta(days=3)

    def _who(self, who):
        return {
            "anon": None,
            "client": self.client_user,
            "ba": self.ba_user,
            "admin": self.admin,
        }[who]

    def _input(self):
        return {
            "input": {
                "startDate": self.start.isoformat(),
                "endDate": self.end.isoformat(),
                "tenantId": str(self.tenant.id),
                "ambassadorUuids": [str(self.ba.uuid)],
            }
        }

    async def _run(self, mutation, who):
        return await self._execute_mutation(
            mutation, self._input(), self.endpoint_path, user=self._who(who)
        )

    async def _approval(self):
        return await sync_to_async(
            lambda: em.PayrollApproval.objects.filter(
                tenant=self.tenant, ambassador=self.ba,
                period_start=self.start, period_end=self.end,
            )
            .values("paid_at")
            .first()
        )()

    async def _seed_approval(self):
        await sync_to_async(em.PayrollApproval.objects.create)(
            tenant=self.tenant, ambassador=self.ba,
            period_start=self.start, period_end=self.end,
        )

    def _assert_refused(self, result, mutation):
        assert result.errors
        assert result.data is None or result.data.get(FIELD[mutation]) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client", "ba"])
    async def test_approve_refused_for_non_admins(self, who):
        self._assert_refused(await self._run(APPROVE, who), APPROVE)
        assert await self._approval() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client", "ba"])
    async def test_mark_paid_refused_for_non_admins(self, who):
        await self._seed_approval()
        self._assert_refused(await self._run(PAID, who), PAID)
        assert (await self._approval())["paid_at"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client", "ba"])
    async def test_unapprove_refused_for_non_admins(self, who):
        await self._seed_approval()
        self._assert_refused(await self._run(UNAPPROVE, who), UNAPPROVE)
        assert await self._approval() is not None

    @pytest.mark.asyncio
    async def test_admin_full_closeout(self):
        result = await self._run(APPROVE, "admin")
        assert result.errors is None, result.errors
        assert result.data["approvePayrollHours"]["success"] is True
        assert await self._approval() == {"paid_at": None}

        result = await self._run(PAID, "admin")
        assert result.errors is None, result.errors
        assert result.data["markPayrollPaid"]["count"] == 1
        assert (await self._approval())["paid_at"] is not None

        result = await self._run(UNAPPROVE, "admin")
        assert result.errors is None, result.errors
        assert result.data["unapprovePayrollHours"]["success"] is True
        assert await self._approval() is None

    @pytest.mark.asyncio
    async def test_admin_with_unhydrated_role_can_approve(self):
        self.admin.role = None
        result = await self._run(APPROVE, "admin")
        assert result.errors is None, result.errors
        assert result.data["approvePayrollHours"]["success"] is True
