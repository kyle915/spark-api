"""Cross-tenant isolation tests for the Announcement clients-schema ops.

Proves the tenant-scoping fix on the CLIENTS schema (the same bug class as
the Favorites leak fixed in PR #692): a client-role caller can neither read
another tenant's announcements, broadcast an announcement (with its BA push
fan-out) to another tenant, nor delete another tenant's announcement by
supplying that tenant's ``tenantId`` / announcement ``uuid``. Same-tenant
and admin cross-tenant access still work.

Mirrors ``jobs/tests/test_favorite_ambassadors_graphql.py``.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from announcements.models import Announcement
from config.schema_client import schema_clients
from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()


ADMIN_LIST_QUERY = """
query AdminAnnouncements($filters: AnnouncementFiltersInput) {
  announcementsAdmin(filters: $filters) {
    uuid
    title
    tenantId
  }
}
"""

CREATE_MUTATION = """
mutation Create($input: CreateAnnouncementInput!) {
  createAnnouncement(input: $input) {
    success
    message
    announcement { uuid title tenantId }
  }
}
"""

DELETE_MUTATION = """
mutation Delete($input: DeleteAnnouncementInput!) {
  deleteAnnouncement(input: $input) {
    success
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestAnnouncementTenantIsolationGraphQL(BaseGraphQLTestCase):
    """Cross-tenant isolation for announcementsAdmin / createAnnouncement /
    deleteAnnouncement on the clients schema."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Ann Mine")
        self.other_tenant = self.create_tenant(name="Ann Theirs")

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

    async def _announcement(self, tenant, title="Ann") -> Announcement:
        return await sync_to_async(Announcement.objects.create)(
            tenant=tenant, title=title, published_at=timezone.now()
        )

    # -- reads ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_list_pinned_to_own_tenant(self):
        """A client passing another tenant's id is pinned to their own tenant."""
        user = await self._client_user_for("an-list", "anlist@test.com", self.tenant)
        await self._announcement(self.tenant, "Mine A")
        await self._announcement(self.other_tenant, "Theirs A")

        result = await self._execute_mutation(
            ADMIN_LIST_QUERY,
            {"filters": {"tenantId": str(self.other_tenant.id)}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        rows = result.data["announcementsAdmin"]
        assert [r["title"] for r in rows] == ["Mine A"]
        assert all(r["tenantId"] == str(self.tenant.id) for r in rows)

    # -- writes --------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_create_pinned_to_own_tenant(self):
        """createAnnouncement with another tenant's id posts to the caller's
        OWN tenant — a client can never broadcast to another brand's BAs."""
        user = await self._client_user_for("an-add", "anadd@test.com", self.tenant)

        result = await self._execute_mutation(
            CREATE_MUTATION,
            {
                "input": {
                    "title": "Injected Broadcast",
                    "tenantId": str(self.other_tenant.id),
                }
            },
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        payload = result.data["createAnnouncement"]
        assert payload["success"] is True
        assert payload["announcement"]["tenantId"] == str(self.tenant.id)

        # Nothing landed on (was broadcast to) the targeted other tenant.
        leaked = await sync_to_async(
            Announcement.objects.filter(tenant_id=self.other_tenant.id).exists
        )()
        assert leaked is False

    @pytest.mark.asyncio
    async def test_client_cannot_delete_other_tenant_announcement(self):
        """deleteAnnouncement of another tenant's row is denied; row survives."""
        user = await self._client_user_for("an-del", "andel@test.com", self.tenant)
        theirs = await self._announcement(self.other_tenant, "Keep Me")

        result = await self._execute_mutation(
            DELETE_MUTATION,
            {"input": {"uuid": str(theirs.uuid)}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["deleteAnnouncement"]["success"] is False

        survived = await sync_to_async(
            Announcement.objects.filter(uuid=theirs.uuid).exists
        )()
        assert survived is True

    @pytest.mark.asyncio
    async def test_client_can_delete_own_tenant_announcement(self):
        user = await self._client_user_for("an-del2", "andel2@test.com", self.tenant)
        mine = await self._announcement(self.tenant, "Mine Bye")

        result = await self._execute_mutation(
            DELETE_MUTATION,
            {"input": {"uuid": str(mine.uuid)}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["deleteAnnouncement"]["success"] is True
        gone = await sync_to_async(
            Announcement.objects.filter(uuid=mine.uuid).exists
        )()
        assert gone is False

    # -- admin ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_admin_can_target_any_tenant(self):
        """A spark-admin may list + post + delete any tenant's announcements."""
        admin = await self._admin_user("an-admin", "anadmin@test.com")
        theirs = await self._announcement(self.other_tenant, "Their Ann")

        list_result = await self._execute_mutation(
            ADMIN_LIST_QUERY,
            {"filters": {"tenantId": str(self.other_tenant.id)}},
            self.endpoint_path,
            user=admin,
        )
        assert list_result.errors is None
        assert [r["title"] for r in list_result.data["announcementsAdmin"]] == [
            "Their Ann"
        ]

        create_result = await self._execute_mutation(
            CREATE_MUTATION,
            {
                "input": {
                    "title": "Admin Broadcast",
                    "tenantId": str(self.other_tenant.id),
                }
            },
            self.endpoint_path,
            user=admin,
        )
        assert create_result.errors is None
        assert create_result.data["createAnnouncement"]["success"] is True
        assert (
            create_result.data["createAnnouncement"]["announcement"]["tenantId"]
            == str(self.other_tenant.id)
        )

        delete_result = await self._execute_mutation(
            DELETE_MUTATION,
            {"input": {"uuid": str(theirs.uuid)}},
            self.endpoint_path,
            user=admin,
        )
        assert delete_result.errors is None
        assert delete_result.data["deleteAnnouncement"]["success"] is True
