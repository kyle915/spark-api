"""
inviteUser / deleteUser are Spark-admin only (same DB-backed gate as
inviteClientUsers). Anonymous, ambassador and client callers are refused
and nothing is created, reactivated, deactivated or emailed. Run through
the real spark schema; mailers are patched so no mail is sent.
"""

from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from tenants.models import TenantedUser
from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()

INVITE = """
mutation Invite($input: InviteUserInput!) {
  inviteUser(input: $input) { success message }
}
"""

DELETE = """
mutation Delete($input: DeleteUserInput!) {
  deleteUser(input: $input) { success message }
}
"""


@pytest.mark.django_db(transaction=True)
class TestUserAdminGates(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Gate Brand")
        self.admin = self.create_user(
            username="ugate-admin", email="ugate-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="ugate-client", email="ugate-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.ba = self.create_user(
            username="ugate-ba", email="ugate-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba, self.tenant)
        self.victim = self.create_user(
            username="ugate-victim", email="ugate-victim@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.victim, self.tenant)
        self.deactivated = self.create_user(
            username="ugate-gone", email="ugate-gone@test.com",
            role=self.roles["client"], is_active=False,
        )

    def _patched_mailers(self):
        magic = patch("tenants.mutations.MagicLinkMailer")
        invite = patch("tenants.mutations.ClientInviteMailer")
        return magic, invite

    async def _invite(self, user, email="new-admin@evil.test", role="admin"):
        magic_p, invite_p = self._patched_mailers()
        with magic_p as magic_cls, invite_p as invite_cls:
            magic_cls.return_value.send_async_now = AsyncMock()
            invite_cls.return_value.send_async_now = AsyncMock()
            result = await self._execute_mutation(
                INVITE,
                {"input": {"email": email, "role": role}},
                self.endpoint_path,
                user=user,
            )
            sent = (
                magic_cls.return_value.send_async_now.await_count
                + invite_cls.return_value.send_async_now.await_count
            )
        return result, sent

    # ── inviteUser ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "ba", "client"])
    async def test_invite_refused_for_non_admins(self, who):
        user = {"anon": None, "ba": self.ba, "client": self.client_user}[who]
        result, sent = await self._invite(user)
        assert result.errors is None
        assert result.data["inviteUser"]["success"] is False
        assert sent == 0
        exists = await sync_to_async(
            User.objects.filter(email__iexact="new-admin@evil.test").exists
        )()
        assert not exists

    @pytest.mark.asyncio
    async def test_invite_cannot_reactivate_for_non_admin(self):
        result, sent = await self._invite(None, email="ugate-gone@test.com")
        assert result.data["inviteUser"]["success"] is False
        assert sent == 0
        await sync_to_async(self.deactivated.refresh_from_db)()
        assert self.deactivated.is_active is False

    @pytest.mark.asyncio
    async def test_admin_can_invite(self):
        result, sent = await self._invite(self.admin)
        assert result.errors is None
        assert result.data["inviteUser"]["success"] is True, result.data
        assert sent == 1
        created = await sync_to_async(
            lambda: User.objects.get(email__iexact="new-admin@evil.test")
        )()
        assert created.role_id == 2
        links = await sync_to_async(
            TenantedUser.objects.filter(user=created).count
        )()
        assert links >= 1

    # ── deleteUser ───────────────────────────────────────────────────

    async def _delete(self, user, target):
        return await self._execute_mutation(
            DELETE, {"input": {"userId": str(target.id)}}, self.endpoint_path,
            user=user,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "ba", "client"])
    async def test_delete_refused_for_non_admins(self, who):
        user = {"anon": None, "ba": self.ba, "client": self.client_user}[who]
        result = await self._delete(user, self.victim)
        assert result.errors is None
        assert result.data["deleteUser"]["success"] is False
        await sync_to_async(self.victim.refresh_from_db)()
        assert self.victim.is_active is True

    @pytest.mark.asyncio
    async def test_admin_can_delete(self):
        result = await self._delete(self.admin, self.victim)
        assert result.errors is None
        assert result.data["deleteUser"]["success"] is True, result.data
        await sync_to_async(self.victim.refresh_from_db)()
        assert self.victim.is_active is False

    @pytest.mark.asyncio
    async def test_admin_cannot_delete_self(self):
        result = await self._delete(self.admin, self.admin)
        assert result.data["deleteUser"]["success"] is False
        assert result.data["deleteUser"]["message"] == "You can't deactivate yourself."
        await sync_to_async(self.admin.refresh_from_db)()
        assert self.admin.is_active is True
