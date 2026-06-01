"""Cross-tenant isolation tests for ``jobBriefingForEvent``.

The last gap in the clients/mobile-schema tenant-isolation sweep (rounds 1-2
fixed in PRs #694 / #695): ``jobBriefingForEvent`` reads an event's job
briefing keyed by a bare event UUID with NO authorization beyond
``StrictIsAuthenticated`` — so any authenticated user (a BA or a client of ANY
tenant) could read ANY event's briefing (brand/products/instructions),
cross-tenant.

Unlike the sibling pk-addressed ``jobBriefing`` (a blunt tenant filter), this
query is the BA-mobile shift-offer entry point: a BA who's been OFFERED a shift
must still be able to read its briefing even though they don't belong to the
event's tenant. So the fix is a caller-aware gate
(``jobs.job_scope.JobScope.can_read_event_briefing``):

  * admin (spark-admin / staff / superuser / ``@igniteproductions.co``) -> any
  * tenant member (client) -> only their OWN tenant's events
  * BA -> only an event their ambassador is linked to via ``AmbassadorEvent``
    (offered ``is_approved=False``, assigned, or on-roster)
  * otherwise / out-of-scope / unauthenticated -> null

Mirrors ``jobs/tests/test_jobs_tenant_isolation_graphql.py``.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from config.schema_client import schema_clients
from jobs import models
from jobs.tests.base import JobsGraphQLTestCase
from ambassadors.models import AmbassadorEvent

User = get_user_model()


JOB_BRIEFING_FOR_EVENT_QUERY = """
query JobBriefingForEvent($eventUuid: ID!) {
  jobBriefingForEvent(eventUuid: $eventUuid) { title body }
}
"""


@pytest.mark.django_db(transaction=True)
class TestJobBriefingForEventIsolationGraphQL(JobsGraphQLTestCase):
    """Caller-aware authorization for ``jobBriefingForEvent``."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Briefing Mine")
        self.other_tenant = self.create_tenant(name="Briefing Theirs")

    # -- fixtures ------------------------------------------------------------

    async def _client_user_for(self, username, email, tenant) -> User:
        user = await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["client"],
            password="password123",
        )
        await sync_to_async(self.create_tenanted_user)(user=user, tenant=tenant)
        return user

    async def _admin_user(self, username, email) -> User:
        return await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["spark_admin"],
            password="password123",
        )

    async def _ambassador_user(self, username, email):
        ba_user = await sync_to_async(self.create_user)(
            username=username, email=email, role=self.roles["ambassador"],
            password="password123", first_name="Bay", last_name="Area",
        )
        ambassador = await sync_to_async(self.create_ambassador)(user=ba_user)
        return ba_user, ambassador

    async def _event_with_briefing(
        self, tenant, *, title="Secret Brief", body="Secret Body", code="JB1"
    ):
        """An Event with a posted Job carrying a briefing, for the tenant."""
        event = await sync_to_async(self.create_event)(
            name="Brief Event", tenant=tenant, address="123 St",
            date=timezone.now(),
        )
        job_title = await sync_to_async(self.create_job_title)(
            name="Brief Title", tenant=tenant
        )
        await sync_to_async(self.create_job)(
            name="Gig",
            code=code,
            address="123 St",
            event=event,
            job_title=job_title,
            tenant=tenant,
            briefing_title=title,
            briefing_body=body,
        )
        return event

    async def _offer_to(self, ambassador, event, tenant, *, is_approved=False):
        """Link a BA to an event via AmbassadorEvent — a shift OFFER when
        ``is_approved=False`` (the BA hasn't accepted yet)."""
        system_user = await sync_to_async(self.get_system_user)()
        return await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=ambassador,
            event=event,
            tenant=tenant,
            is_approved=is_approved,
            created_by=system_user,
        )

    # == BA (ambassador) =====================================================

    @pytest.mark.asyncio
    async def test_ba_offered_can_read_briefing(self):
        """The core mobile flow: a BA who's been OFFERED a shift (an
        AmbassadorEvent with is_approved=False) can read its briefing even
        though they don't belong to the event's tenant."""
        ba_user, amb = await self._ambassador_user("ba-off", "baoff@test.com")
        event = await self._event_with_briefing(
            self.tenant, title="Offer Brief", body="Offer Body"
        )
        await self._offer_to(amb, event, self.tenant, is_approved=False)

        result = await self._execute_mutation(
            JOB_BRIEFING_FOR_EVENT_QUERY,
            {"eventUuid": str(event.uuid)},
            self.endpoint_path, user=ba_user,
        )
        assert result.errors is None
        assert result.data["jobBriefingForEvent"] is not None
        assert result.data["jobBriefingForEvent"]["title"] == "Offer Brief"
        assert result.data["jobBriefingForEvent"]["body"] == "Offer Body"

    @pytest.mark.asyncio
    async def test_ba_rostered_can_read_briefing(self):
        """A BA who's accepted / is on-roster (is_approved=True) can read it."""
        ba_user, amb = await self._ambassador_user("ba-ros", "baros@test.com")
        event = await self._event_with_briefing(self.tenant, title="Roster Brief")
        await self._offer_to(amb, event, self.tenant, is_approved=True)

        result = await self._execute_mutation(
            JOB_BRIEFING_FOR_EVENT_QUERY,
            {"eventUuid": str(event.uuid)},
            self.endpoint_path, user=ba_user,
        )
        assert result.errors is None
        assert result.data["jobBriefingForEvent"]["title"] == "Roster Brief"

    @pytest.mark.asyncio
    async def test_ba_not_linked_gets_null(self):
        """THE FIX: a BA with no AmbassadorEvent for the event cannot read
        its briefing, even though they're authenticated."""
        ba_user, _amb = await self._ambassador_user("ba-no", "bano@test.com")
        event = await self._event_with_briefing(
            self.tenant, title="Not For You", body="Hidden Body"
        )
        # No AmbassadorEvent linking this BA to the event.

        result = await self._execute_mutation(
            JOB_BRIEFING_FOR_EVENT_QUERY,
            {"eventUuid": str(event.uuid)},
            self.endpoint_path, user=ba_user,
        )
        assert result.errors is None
        assert result.data["jobBriefingForEvent"] is None

    @pytest.mark.asyncio
    async def test_ba_offered_other_event_cannot_read_this_one(self):
        """A BA offered event A can't read event B's briefing by swapping
        the UUID — the link is per-event, not per-BA."""
        ba_user, amb = await self._ambassador_user("ba-sw", "basw@test.com")
        offered = await self._event_with_briefing(self.tenant, code="JB-A")
        other = await self._event_with_briefing(
            self.other_tenant, title="Other Brief", code="JB-B"
        )
        await self._offer_to(amb, offered, self.tenant, is_approved=False)

        result = await self._execute_mutation(
            JOB_BRIEFING_FOR_EVENT_QUERY,
            {"eventUuid": str(other.uuid)},
            self.endpoint_path, user=ba_user,
        )
        assert result.errors is None
        assert result.data["jobBriefingForEvent"] is None

    # == tenant member (client) ==============================================

    @pytest.mark.asyncio
    async def test_client_can_read_own_tenant_event_briefing(self):
        user = await self._client_user_for("cl-o", "clo@test.com", self.tenant)
        event = await self._event_with_briefing(self.tenant, title="Mine")

        result = await self._execute_mutation(
            JOB_BRIEFING_FOR_EVENT_QUERY,
            {"eventUuid": str(event.uuid)},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["jobBriefingForEvent"]["title"] == "Mine"

    @pytest.mark.asyncio
    async def test_client_cannot_read_other_tenant_event_briefing(self):
        """THE FIX: a client of tenant A cannot read tenant B's event
        briefing by holding/guessing its event UUID."""
        user = await self._client_user_for("cl-x", "clx@test.com", self.tenant)
        event = await self._event_with_briefing(
            self.other_tenant, title="Secret", body="Secret Body"
        )

        result = await self._execute_mutation(
            JOB_BRIEFING_FOR_EVENT_QUERY,
            {"eventUuid": str(event.uuid)},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["jobBriefingForEvent"] is None

    # == admin ===============================================================

    @pytest.mark.asyncio
    async def test_admin_can_read_any_tenant_event_briefing(self):
        admin = await self._admin_user("ad-a", "ada@test.com")
        event = await self._event_with_briefing(
            self.other_tenant, title="Admin Sees This"
        )

        result = await self._execute_mutation(
            JOB_BRIEFING_FOR_EVENT_QUERY,
            {"eventUuid": str(event.uuid)},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        assert result.data["jobBriefingForEvent"]["title"] == "Admin Sees This"

    # == unauthenticated =====================================================

    @pytest.mark.asyncio
    async def test_unauthenticated_is_denied(self):
        """No user -> StrictIsAuthenticated denies; the field resolves to
        null and never leaks the briefing."""
        event = await self._event_with_briefing(self.tenant, title="NoAnon")

        result = await self._execute_mutation(
            JOB_BRIEFING_FOR_EVENT_QUERY,
            {"eventUuid": str(event.uuid)},
            self.endpoint_path, user=None,
        )
        # StrictIsAuthenticated surfaces as an error and/or a null field;
        # either way the briefing must NOT be returned.
        assert result.data is None or result.data.get("jobBriefingForEvent") is None
