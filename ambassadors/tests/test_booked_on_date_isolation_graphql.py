"""Cross-tenant isolation test for ambassadorsBookedOnDate (clients schema).

Round-2 of the clients-schema tenant-isolation sweep. ``ambassadorsBookedOnDate``
returned Ambassador ids booked on a date across ALL tenants when no tenantId
was supplied (and honored any supplied tenantId without an ownership check) —
letting a client enumerate which BAs are booked on another brand's shifts.

Proves the fix: a client/non-admin is constrained to the tenant(s) they belong
to (the no-arg variant no longer spans every tenant, and a supplied foreign
tenantId intersects to empty), while an admin keeps the cross-tenant probe used
by the InviteBAModal.
"""

from datetime import datetime, time

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from config.schema_client import schema_clients
from ambassadors import models as a_models
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


BOOKED_QUERY = """
query Booked($onDate: String!, $tenantId: ID) {
  ambassadorsBookedOnDate(onDate: $onDate, tenantId: $tenantId)
}
"""


@pytest.mark.django_db(transaction=True)
class TestBookedOnDateIsolationGraphQL(AmbassadorsGraphQLTestCase):
    """Cross-tenant isolation for ambassadorsBookedOnDate."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Booked Mine")
        self.other_tenant = self.create_tenant(name="Booked Theirs")
        self.on_date = timezone.now().date().isoformat()

    async def _client_user_for(self, username, email, tenant) -> User:
        user = await sync_to_async(self.create_user)(
            username=username, email=email, role=self.roles["client"],
            password="password123",
        )
        await sync_to_async(self.create_tenanted_user)(user=user, tenant=tenant)
        return user

    async def _admin_user(self, username, email) -> User:
        return await sync_to_async(self.create_user)(
            username=username, email=email, role=self.roles["spark_admin"],
            password="password123",
        )

    async def _booked_ambassador(self, username, email, tenant):
        """A BA booked (AmbassadorEvent) on self.on_date for `tenant`."""
        ba_user = await sync_to_async(self.create_user)(
            username=username, email=email, role=self.roles["ambassador"],
            password="password123",
        )
        amb = await sync_to_async(self.create_ambassador)(user=ba_user)
        # event.date is a DateTimeField; the resolver filters with a bare
        # `event__date=<date>`, which matches the stored datetime at midnight,
        # so store the date at midnight (aware) for a deterministic match.
        midnight = timezone.make_aware(
            datetime.combine(timezone.now().date(), time.min)
        )
        event = await sync_to_async(self.create_event)(
            name=f"{username} Event", tenant=tenant, address="1 St",
            date=midnight,
        )
        await sync_to_async(a_models.AmbassadorEvent.objects.create)(
            ambassador=amb, event=event, tenant=tenant, is_approved=True,
            created_by=self.get_system_user(),
        )
        return amb

    @pytest.mark.asyncio
    async def test_client_no_arg_does_not_span_other_tenants(self):
        """With no tenantId, a client sees only their OWN tenant's booked BAs."""
        user = await self._client_user_for("bk-c", "bkc@test.com", self.tenant)
        mine = await self._booked_ambassador("bk-mine", "bkmine@test.com", self.tenant)
        await self._booked_ambassador("bk-theirs", "bktheirs@test.com", self.other_tenant)

        result = await self._execute_mutation(
            BOOKED_QUERY, {"onDate": self.on_date}, self.endpoint_path, user=user,
        )
        assert result.errors is None
        ids = set(result.data["ambassadorsBookedOnDate"])
        assert ids == {str(mine.id)}

    @pytest.mark.asyncio
    async def test_client_foreign_tenant_id_returns_empty(self):
        """A client passing another tenant's id gets [] (intersect to empty)."""
        user = await self._client_user_for("bk-c2", "bkc2@test.com", self.tenant)
        await self._booked_ambassador("bk-theirs2", "bkt2@test.com", self.other_tenant)

        result = await self._execute_mutation(
            BOOKED_QUERY,
            {"onDate": self.on_date, "tenantId": str(self.other_tenant.id)},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["ambassadorsBookedOnDate"] == []

    @pytest.mark.asyncio
    async def test_admin_no_arg_spans_all_tenants(self):
        """An admin (no tenantId) keeps the cross-tenant probe."""
        admin = await self._admin_user("bk-a", "bka@test.com")
        mine = await self._booked_ambassador("bk-m2", "bkm2@test.com", self.tenant)
        theirs = await self._booked_ambassador("bk-t3", "bkt3@test.com", self.other_tenant)

        result = await self._execute_mutation(
            BOOKED_QUERY, {"onDate": self.on_date}, self.endpoint_path, user=admin,
        )
        assert result.errors is None
        ids = set(result.data["ambassadorsBookedOnDate"])
        assert {str(mine.id), str(theirs.id)} <= ids

    @pytest.mark.asyncio
    async def test_admin_can_probe_specific_tenant(self):
        admin = await self._admin_user("bk-a2", "bka2@test.com")
        await self._booked_ambassador("bk-m3", "bkm3@test.com", self.tenant)
        theirs = await self._booked_ambassador("bk-t4", "bkt4@test.com", self.other_tenant)

        result = await self._execute_mutation(
            BOOKED_QUERY,
            {"onDate": self.on_date, "tenantId": str(self.other_tenant.id)},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        ids = set(result.data["ambassadorsBookedOnDate"])
        assert ids == {str(theirs.id)}
