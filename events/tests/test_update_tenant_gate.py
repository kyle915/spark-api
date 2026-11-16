"""
Cross-tenant write gate on the shared create/update service
(`BaseMutationService.set_user_and_tenant`) and on `bulkCloneRequest`.

Before: a client or ambassador could pass ANOTHER brand's row id (no
tenantId) to updateEvent / updateRetailer / updateRequest. The row was
loaded by raw id, edited, and its tenant_id rewritten to the caller's own
tenant on save.

Now, for non-admins the row's own tenant decides access: a row outside
the caller's active tenants reads as "<Model> not found." and is left
untouched; an in-tenant update keeps the row's tenant. Admins behave as
before. Run through the real spark schema.
"""

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models

WHEN = datetime(2026, 12, 5, 17, 0, tzinfo=dt_tz.utc)

UPDATE_EVENT = """
mutation UpdateEvent($input: UpdateEventInput!) {
  updateEvent(input: $input) { success message }
}
"""

UPDATE_RETAILER = """
mutation UpdateRetailer($input: UpdateRetailerInput!) {
  updateRetailer(input: $input) { success message }
}
"""

BULK_CLONE = """
mutation BulkClone($input: BulkCloneRequestInput!) {
  bulkCloneRequest(input: $input) { success message }
}
"""


