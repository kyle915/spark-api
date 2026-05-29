"""
Tests for the TALENT search filters on the clients-schema `ambassadors`
query: address, city, college, and the in_college flag.

The search is tenant-scoped via TenantedUser, so each BA under test is
attached to the active tenant. We assert the right subset comes back for
each filter.
"""
import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


QUERY = """
query Talent($filters: AmbassadorFiltersInput) {
  ambassadors(filters: $filters, first: 50) {
    edges { node { uuid college inCollege address } }
    totalCount
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestTalentSearch(AmbassadorsGraphQLTestCase):
    """Coverage for city / address / college / in_college filtering."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Talent Tenant")

        uid = str(uuid.uuid4())[:8]
        self.admin = self.create_user(
            username=f"admin_{uid}@test.com",
            email=f"admin_{uid}@test.com",
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(self.admin, self.tenant)

    def _make_ba(self, label, *, address=None, college="", in_college=False):
        uid = str(uuid.uuid4())[:8]
        u = self.create_user(
            username=f"ba_{label}_{uid}@test.com",
            email=f"ba_{label}_{uid}@test.com",
            role=self.roles["ambassador"],
        )
        # Attach to the active tenant so tenant scoping includes them.
        self.create_tenanted_user(u, self.tenant)
        return self.create_ambassador(
            u,
            address=address,
            is_active=True,
            college=college,
            in_college=in_college,
        )

    async def _run(self, filters):
        return await self._execute_mutation(
            QUERY,
            {"filters": {"tenantId": str(self.tenant.id), **filters}},
            self.endpoint_path,
            user=self.admin,
        )

    @pytest.mark.asyncio
    async def test_filter_by_city_matches_address_substring(self):
        austin = await sync_to_async(self._make_ba)(
            "austin", address="123 Main St, Austin, TX 78701"
        )
        await sync_to_async(self._make_ba)(
            "dallas", address="9 Oak Ave, Dallas, TX 75201"
        )

        result = await self._run({"city": "Austin"})
        assert result.errors is None, f"errored: {result.errors}"
        uuids = {
            e["node"]["uuid"]
            for e in result.data["ambassadors"]["edges"]
        }
        assert str(austin.uuid) in uuids
        assert len(uuids) == 1

    @pytest.mark.asyncio
    async def test_filter_by_address_and_city_anded(self):
        match = await sync_to_async(self._make_ba)(
            "match", address="500 Congress Ave, Austin, TX"
        )
        # Same city, different street — excluded by the address term.
        await sync_to_async(self._make_ba)(
            "other", address="12 Lamar Blvd, Austin, TX"
        )

        result = await self._run({"address": "Congress", "city": "Austin"})
        assert result.errors is None, f"errored: {result.errors}"
        uuids = {
            e["node"]["uuid"]
            for e in result.data["ambassadors"]["edges"]
        }
        assert uuids == {str(match.uuid)}

    @pytest.mark.asyncio
    async def test_filter_by_college(self):
        utexas = await sync_to_async(self._make_ba)(
            "utexas", college="University of Texas"
        )
        await sync_to_async(self._make_ba)("a&m", college="Texas A&M")

        result = await self._run({"college": "University of Texas"})
        assert result.errors is None, f"errored: {result.errors}"
        uuids = {
            e["node"]["uuid"]
            for e in result.data["ambassadors"]["edges"]
        }
        assert uuids == {str(utexas.uuid)}

    @pytest.mark.asyncio
    async def test_in_college_only_flag(self):
        student = await sync_to_async(self._make_ba)(
            "student", college="UT", in_college=True
        )
        alum = await sync_to_async(self._make_ba)(
            "alum", college="UT", in_college=False
        )

        result = await self._run({"inCollege": True})
        assert result.errors is None, f"errored: {result.errors}"
        nodes = result.data["ambassadors"]["edges"]
        uuids = {e["node"]["uuid"] for e in nodes}
        assert str(student.uuid) in uuids
        assert str(alum.uuid) not in uuids
        # Every returned BA must be in college.
        assert all(e["node"]["inCollege"] for e in nodes)
