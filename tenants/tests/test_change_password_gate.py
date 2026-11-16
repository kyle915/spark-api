"""
changeUserPassword (spark/clients schema): anyone signed in may change their
OWN password; changing someone else's is Spark-admin only.

Before: a client could reset the password of any user sharing one of their
tenants (other client users, BAs on the roster, Ignite staff auto-linked to
the brand) and then sign in as them. Run through the real clients schema.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()

CHANGE = """
mutation Change($input: ChangeUserPasswordInput!) {
  changeUserPassword(input: $input) { success message }
}
"""

NEW_PW = "N3w-pass-word!"


@pytest.mark.django_db(transaction=True)
class TestChangeUserPasswordGate(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Pw Brand")
        self.other_tenant = self.create_tenant(name="Pw Other Brand")

        self.admin = self.create_user(
            username="pw-admin", email="pw-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="pw-client", email="pw-client@test.com",
            role=self.roles["client"], password="old-pass-1",
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        # Same-tenant colleague and BA: the old gate let the client reset these.
        self.colleague = self.create_user(
            username="pw-colleague", email="pw-colleague@test.com",
            role=self.roles["client"], password="old-pass-2",
        )
        self.create_tenanted_user(self.colleague, self.tenant)
        self.ba = self.create_user(
            username="pw-ba", email="pw-ba@test.com",
            role=self.roles["ambassador"], password="old-pass-3",
        )
        self.create_tenanted_user(self.ba, self.tenant)
        self.outsider = self.create_user(
            username="pw-outsider", email="pw-outsider@test.com",
            role=self.roles["client"], password="old-pass-4",
        )
        self.create_tenanted_user(self.outsider, self.other_tenant)

    async def _change(self, caller, target):
        return await self._execute_mutation(
            CHANGE,
            {"input": {"id": str(target.id), "password1": NEW_PW, "password2": NEW_PW}},
            self.endpoint_path,
            user=caller,
        )

    async def _has_new_password(self, user):
        fresh = await sync_to_async(User.objects.get)(pk=user.pk)
        return fresh.check_password(NEW_PW)

    @pytest.mark.asyncio
    async def test_anonymous_refused(self):
        result = await self._change(None, self.colleague)
        assert result.data["changeUserPassword"]["success"] is False
        assert not await self._has_new_password(self.colleague)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["colleague", "ba", "admin"])
    async def test_client_cannot_change_same_tenant_users(self, target):
        victim = getattr(self, target)
        result = await self._change(self.client_user, victim)
        assert result.errors is None, result.errors
        payload = result.data["changeUserPassword"]
        assert payload["success"] is False
        assert payload["message"] == "You can only change your own password."
        assert not await self._has_new_password(victim)

    @pytest.mark.asyncio
    async def test_client_cannot_change_other_tenant_user(self):
        result = await self._change(self.client_user, self.outsider)
        assert result.data["changeUserPassword"]["success"] is False
        assert not await self._has_new_password(self.outsider)

    @pytest.mark.asyncio
    async def test_non_admin_probe_gets_same_message_for_missing_user(self):
        result = await self._execute_mutation(
            CHANGE,
            {"input": {"id": "987654321", "password1": NEW_PW, "password2": NEW_PW}},
            self.endpoint_path,
            user=self.client_user,
        )
        assert result.data["changeUserPassword"]["message"] == (
            "You can only change your own password."
        )

    @pytest.mark.asyncio
    async def test_client_can_change_own_password(self):
        result = await self._change(self.client_user, self.client_user)
        assert result.data["changeUserPassword"]["success"] is True, result.data
        assert await self._has_new_password(self.client_user)

    @pytest.mark.asyncio
    async def test_ba_can_change_own_but_not_others(self):
        own = await self._change(self.ba, self.ba)
        assert own.data["changeUserPassword"]["success"] is True
        other = await self._change(self.ba, self.colleague)
        assert other.data["changeUserPassword"]["success"] is False
        assert not await self._has_new_password(self.colleague)

    @pytest.mark.asyncio
    async def test_admin_can_change_any_user(self):
        result = await self._change(self.admin, self.outsider)
        assert result.data["changeUserPassword"]["success"] is True, result.data
        assert await self._has_new_password(self.outsider)

    @pytest.mark.asyncio
    async def test_admin_with_unhydrated_role_is_not_spuriously_denied(self):
        self.admin.role = None
        result = await self._change(self.admin, self.colleague)
        assert result.data["changeUserPassword"]["success"] is True, result.data
