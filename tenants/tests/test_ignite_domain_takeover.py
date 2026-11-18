"""Self-serve paths must never produce a token that passes the Ignite-admin gate.

The admin gate (``_is_admin_access``) used to grant every
``@igniteproductions.co`` address full cross-tenant access on the email
alone, and several public, unauthenticated mutations let anyone create an
account (and get a working session) for an address they do not own, or pick
the ``spark-admin`` role outright. Each test below drives a real HTTP request
through the real GraphQL endpoints, takes whatever token the path yields, and
asks the clients schema for ``systemHealth`` — an Ignite-admin-only field that
returns null for everyone else. Every one of these must come back null (or the
path must refuse to create the account at all).

The last group proves real Ignite staff keep admin: spark-admin role,
is_staff, and a verified client-role @igniteproductions.co account.
"""

from __future__ import annotations

import base64
import json

import pytest
from django.test import AsyncClient

from gqlauth.models import UserStatus
from tenants.models import User
from tenants.tests.base import BaseGraphQLTestCase

CLIENTS = "/api/v615204/graphql/clients"
SPARK = "/api/v483927/graphql/spark"
AMBASSADORS = "/api/v839471/graphql/ambassadors"
MOBILE = "/api/v270986/graphql/mobile"

PW = "Correct-Horse-Battery-9"

SYSTEM_HEALTH = "query { systemHealth { databaseOk } }"

TOKEN_AUTH = """
mutation T($input: ObtainJSONWebTokenInput!) {
  tokenAuth(input: $input) {
    success errors token { token }
  }
}
"""

VERIFY_ACCOUNT = """
mutation V($input: VerifyAccountInput!) { verifyAccount(input: $input) { success errors } }
"""

CREATE_PUBLIC = """
mutation C($input: CreatePublicAmbassadorInput!) {
  createPublicAmbassador(input: $input) { success message activationToken }
}
"""

CREATE_WITH_USER = """
mutation C($input: CreateAmbassadorWithUserInput!) {
  createAmbassadorWithUser(input: $input) { success message }
}
"""

WALKUP_SIGNUP = """
mutation W($input: CreatePublicAmbassadorInput!) {
  walkupSignUp(input: $input) { success message token }
}
"""

AMB_REGISTER = """
mutation R($input: AmbassadorRegisterInput!) {
  register(input: $input) { success message activationToken }
}
"""

CLIENT_REGISTER = """
mutation R($input: ClientRegisterInput!) {
  register(input: $input) { success message activationToken }
}
"""

SPARK_REGISTER = """
mutation R($input: BaseRegisterInput!) {
  register(input: $input) { success message activationToken }
}
"""

APPLE_LEGACY = """
mutation A($input: AppleSocialAuthInput!) {
  socialAuthApple(input: $input) { success message token }
}
"""


async def gql(path, query, variables=None, token=None):
    client = AsyncClient()
    headers = {"Authorization": f"JWT {token}"} if token else {}
    resp = await client.post(
        path,
        data=json.dumps({"query": query, "variables": variables or {}}),
        content_type="application/json",
        headers=headers,
    )
    return resp.json()


async def is_admin_with(token) -> bool:
    body = await gql(CLIENTS, SYSTEM_HEALTH, token=token)
    return bool((body.get("data") or {}).get("systemHealth"))


async def password_login(email) -> str | None:
    body = await gql(CLIENTS, TOKEN_AUTH, {"input": {"username": email, "password": PW}})
    data = (body.get("data") or {}).get("tokenAuth") or {}
    return ((data.get("token") or {}) or {}).get("token")


def signup_input(email, **extra):
    return {
        "firstName": "Eve",
        "email": email,
        "password1": PW,
        "password2": PW,
        **extra,
    }


