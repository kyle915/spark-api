"""Magic-link sign-in requires a verified account, like tokenAuth.

Consuming a link must not verify the account either: someone could register
an address they don't own with a password of their own, and the owner's
click would then hand that password a verified login to the owner's account.
"""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async
from django.core import signing
from gqlauth.models import UserStatus

from tenants.mutations import (
    CLIENT_INVITE_SALT,
    MAGIC_LINK_SALT,
    _ensure_verified,
)
from tenants.tests.base import BaseGraphQLTestCase

LOGIN = """
mutation Login($input: LoginWithMagicTokenInput!) {
    loginWithMagicToken(input: $input) { success message token }
}
"""


@pytest.mark.django_db(transaction=True)
class TestMagicLinkNeedsVerifiedAccount(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    async def _user(self, email: str, *, verified: bool):
        user = await sync_to_async(self.create_user)(
            username=email, email=email, role=self.roles["client"]
        )
        await sync_to_async(UserStatus.objects.update_or_create)(
            user=user, defaults={"verified": verified}
        )
        return user

    async def _login(self, token: str) -> dict:
        result = await self._execute_mutation(LOGIN, {"input": {"token": token}}, self.endpoint_path)
        assert result.errors is None
        return result.data["loginWithMagicToken"]

    @pytest.mark.asyncio
    async def test_unverified_user_gets_no_session_and_stays_unverified(self):
        user = await self._user("self-signup@client.com", verified=False)
        for salt in (MAGIC_LINK_SALT, CLIENT_INVITE_SALT):
            token = signing.dumps({"u": user.id, "e": user.email}, salt=salt)
            payload = await self._login(token)
            assert payload["success"] is False
            assert payload["token"] is None
            assert "verify your account" in payload["message"]
        status = await sync_to_async(UserStatus.objects.get)(user=user)
        assert status.verified is False

    @pytest.mark.asyncio
    async def test_missing_status_counts_as_unverified(self):
        user = await self._user("nostatus@client.com", verified=False)
        await sync_to_async(UserStatus.objects.filter(user=user).delete)()
        token = signing.dumps({"u": user.id, "e": user.email}, salt=MAGIC_LINK_SALT)
        assert (await self._login(token))["success"] is False

    @pytest.mark.asyncio
    async def test_verified_user_still_signs_in(self):
        user = await self._user("verified@client.com", verified=True)
        token = signing.dumps({"u": user.id, "e": user.email}, salt=MAGIC_LINK_SALT)
        payload = await self._login(token)
        assert payload["success"] is True and payload["token"].count(".") == 2

    @pytest.mark.asyncio
    async def test_invited_user_is_verified_by_the_invite(self):
        """The invite path verifies at invite time, so its link still works."""
        user = await self._user("invited@client.com", verified=False)
        await sync_to_async(_ensure_verified)(user)
        token = signing.dumps({"u": user.id, "e": user.email, "k": "invite"}, salt=CLIENT_INVITE_SALT)
        assert (await self._login(token))["success"] is True