@pytest.mark.django_db(transaction=True)
class TestUpdateTenantGate(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()

        self.tenant_a = self.create_tenant(name="Brand A")
        self.tenant_b = self.create_tenant(name="Brand B")

        self.admin = self.create_user(
            username="gate-admin", email="gate-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_a = self.create_user(
            username="gate-client-a", email="gate-client-a@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_a, self.tenant_a)
        self.ba_a = self.create_user(
            username="gate-ba-a", email="gate-ba-a@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_a, self.tenant_a)
        self.create_ambassador(self.ba_a)

        self.event_a = event_models.Event.objects.create(
            name="A event", tenant=self.tenant_a, address="",
            date=WHEN, start_time=WHEN, end_time=WHEN + timedelta(hours=4),
            created_by=self.system_user,
        )
        self.event_b = event_models.Event.objects.create(
            name="B event", tenant=self.tenant_b, address="",
            date=WHEN, start_time=WHEN, end_time=WHEN + timedelta(hours=4),
            created_by=self.system_user,
        )
        self.location = event_models.Location.objects.create(
            name="Austin", code="AUS", zip="78701", created_by=self.system_user,
        )
        self.retailer_b = self.create_retailer(
            name="B store", address="1 Main", store_contact="x",
            location=self.location, tenant=self.tenant_b,
        )
        rt_b = self.create_request_type(name="Demo", tenant=self.tenant_b)
        client_b = self.create_client(
            name="B client", email="b@client.test", tenant=self.tenant_b,
        )
        dist_b = self.create_distributor(
            name="B dist", email="b@dist.test", location=self.location,
            tenant=self.tenant_b,
        )
        self.request_b = self.create_request(
            name="B request", date=WHEN, address="1 Main",
            client=client_b, distributor=dist_b, retailer=self.retailer_b,
            request_type=rt_b, tenant=self.tenant_b,
            start_time=WHEN, end_time=WHEN + timedelta(hours=4),
        )

    async def _update_event(self, event, user, name="Renamed"):
        result = await self._execute_mutation(
            UPDATE_EVENT,
            {"input": {"id": str(event.id), "name": name}},
            self.endpoint_path,
            user=user,
        )
        return result

    async def _event_row(self, event):
        return await sync_to_async(
            lambda: event_models.Event.objects.values("name", "tenant_id").get(
                id=event.id
            )
        )()

    # ── updateEvent ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_anonymous_refused(self):
        result = await self._update_event(self.event_b, None)
        assert result.errors or not result.data["updateEvent"]["success"]
        row = await self._event_row(self.event_b)
        assert row == {"name": "B event", "tenant_id": self.tenant_b.id}

    @pytest.mark.asyncio
    async def test_client_cannot_update_other_brands_event(self):
        result = await self._update_event(self.event_b, self.client_a)
        assert result.errors is None
        payload = result.data["updateEvent"]
        assert payload["success"] is False
        assert payload["message"] == "Event not found."
        row = await self._event_row(self.event_b)
        assert row == {"name": "B event", "tenant_id": self.tenant_b.id}

    @pytest.mark.asyncio
    async def test_ambassador_cannot_update_other_brands_event(self):
        result = await self._update_event(self.event_b, self.ba_a)
        assert result.errors is None
        assert result.data["updateEvent"]["success"] is False
        row = await self._event_row(self.event_b)
        assert row == {"name": "B event", "tenant_id": self.tenant_b.id}

    @pytest.mark.asyncio
    async def test_client_can_update_own_event_tenant_kept(self):
        result = await self._update_event(self.event_a, self.client_a)
        assert result.errors is None
        assert result.data["updateEvent"]["success"] is True, result.data
        row = await self._event_row(self.event_a)
        assert row == {"name": "Renamed", "tenant_id": self.tenant_a.id}

    @pytest.mark.asyncio
    async def test_admin_updates_any_event_tenant_kept(self):
        result = await self._update_event(self.event_b, self.admin)
        assert result.errors is None
        assert result.data["updateEvent"]["success"] is True, result.data
        row = await self._event_row(self.event_b)
        assert row == {"name": "Renamed", "tenant_id": self.tenant_b.id}

    @pytest.mark.asyncio
    async def test_admin_with_unhydrated_role_updates_any_event(self):
        # The JWT user's role doesn't hydrate in async resolvers; admin is
        # decided from the DB, so a real admin is not treated as a client.
        self.admin.role = None
        result = await self._update_event(self.event_b, self.admin)
        assert result.errors is None
        assert result.data["updateEvent"]["success"] is True, result.data
        row = await self._event_row(self.event_b)
        assert row == {"name": "Renamed", "tenant_id": self.tenant_b.id}

    @pytest.mark.asyncio
    async def test_excluded_admin_with_stale_role_is_gated(self):
        # A removed Ignite admin (IGNITE_ADMIN_EXCLUDE) whose request user
        # still carries the spark-admin role. The old gate OR-ed in
        # is_spark_schema_request (sync role read, ignores the exclude
        # list) and let them edit and re-home any brand's row.
        from utils.graphql.permissions import IGNITE_ADMIN_EXCLUDE

        email = "gate-removed@igniteproductions.co"
        removed = await sync_to_async(self.create_user)(
            username="gate-removed", email=email,
            role=self.roles["spark_admin"],
        )
        await sync_to_async(self.create_tenanted_user)(removed, self.tenant_a)
        IGNITE_ADMIN_EXCLUDE.add(email)
        try:
            result = await self._update_event(self.event_b, removed)
        finally:
            IGNITE_ADMIN_EXCLUDE.discard(email)
        assert result.errors is None
        payload = result.data["updateEvent"]
        assert payload["success"] is False
        assert payload["message"] == "Event not found."
        row = await self._event_row(self.event_b)
        assert row == {"name": "B event", "tenant_id": self.tenant_b.id}

    # ── catalog: updateRetailer ──────────────────────────────────────

    def _retailer_input(self):
        return {
            "input": {
                "id": str(self.retailer_b.id),
                "name": "Hijacked",
                "address": "1 Main",
                "storeContact": "x",
                "locationId": str(self.location.id),
            }
        }

    @pytest.mark.asyncio
    async def test_client_cannot_update_other_brands_retailer(self):
        result = await self._execute_mutation(
            UPDATE_RETAILER, self._retailer_input(), self.endpoint_path,
            user=self.client_a,
        )
        assert result.errors is None
        assert result.data["updateRetailer"]["success"] is False
        row = await sync_to_async(
            lambda: event_models.Retailer.objects.values("name", "tenant_id").get(
                id=self.retailer_b.id
            )
        )()
        assert row == {"name": "B store", "tenant_id": self.tenant_b.id}

    @pytest.mark.asyncio
    async def test_admin_can_update_retailer(self):
        result = await self._execute_mutation(
            UPDATE_RETAILER, self._retailer_input(), self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None
        assert result.data["updateRetailer"]["success"] is True, result.data
        row = await sync_to_async(
            lambda: event_models.Retailer.objects.values("name", "tenant_id").get(
                id=self.retailer_b.id
            )
        )()
        assert row == {"name": "Hijacked", "tenant_id": self.tenant_b.id}

    # ── bulkCloneRequest ─────────────────────────────────────────────

    def _clone_input(self):
        return {
            "input": {
                "sourceRequestId": str(self.request_b.id),
                "copies": [{"dateIso": "2026-12-12"}],
            }
        }

    async def _request_count(self):
        return await sync_to_async(
            lambda: event_models.Request.objects.filter(
                tenant_id=self.tenant_b.id
            ).count()
        )()

    @pytest.mark.asyncio
    async def test_bulk_clone_refused_for_anonymous_ba_and_other_client(self):
        before = await self._request_count()
        anon = await self._execute_mutation(
            BULK_CLONE, self._clone_input(), self.endpoint_path, user=None,
        )
        assert anon.errors or not anon.data["bulkCloneRequest"]["success"]
        for user in (self.client_a, self.ba_a):
            result = await self._execute_mutation(
                BULK_CLONE, self._clone_input(), self.endpoint_path, user=user,
            )
            assert result.errors is None
            assert result.data["bulkCloneRequest"]["success"] is False
        assert await self._request_count() == before

    @pytest.mark.asyncio
    async def test_bulk_clone_admin_still_works(self):
        before = await self._request_count()
        result = await self._execute_mutation(
            BULK_CLONE, self._clone_input(), self.endpoint_path, user=self.admin,
        )
        assert result.errors is None
        assert result.data["bulkCloneRequest"]["success"] is True, result.data
        assert await self._request_count() == before + 1
