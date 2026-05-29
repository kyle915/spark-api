"""
Tests for the admin TALENT profile pop-up resolver
`ambassadorProfileDetail` (clients schema) and its tenant scoping.

Reachability rule (mirrors the chat recipient list): an admin can open a
BA only if that BA has an AmbassadorEvent in the admin's active tenant.
A BA reachable only in another tenant must NOT be openable.
"""
import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


QUERY = """
query Detail($uuid: ID!, $tenantId: ID) {
  ambassadorProfileDetail(ambassadorUuid: $uuid, tenantId: $tenantId) {
    fullName
    email
    phone
    bio
    college
    inCollege
    headshotUrl
    resumeUrl
    ratingCount
    jobsCount
    gigHistory { eventUuid brandName venue status }
    photos { uuid }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestAmbassadorProfileDetail(AmbassadorsGraphQLTestCase):
    """Coverage for the openable profile + its tenant isolation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.tenant_a = self.create_tenant(name="Tenant A")
        self.tenant_b = self.create_tenant(name="Tenant B")

        uid = str(uuid.uuid4())[:8]
        # Admin who belongs to BOTH tenants (so role/tenant resolution is
        # not the thing under test — reachability is).
        self.admin = self.create_user(
            username=f"admin_{uid}@test.com",
            email=f"admin_{uid}@test.com",
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(self.admin, self.tenant_a)
        self.create_tenanted_user(self.admin, self.tenant_b)

    def _make_ba(self, label, **kwargs):
        uid = str(uuid.uuid4())[:8]
        u = self.create_user(
            username=f"ba_{label}_{uid}@test.com",
            email=f"ba_{label}_{uid}@test.com",
            first_name=label.title(),
            last_name="Tester",
            role=self.roles["ambassador"],
        )
        return self.create_ambassador(u, is_active=True, **kwargs)

    def _gig(self, ambassador, tenant, name, *, days_ago=7):
        from datetime import datetime, timedelta, timezone as _tz

        when = datetime.now(_tz.utc) - timedelta(days=days_ago)
        ev = self.create_event(
            name=name,
            tenant=tenant,
            address="123 St",
            date=when,
            start_time=when,
            end_time=when + timedelta(hours=4),
        )
        AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=ev,
            tenant=tenant,
            is_approved=True,
            created_by=self.get_system_user(),
        )
        return ev

    @pytest.mark.asyncio
    async def test_returns_full_profile_for_reachable_ba(self):
        ba = await sync_to_async(self._make_ba)(
            "reachable",
            phone="555-0100",
            bio="Energetic brand rep.",
            college="UT Austin",
            in_college=True,
        )
        await sync_to_async(self._gig)(ba, self.tenant_a, "LD Pop-up")

        result = await self._execute_mutation(
            QUERY,
            {"uuid": str(ba.uuid), "tenantId": str(self.tenant_a.id)},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None, f"errored: {result.errors}"
        detail = result.data["ambassadorProfileDetail"]
        assert detail is not None
        assert detail["phone"] == "555-0100"
        assert detail["bio"] == "Energetic brand rep."
        assert detail["college"] == "UT Austin"
        assert detail["inCollege"] is True
        assert detail["email"] is not None  # admin sees tenant-roster PII
        assert detail["fullName"].startswith("Reachable")
        assert len(detail["gigHistory"]) == 1
        assert detail["gigHistory"][0]["status"] == "worked"

    @pytest.mark.asyncio
    async def test_cross_tenant_ba_is_not_openable(self):
        # BA reachable ONLY in tenant B.
        ba = await sync_to_async(self._make_ba)("crosstenant")
        await sync_to_async(self._gig)(ba, self.tenant_b, "Other Brand Gig")

        # Admin asks for it scoped to tenant A — must get None, no leak.
        result = await self._execute_mutation(
            QUERY,
            {"uuid": str(ba.uuid), "tenantId": str(self.tenant_a.id)},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["ambassadorProfileDetail"] is None

        # Same admin, scoped to tenant B — now it resolves.
        result_b = await self._execute_mutation(
            QUERY,
            {"uuid": str(ba.uuid), "tenantId": str(self.tenant_b.id)},
            self.endpoint_path,
            user=self.admin,
        )
        assert result_b.errors is None, f"errored: {result_b.errors}"
        assert result_b.data["ambassadorProfileDetail"] is not None

    @pytest.mark.asyncio
    async def test_gig_history_scoped_to_active_tenant(self):
        # BA worked in BOTH tenants; viewing under A shows only A's gig.
        ba = await sync_to_async(self._make_ba)("multi")
        await sync_to_async(self._gig)(ba, self.tenant_a, "A Gig")
        await sync_to_async(self._gig)(ba, self.tenant_b, "B Gig")

        result = await self._execute_mutation(
            QUERY,
            {"uuid": str(ba.uuid), "tenantId": str(self.tenant_a.id)},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None, f"errored: {result.errors}"
        detail = result.data["ambassadorProfileDetail"]
        venues = {g["venue"] for g in detail["gigHistory"]}
        assert venues == {"A Gig"}
