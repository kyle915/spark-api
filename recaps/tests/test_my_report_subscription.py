"""GraphQL tests for the personal monthly-report subscription
(``setMyReportSubscription`` / ``myReportSubscription``, clients schema).

The guarantees that matter: it only ever changes the CALLER's own
subscription, a client is pinned to their own brand, and it never edits the
brand-wide recipient list or the brand switch."""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from config.schema_client import schema_clients
from tenants.models import ClientReportSubscription, Tenant
from tenants.tests.base import BaseGraphQLTestCase

SET_M = """
mutation Sub($input: SetMyReportSubscriptionInput!) {
  setMyReportSubscription(input: $input) { success message subscribed }
}
"""
GET_Q = """
query Mine($tid: ID) { myReportSubscription(tenantId: $tid) }
"""


@pytest.mark.django_db(transaction=True)
class TestMyReportSubscription(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(
            name="Sub GQL Brand",
            scheduled_report_enabled=False,
            recap_recipient_emails="list@brand.com",
        )
        self.other = self.create_tenant(name="Sub GQL Other")

    async def _client(self, email, tenant):
        user = await sync_to_async(self.create_user)(
            username=email, email=email, role=self.roles["client"],
            password="password123",
        )
        await sync_to_async(self.create_tenanted_user)(user=user, tenant=tenant)
        return user

    async def _subs(self):
        return await sync_to_async(
            lambda: list(
                ClientReportSubscription.objects.values_list(
                    "user__email", "tenant_id", "monthly_report"
                )
            )
        )()

    @pytest.mark.asyncio
    async def test_client_subscribes_then_unsubscribes_self_only(self):
        me = await self._client("me@brand.com", self.tenant)
        res = await self._execute_mutation(
            SET_M,
            {"input": {"tenantId": str(self.tenant.id), "subscribed": True}},
            self.endpoint_path,
            user=me,
        )
        assert res.errors is None, res.errors
        assert res.data["setMyReportSubscription"] == {
            "success": True,
            "message": res.data["setMyReportSubscription"]["message"],
            "subscribed": True,
        }
        assert await self._subs() == [("me@brand.com", self.tenant.id, True)]

        got = await self._execute_mutation(
            GET_Q, {"tid": str(self.tenant.id)}, self.endpoint_path, user=me
        )
        assert got.errors is None, got.errors
        assert got.data["myReportSubscription"] is True

        # The brand-wide list and switch are untouched.
        tenant = await sync_to_async(Tenant.objects.get)(id=self.tenant.id)
        assert tenant.recap_recipient_emails == "list@brand.com"
        assert tenant.scheduled_report_enabled is False

        res = await self._execute_mutation(
            SET_M,
            {"input": {"tenantId": str(self.tenant.id), "subscribed": False}},
            self.endpoint_path,
            user=me,
        )
        assert res.data["setMyReportSubscription"]["subscribed"] is False
        assert await self._subs() == [("me@brand.com", self.tenant.id, False)]

    @pytest.mark.asyncio
    async def test_client_is_pinned_to_own_brand(self):
        """Passing another brand's id subscribes the client to THEIR brand."""
        me = await self._client("pinned@brand.com", self.tenant)
        res = await self._execute_mutation(
            SET_M,
            {"input": {"tenantId": str(self.other.id), "subscribed": True}},
            self.endpoint_path,
            user=me,
        )
        assert res.errors is None, res.errors
        assert res.data["setMyReportSubscription"]["success"] is True
        assert await self._subs() == [("pinned@brand.com", self.tenant.id, True)]

    @pytest.mark.asyncio
    async def test_default_is_not_subscribed(self):
        me = await self._client("fresh@brand.com", self.tenant)
        got = await self._execute_mutation(
            GET_Q, {"tid": str(self.tenant.id)}, self.endpoint_path, user=me
        )
        assert got.errors is None, got.errors
        assert got.data["myReportSubscription"] is False