def forged_apple_token(email: str) -> str:
    def b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'email': email, 'sub': 'x'})}.sig"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestIgniteDomainTakeover(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, transactional_db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Victim Brand")
        # Kyle's real shape: a verified client-role @igniteproductions.co
        # account with no spark-admin role or staff flag — admin purely by
        # domain, which must keep working.
        self.kyle = _make_user(
            "kyle@igniteproductions.co", self.roles["client"], verified=True
        )

    # --- 1. public BA signup + returned activation token ---------------

    async def test_public_signup_cannot_take_ignite_address(self):
        email = "eve@igniteproductions.co"
        body = await gql(AMBASSADORS, CREATE_PUBLIC, {"input": signup_input(email)})
        res = body["data"]["createPublicAmbassador"]
        if res["success"]:
            token = res.get("activationToken")
            if token:
                await gql(AMBASSADORS, VERIFY_ACCOUNT, {"input": {"token": token}})
            jwt = await password_login(email)
            assert not (jwt and await is_admin_with(jwt)), (
                "public signup on the Ignite domain reached admin"
            )
            pytest.fail("public signup accepted an @igniteproductions.co email")
        assert not await User.objects.filter(email__iexact=email).aexists()

    async def test_public_signup_never_returns_activation_token(self):
        body = await gql(
            AMBASSADORS, CREATE_PUBLIC, {"input": signup_input("bob@example.com")}
        )
        res = body["data"]["createPublicAmbassador"]
        assert res["success"], res
        assert res["activationToken"] is None

    @pytest.mark.parametrize(
        "email",
        [
            "Eve@IgniteProductions.CO",
            "eve@igniteproductions.co.",
            " eve@igniteproductions.co ",
            "eve+x@IGNITEPRODUCTIONS.co",
            "eve@mail.igniteproductions.co",
        ],
    )
    async def test_public_signup_rejects_domain_lookalikes(self, email):
        body = await gql(AMBASSADORS, CREATE_PUBLIC, {"input": signup_input(email)})
        assert body["data"]["createPublicAmbassador"]["success"] is False
        assert not await User.objects.filter(email__icontains="igniteproductions").exclude(
            pk=self.kyle.pk
        ).aexists()

    async def test_public_signup_cannot_shadow_existing_staff_by_case(self):
        body = await gql(
            AMBASSADORS,
            CREATE_PUBLIC,
            {"input": signup_input("KYLE@igniteproductions.co")},
        )
        assert body["data"]["createPublicAmbassador"]["success"] is False
        assert await User.objects.filter(email__iexact="kyle@igniteproductions.co").acount() == 1

    # --- 2. walk-up signup (issues a session token immediately) ---------

    async def test_walkup_signup_cannot_take_ignite_address(self):
        email = "eve@igniteproductions.co"
        body = await gql(MOBILE, WALKUP_SIGNUP, {"input": signup_input(email)})
        res = body["data"]["walkupSignUp"]
        if res.get("token"):
            assert not await is_admin_with(res["token"]), (
                "walk-up signup token on the Ignite domain reached admin"
            )
        assert res["success"] is False
        assert res.get("token") is None

    # --- 3. an ambassador token creating a verified account -------------

    async def test_ambassador_cannot_mint_verified_accounts(self):
        body = await gql(
            MOBILE, WALKUP_SIGNUP, {"input": signup_input("walker@example.com")}
        )
        ba_token = body["data"]["walkupSignUp"]["token"]
        assert ba_token
        email = "eve2@igniteproductions.co"
        res = await gql(
            CLIENTS,
            CREATE_WITH_USER,
            {"input": signup_input(email)},
            token=ba_token,
        )
        jwt = await password_login(email)
        assert not (jwt and await is_admin_with(jwt)), (
            "BA-created account on the Ignite domain reached admin"
        )
        assert not (res.get("data") or {}).get("createAmbassadorWithUser", {}).get(
            "success"
        )

    # --- 4. anonymous register mutations that pick the role -------------

    @pytest.mark.parametrize("path", [AMBASSADORS, MOBILE])
    async def test_anonymous_ambassador_register_cannot_pick_spark_admin(self, path):
        email = "eve3@example.com"
        body = await gql(
            path,
            AMB_REGISTER,
            {"input": {**signup_input(email), "role": "SPARK"}},
        )
        res = (body.get("data") or {}).get("register") or {}
        token = res.get("activationToken")
        if token:
            await gql(path, VERIFY_ACCOUNT, {"input": {"token": token}})
        jwt = await password_login(email)
        assert not (jwt and await is_admin_with(jwt)), (
            "anonymous register(role: SPARK) reached admin"
        )
        user = await User.objects.select_related("role").filter(email=email).afirst()
        assert user is None or user.role.slug == "ambassador"

    async def test_anonymous_clients_register_cannot_pick_spark_admin(self):
        email = "eve4@example.com"
        body = await gql(
            CLIENTS,
            CLIENT_REGISTER,
            {"input": {**signup_input(email), "role": "SPARK"}},
        )
        res = (body.get("data") or {}).get("register") or {}
        assert not res.get("activationToken")
        if res.get("activationToken"):
            await gql(CLIENTS, VERIFY_ACCOUNT, {"input": {"token": res["activationToken"]}})
        jwt = await password_login(email)
        assert not (jwt and await is_admin_with(jwt))

    async def test_anonymous_spark_register_is_refused(self):
        email = "eve5@example.com"
        body = await gql(SPARK, SPARK_REGISTER, {"input": signup_input(email)})
        res = (body.get("data") or {}).get("register") or {}
        if res.get("activationToken"):
            await gql(SPARK, VERIFY_ACCOUNT, {"input": {"token": res["activationToken"]}})
        jwt = await password_login(email)
        assert not (jwt and await is_admin_with(jwt)), (
            "anonymous spark register reached admin"
        )
        assert not res.get("success")
        assert not await User.objects.filter(email=email).aexists()

    @pytest.mark.parametrize("path,query", [(CLIENTS, CLIENT_REGISTER), (AMBASSADORS, AMB_REGISTER)])
    async def test_anonymous_register_cannot_join_a_brand(self, path, query):
        from tenants.models import TenantedUser

        email = "joiner@example.com"
        body = await gql(
            path,
            query,
            {"input": {**signup_input(email), "role": "CLIENT", "tenantId": str(self.tenant.pk)}},
        )
        res = (body.get("data") or {}).get("register") or {}
        assert not res.get("success")
        assert not await TenantedUser.objects.filter(
            user__email=email, tenant=self.tenant
        ).aexists()

    async def test_brand_user_cannot_register_spark_admin(self):
        await self._acreate_user("brandadmin@example.com", self.roles["client"], verified=True)
        jwt = await password_login("brandadmin@example.com")
        body = await gql(
            CLIENTS,
            CLIENT_REGISTER,
            {"input": {**signup_input("escalate@example.com"), "role": "SPARK"}},
            token=jwt,
        )
        assert not body["data"]["register"]["success"]
        assert not await User.objects.filter(email="escalate@example.com").aexists()

    async def _brand_user_token(self, email):
        from asgiref.sync import sync_to_async

        user = await self._acreate_user(email, self.roles["client"], verified=True)
        await sync_to_async(self.create_tenanted_user)(user, self.tenant, is_active=True)
        return user, await password_login(email)

    async def test_brand_user_cannot_create_ignite_domain_user(self):
        _, jwt = await self._brand_user_token("brandowner@example.com")
        email = "minted@igniteproductions.co"
        body = await gql(
            CLIENTS,
            """mutation C($input: CreateUserInput!) { createUser(input: $input) { success message } }""",
            {"input": {**signup_input(email), "role": "CLIENT", "tenantId": str(self.tenant.pk)}},
            token=jwt,
        )
        assert not (body.get("data") or {}).get("createUser", {}).get("success"), body
        minted = await password_login(email)
        assert not (minted and await is_admin_with(minted))

    async def test_brand_user_cannot_move_own_email_onto_ignite_domain(self):
        user, jwt = await self._brand_user_token("brandself@example.com")
        body = await gql(
            CLIENTS,
            """mutation U($input: UpdateUserInput!) { updateUser(input: $input) { success message } }""",
            {"input": {"uuid": str(user.uuid), "email": "brandself@igniteproductions.co"}},
            token=jwt,
        )
        assert not (body.get("data") or {}).get("updateUser", {}).get("success"), body
        assert await User.objects.filter(pk=user.pk, email="brandself@example.com").aexists()
        assert not await is_admin_with(jwt)

    async def test_admin_can_still_register_users(self):
        await self._acreate_user("ops2@example.com", self.roles["spark_admin"], verified=True)
        jwt = await password_login("ops2@example.com")
        body = await gql(
            CLIENTS,
            CLIENT_REGISTER,
            {"input": {**signup_input("newclient@example.com"), "role": "CLIENT", "tenantId": str(self.tenant.pk)}},
            token=jwt,
        )
        assert body["data"]["register"]["success"], body
        assert await password_login("newclient@example.com")

    async def test_request_cc_list_skips_untrusted_ignite_accounts(self):
        from asgiref.sync import sync_to_async
        from events.mutations import _get_ignite_domain_emails

        await self._acreate_user(
            "planted2@igniteproductions.co", self.roles["ambassador"], verified=True
        )
        await self._acreate_user(
            "unverified2@igniteproductions.co", self.roles["client"], verified=False
        )
        emails = {e.lower() for e in await sync_to_async(_get_ignite_domain_emails)()}
        assert "kyle@igniteproductions.co" in emails
        assert "planted2@igniteproductions.co" not in emails
        assert "unverified2@igniteproductions.co" not in emails

    # --- 5. legacy Apple social auth (signature never verified) ---------

    @pytest.mark.parametrize("path", [SPARK, CLIENTS, AMBASSADORS])
    async def test_forged_apple_token_cannot_log_in_as_staff(self, path):
        body = await gql(
            path,
            APPLE_LEGACY,
            {"input": {"identityToken": forged_apple_token("kyle@igniteproductions.co")}},
        )
        res = (body.get("data") or {}).get("socialAuthApple") or {}
        if res.get("token"):
            assert not await is_admin_with(res["token"]), (
                "forged Apple token logged in as Kyle"
            )
        assert not res.get("success")

    @pytest.mark.parametrize("path", [SPARK, CLIENTS])
    async def test_forged_apple_token_cannot_create_accounts(self, path):
        email = "nobody-apple@example.com"
        await gql(
            path,
            APPLE_LEGACY,
            {"input": {"identityToken": forged_apple_token(email)}},
        )
        assert not await User.objects.filter(email=email).aexists()

    # --- 6. tokenAuth refuses unverified accounts (control) -------------

    async def test_token_auth_rejects_unverified(self):
        user = await self._acreate_user(
            "unverified@example.com", self.roles["client"], verified=False
        )
        assert user
        assert await password_login("unverified@example.com") is None

    # --- 7. pre-existing bad accounts: domain alone no longer grants admin

    async def test_ignite_domain_ambassador_is_not_admin(self):
        await self._acreate_user(
            "planted@igniteproductions.co", self.roles["ambassador"], verified=True
        )
        jwt = await password_login("planted@igniteproductions.co")
        assert jwt
        assert not await is_admin_with(jwt)

    async def test_unverified_ignite_domain_client_is_not_admin(self):
        user = await self._acreate_user(
            "unverified@igniteproductions.co", self.roles["client"], verified=False
        )
        from gqlauth.jwt.types_ import TokenType
        from asgiref.sync import sync_to_async

        jwt = (await sync_to_async(TokenType.from_user)(user)).token
        assert not await is_admin_with(jwt)

    # --- 8. real staff keep admin ---------------------------------------

    async def test_verified_ignite_client_keeps_admin(self):
        jwt = await password_login("kyle@igniteproductions.co")
        assert jwt
        assert await is_admin_with(jwt)

    async def test_spark_admin_role_keeps_admin(self):
        await self._acreate_user("ops@example.com", self.roles["spark_admin"], verified=True)
        jwt = await password_login("ops@example.com")
        assert jwt and await is_admin_with(jwt)

    async def test_is_staff_keeps_admin(self):
        await self._acreate_user(
            "staffer@example.com", self.roles["client"], verified=True, is_staff=True
        )
        jwt = await password_login("staffer@example.com")
        assert jwt and await is_admin_with(jwt)

    async def test_plain_client_is_not_admin(self):
        await self._acreate_user("brand@example.com", self.roles["client"], verified=True)
        jwt = await password_login("brand@example.com")
        assert jwt and not await is_admin_with(jwt)

    # ---------------------------------------------------------------------

    async def _acreate_user(self, email, role, *, verified, **kwargs):
        from asgiref.sync import sync_to_async

        return await sync_to_async(_make_user)(email, role, verified=verified, **kwargs)


def _make_user(email, role, *, verified, **kwargs):
    user = User.objects.create_user(
        username=email, email=email, role=role, password=PW, **kwargs
    )
    UserStatus.objects.update_or_create(
        user=user, defaults={"verified": verified, "archived": False}
    )
    return user
