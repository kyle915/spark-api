"""
Client-visibility coverage for the recap list resolvers (`recaps` and
`customRecaps` on the clients schema).

Drafts/pending recaps must NOT be visible on the client-facing recap
surface — only APPROVED recaps. Admin/RMM surfaces MUST still see drafts.
The recap "approved" state is a BooleanField on Recap/CustomRecap
(`approved`, default False); there is no separate draft/pending enum — an
unapproved recap (`approved=False`) is the draft, an approved one
(`approved=True`) is client-visible.

Two enforcement paths exist and are both covered here:
  1. The client VIEW (an Ignite admin toggled into client mode in the web
     UI) passes `filters.approved = true`, and the resolver honours it →
     drafts excluded. The admin/RMM view passes no `approved` filter →
     drafts included.
  2. A real client-role login is forced to approved-only by the server
     (`_is_client_only_user`) even if no `approved` filter is sent — a
     stale frontend can't expose drafts to an actual client.

Tests assert:
- admin with no approved filter sees BOTH approved and draft recaps;
- admin passing approved=true (the client view) sees ONLY approved;
- a real client user sees ONLY approved even with no filter;
- the same for customRecaps.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


RECAPS_QUERY = """
query Recaps($tenantId: ID, $approved: Boolean, $first: Int) {
  recaps(filters: { tenantId: $tenantId, approved: $approved }, first: $first) {
    totalCount
    edges { node { uuid name approved } }
  }
}
"""

CUSTOM_RECAPS_QUERY = """
query CustomRecaps($tenantId: ID, $approved: Boolean, $first: Int) {
  customRecaps(filters: { tenantId: $tenantId, approved: $approved }, first: $first) {
    totalCount
    edges { node { uuid name approved } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapClientApprovedOnly(AmbassadorsGraphQLTestCase):
    """Client recap surface = approved-only; admin/RMM still sees drafts."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")

        self.spark_admin = self.create_user(
            username="admin-approved-only",
            email="admin-approved-only@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-approved-only",
            email="client-approved-only@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )

        # One approved recap, one draft (unapproved) recap.
        self.approved_recap = recap_models.Recap.objects.create(
            name="Approved recap",
            approved=True,
            event=self.event,
            created_by=system_user,
            updated_by=system_user,
        )
        self.draft_recap = recap_models.Recap.objects.create(
            name="Draft recap",
            approved=False,
            event=self.event,
            created_by=system_user,
            updated_by=system_user,
        )

        # Same split for custom recaps.
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=system_user,
        )
        self.approved_custom = recap_models.CustomRecap.objects.create(
            name="Approved custom recap",
            approved=True,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=system_user,
            updated_by=system_user,
        )
        self.draft_custom = recap_models.CustomRecap.objects.create(
            name="Draft custom recap",
            approved=False,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=system_user,
            updated_by=system_user,
        )

    @pytest.mark.asyncio
    async def test_admin_no_filter_sees_drafts(self):
        # Admin / RMM surface: no approved filter => drafts ARE visible.
        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Approved recap", "Draft recap"}, names
        assert conn["totalCount"] == 2

    @pytest.mark.asyncio
    async def test_admin_client_view_sees_only_approved(self):
        # Client view (admin toggled to client mode) passes approved=true.
        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "approved": True, "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Approved recap"}, names
        assert "Draft recap" not in names
        assert conn["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_real_client_user_sees_only_approved_without_filter(self):
        # A genuine client login is forced approved-only server-side even
        # with NO approved filter — a stale frontend can't leak drafts.
        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {"first": 50},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Approved recap"}, names
        assert "Draft recap" not in names
        assert conn["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_custom_admin_no_filter_sees_drafts(self):
        result = await self._execute_query_authenticated(
            CUSTOM_RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Approved custom recap", "Draft custom recap"}, names
        assert conn["totalCount"] == 2

    @pytest.mark.asyncio
    async def test_custom_client_view_sees_only_approved(self):
        result = await self._execute_query_authenticated(
            CUSTOM_RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "approved": True, "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Approved custom recap"}, names
        assert conn["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_custom_real_client_user_sees_only_approved(self):
        result = await self._execute_query_authenticated(
            CUSTOM_RECAPS_QUERY,
            {"first": 50},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Approved custom recap"}, names
        assert conn["totalCount"] == 1
