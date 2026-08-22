"""
Client-invite flow: the welcome email, its 7-day token, and the bulk
inviteClientUsers mutation.

Kyle's rules encoded here:
  1. A NEW client gets ONE welcome email offering all three sign-in paths
     (set password / Google SSO / one-click magic link) on a 7-day token —
     not the 30-minute bare magic link that dead-ended invites before.
  2. The invite token works in BOTH loginWithMagicToken (one-click) and
     confirmPasswordReset (set password) — same token, two pages.
  3. Bulk invite NEVER emails someone who already has a Spark account;
     existing users just get the tenant link. Only brand-new accounts get
     the welcome email.
  4. Bulk invite is spark-admin-only.

Envelope tests render the real template (no email is sent, no network).
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core import signing

from tenants.envelopes import ClientInviteMailer
from tenants.mutations import CLIENT_INVITE_SALT, CLIENT_INVITE_TTL_SECONDS
from tenants.models import TenantedUser
from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()

SET_PW = "https://admin.igniteproductions.co/reset-password/tok-invite"
MAGIC = "https://admin.igniteproductions.co/magic/tok-invite"
LOGIN = "https://admin.igniteproductions.co/login"


def _mailer(user, **overrides):
    kwargs = dict(
        tenant_name="Liquid Death",
        set_password_link=SET_PW,
        magic_link=MAGIC,
        login_url=LOGIN,
        inviter_name="Kyle Christiansen",
        note=None,
        expires_days=7,
    )
    kwargs.update(overrides)
    return ClientInviteMailer(user, **kwargs)


@pytest.mark.django_db
class TestClientInviteEnvelope(BaseGraphQLTestCase):
    """The email Kyle reviews: subject + body must offer all three ways in."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client_role = self.create_role(name="Client", slug="client")
        self.user = self.create_user(
            username="ross@liquid-death.com",
            email="ross@liquid-death.com",
            role=self.client_role,
            first_name="Ross",
        )

    def test_subject_names_the_tenant(self):
        assert (
            _mailer(self.user).envelope().subject
            == "You're invited to Liquid Death on Spark"
        )

    def test_subject_falls_back_without_tenant(self):
        assert (
            _mailer(self.user, tenant_name=None).envelope().subject
            == "You're invited to Spark"
        )

    def test_body_offers_all_three_sign_in_paths(self):
        html = _mailer(self.user).envelope().render_template()
        # 1. Set a password (primary lime CTA)
        assert SET_PW in html
        assert "Set your password" in html
        # 2. Google SSO → the login page, keyed to their email
        assert "Continue with Google" in html
        assert LOGIN in html
        assert "ross@liquid-death.com" in html
        # 3. One-click magic link
        assert MAGIC in html
        assert "One-click sign-in" in html
        # 7-day expiry is spelled out
        assert "7 days" in html

    def test_body_is_a_welcome_not_a_welcome_back(self):
        html = _mailer(self.user).envelope().render_template()
        assert "Welcome to Spark, Ross." in html
        assert "Welcome back" not in html

    def test_personal_note_rendered_when_present(self):
        html = _mailer(
            self.user, note="Excited to onboard the Liquid Death program."
        ).envelope().render_template()
        assert "Excited to onboard the Liquid Death program." in html
        assert "Kyle Christiansen" in html

    def test_no_note_block_when_absent(self):
        html = _mailer(self.user).envelope().render_template()
        assert "&ldquo;" not in html


@pytest.mark.django_db(transaction=True)
class TestClientInviteToken(BaseGraphQLTestCase):
    """The 7-day invite token must open BOTH invite paths."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    def _invite_token(self, user) -> str:
        return signing.dumps(
            {"u": user.id, "e": user.email, "k": "invite"},
            salt=CLIENT_INVITE_SALT,
        )

    @pytest.mark.asyncio
    async def test_invite_token_logs_in_via_magic_mutation(self):
        user = await sync_to_async(self.create_user)(
            username="new@client.com",
            email="new@client.com",
            role=self.roles["client"],
        )
        token = self._invite_token(user)

        result = await self._execute_mutation(
            """
            mutation Login($input: LoginWithMagicTokenInput!) {
                loginWithMagicToken(input: $input) { success message token email }
            }
            """,
            {"input": {"token": token}},
            self.endpoint_path,
        )

        assert result.errors is None
        payload = result.data["loginWithMagicToken"]
        assert payload["success"] is True
        assert payload["token"]
        assert payload["email"] == "new@client.com"

    @pytest.mark.asyncio
    async def test_invite_token_sets_password_via_reset_mutation(self):
        user = await sync_to_async(self.create_user)(
            username="setpw@client.com",
            email="setpw@client.com",
            role=self.roles["client"],
        )
        token = self._invite_token(user)

        result = await self._execute_mutation(
            """
            mutation SetPw($input: ConfirmPasswordResetInput!) {
                confirmPasswordReset(input: $input) { success message }
            }
            """,
            {
                "input": {
                    "token": token,
                    "password1": "spark-rocks-123",
                    "password2": "spark-rocks-123",
                }
            },
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data["confirmPasswordReset"]["success"] is True
        await sync_to_async(user.refresh_from_db)()
        assert await sync_to_async(user.check_password)("spark-rocks-123")

    @pytest.mark.asyncio
    async def test_plain_reset_token_still_works(self):
        """The 30-minute forgot-password flow is untouched."""
        user = await sync_to_async(self.create_user)(
            username="reset@client.com",
            email="reset@client.com",
            role=self.roles["client"],
        )
        token = signing.dumps(
            {"u": user.id, "e": user.email, "k": "pwd"},
            salt="spark.password-reset.v1",
        )

        result = await self._execute_mutation(
            """
            mutation SetPw($input: ConfirmPasswordResetInput!) {
                confirmPasswordReset(input: $input) { success message }
            }
            """,
            {
                "input": {
                    "token": token,
                    "password1": "spark-rocks-123",
                    "password2": "spark-rocks-123",
                }
            },
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data["confirmPasswordReset"]["success"] is True

    @pytest.mark.asyncio
    async def test_garbage_token_is_invalid_not_expired(self):
        result = await self._execute_mutation(
            """
            mutation Login($input: LoginWithMagicTokenInput!) {
                loginWithMagicToken(input: $input) { success message }
            }
            """,
            {"input": {"token": "not-a-real-token"}},
            self.endpoint_path,
        )

        assert result.errors is None
        payload = result.data["loginWithMagicToken"]
        assert payload["success"] is False
        assert payload["message"] == "Invalid sign-in link."


@pytest.mark.django_db(transaction=True)
class TestInviteUserWelcomeEmail(BaseGraphQLTestCase):
    """inviteUser: NEW clients get the welcome invite; everyone else keeps
    the plain 30-minute magic link."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.tenant = None

    async def _tenant(self):
        if self.tenant is None:
            self.tenant = await sync_to_async(self.create_tenant)(
                name="Liquid Death"
            )
        return self.tenant

    @pytest.mark.asyncio
    async def test_new_client_gets_welcome_invite(self):
        tenant = await self._tenant()

        with patch(
            "tenants.mutations.ClientInviteMailer"
        ) as invite_cls, patch(
            "tenants.mutations.MagicLinkMailer"
        ) as magic_cls:
            invite_cls.return_value.send_async_now = AsyncMock()
            magic_cls.return_value.send_async_now = AsyncMock()

            result = await self._execute_mutation(
                """
                mutation Invite($input: InviteUserInput!) {
                    inviteUser(input: $input) { success message }
                }
                """,
                {
                    "input": {
                        "email": "Ross@Liquid-Death.com",
                        "firstName": "Ross",
                        "role": "client",
                        "tenantId": str(tenant.id),
                        "note": "Welcome aboard!",
                    }
                },
                self.endpoint_path,
            )

        assert result.errors is None
        assert result.data["inviteUser"]["success"] is True
        # Welcome invite went out; the bare magic link did NOT.
        invite_cls.return_value.send_async_now.assert_awaited_once()
        magic_cls.return_value.send_async_now.assert_not_awaited()

        # The mailer got the tenant name, the inviter's note, and all
        # three links on a 7-day token.
        _, kwargs = invite_cls.call_args
        assert kwargs["tenant_name"] == "Liquid Death"
        assert kwargs["note"] == "Welcome aboard!"
        assert kwargs["expires_days"] == 7
        assert "/reset-password/" in kwargs["set_password_link"]
        assert "/magic/" in kwargs["magic_link"]
        assert kwargs["login_url"].endswith("/login")

        user = await sync_to_async(
            lambda: User.objects.filter(email="ross@liquid-death.com").first()
        )()
        assert user is not None
        assert user.is_active is True  # no activation trap
        assert user.role_id == 3
        # gqlauth verified — password login must NOT hit "Please verify
        # your account" after they set a password from the invite.
        from gqlauth.models import UserStatus

        status = await sync_to_async(
            lambda: UserStatus.objects.filter(user=user).first()
        )()
        assert status is not None and status.verified is True
        link = await sync_to_async(
            lambda: TenantedUser.objects.filter(user=user, tenant=tenant).first()
        )()
        assert link is not None and link.is_active is True

        # And the token in the email really is the 7-day invite salt.
        token = kwargs["magic_link"].rsplit("/magic/", 1)[1]
        payload = signing.loads(
            token, salt=CLIENT_INVITE_SALT, max_age=CLIENT_INVITE_TTL_SECONDS
        )
        assert payload == {"u": user.id, "e": user.email, "k": "invite"}

    @pytest.mark.asyncio
    async def test_existing_user_keeps_plain_magic_link(self):
        tenant = await self._tenant()
        await sync_to_async(self.create_user)(
            username="ross@liquid-death.com",
            email="ross@liquid-death.com",
            role=self.roles["client"],
        )

        with patch(
            "tenants.mutations.ClientInviteMailer"
        ) as invite_cls, patch(
            "tenants.mutations.MagicLinkMailer"
        ) as magic_cls:
            invite_cls.return_value.send_async_now = AsyncMock()
            magic_cls.return_value.send_async_now = AsyncMock()

            result = await self._execute_mutation(
                """
                mutation Invite($input: InviteUserInput!) {
                    inviteUser(input: $input) { success message }
                }
                """,
                {
                    "input": {
                        "email": "ross@liquid-death.com",
                        "role": "client",
                        "tenantId": str(tenant.id),
                    }
                },
                self.endpoint_path,
            )

        assert result.errors is None
        assert result.data["inviteUser"]["success"] is True
        assert "re-sent" in result.data["inviteUser"]["message"]
        # Existing user: plain sign-in link, NOT the welcome invite.
        magic_cls.return_value.send_async_now.assert_awaited_once()
        invite_cls.return_value.send_async_now.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
class TestInviteClientUsersBulk(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    async def _admin(self):
        return await sync_to_async(self.create_user)(
            username="kyle@igniteproductions.co",
            email="kyle@igniteproductions.co",
            role=self.roles["spark_admin"],
            first_name="Kyle",
        )

    async def _tenant(self):
        return await sync_to_async(self.create_tenant)(name="Liquid Death")

    _MUTATION = """
        mutation BulkInvite($input: InviteClientUsersInput!) {
            inviteClientUsers(input: $input) {
                success
                message
                invited
                existing
                invalid
                results { email status message }
            }
        }
    """

    @pytest.mark.asyncio
    async def test_mixed_batch(self):
        tenant = await self._tenant()
        admin = await self._admin()
        # One seat already taken — must be linked but NOT emailed.
        await sync_to_async(self.create_user)(
            username="vet@liquid-death.com",
            email="vet@liquid-death.com",
            role=self.roles["client"],
        )

        with patch(
            "tenants.mutations.ClientInviteMailer"
        ) as invite_cls:
            invite_cls.return_value.send_async_now = AsyncMock()

            result = await self._execute_mutation(
                self._MUTATION,
                {
                    "input": {
                        "tenantId": str(tenant.id),
                        "rows": [
                            {"email": "New@Liquid-Death.com", "firstName": "New"},
                            {"email": "vet@liquid-death.com"},
                            {"email": "not-an-email"},
                            {"email": "new@liquid-death.com"},  # dupe in batch
                        ],
                    }
                },
                self.endpoint_path,
                user=admin,
            )

        assert result.errors is None
        payload = result.data["inviteClientUsers"]
        assert payload["success"] is True
        assert payload["invited"] == 1
        assert payload["existing"] == 1
        assert payload["invalid"] == 2

        by_email = {r["email"]: r for r in payload["results"]}
        assert by_email["New@Liquid-Death.com"]["status"] == "invited"
        assert by_email["vet@liquid-death.com"]["status"] == "existing"
        assert "no email" in by_email["vet@liquid-death.com"]["message"]
        assert by_email["not-an-email"]["status"] == "invalid"
        # In-batch dupe of the first row (case-insensitive) — skipped.
        assert by_email["new@liquid-death.com"]["status"] == "invalid"
        assert "Duplicate" in by_email["new@liquid-death.com"]["message"]

        # Exactly ONE email went out — to the brand-new account only.
        assert invite_cls.return_value.send_async_now.await_count == 1

        # New user is active, verified, client-roled, tenant-linked
        # (no activation trap on any of the three sign-in paths).
        user = await sync_to_async(
            lambda: User.objects.filter(email="new@liquid-death.com").first()
        )()
        assert user is not None
        assert user.is_active is True
        assert user.role_id == 3
        assert not user.has_usable_password()  # forced through a set-password path
        from gqlauth.models import UserStatus

        status = await sync_to_async(
            lambda: UserStatus.objects.filter(user=user).first()
        )()
        assert status is not None and status.verified is True
        link = await sync_to_async(
            lambda: TenantedUser.objects.filter(user=user, tenant=tenant).first()
        )()
        assert link is not None and link.is_active is True

        # Existing user got the tenant link too — silently.
        vet = await sync_to_async(
            lambda: User.objects.get(email="vet@liquid-death.com")
        )()
        vet_link = await sync_to_async(
            lambda: TenantedUser.objects.filter(user=vet, tenant=tenant).first()
        )()
        assert vet_link is not None and vet_link.is_active is True

    @pytest.mark.asyncio
    async def test_non_admin_is_rejected(self):
        tenant = await self._tenant()
        client = await sync_to_async(self.create_user)(
            username="client@liquid-death.com",
            email="client@liquid-death.com",
            role=self.roles["client"],
        )

        with patch(
            "tenants.mutations.ClientInviteMailer"
        ) as invite_cls:
            invite_cls.return_value.send_async_now = AsyncMock()
            result = await self._execute_mutation(
                self._MUTATION,
                {
                    "input": {
                        "tenantId": str(tenant.id),
                        "rows": [{"email": "a@b.com"}],
                    }
                },
                self.endpoint_path,
                user=client,
            )

        assert result.errors is None
        payload = result.data["inviteClientUsers"]
        assert payload["success"] is False
        assert "admin" in payload["message"].lower()
        invite_cls.return_value.send_async_now.assert_not_awaited()
        assert await sync_to_async(
            lambda: User.objects.filter(email="a@b.com").exists()
        )() is False

    @pytest.mark.asyncio
    async def test_anonymous_is_rejected(self):
        tenant = await self._tenant()
        result = await self._execute_mutation(
            self._MUTATION,
            {
                "input": {
                    "tenantId": str(tenant.id),
                    "rows": [{"email": "a@b.com"}],
                }
            },
            self.endpoint_path,
        )
        assert result.errors is None
        assert result.data["inviteClientUsers"]["success"] is False

    @pytest.mark.asyncio
    async def test_over_100_rows_rejected(self):
        tenant = await self._tenant()
        admin = await self._admin()
        result = await self._execute_mutation(
            self._MUTATION,
            {
                "input": {
                    "tenantId": str(tenant.id),
                    "rows": [{"email": f"u{i}@x.com"} for i in range(101)],
                }
            },
            self.endpoint_path,
            user=admin,
        )
        assert result.errors is None
        payload = result.data["inviteClientUsers"]
        assert payload["success"] is False
        assert "100" in payload["message"]

    @pytest.mark.asyncio
    async def test_unknown_tenant_rejected(self):
        admin = await self._admin()
        result = await self._execute_mutation(
            self._MUTATION,
            {
                "input": {
                    "tenantId": "999999",
                    "rows": [{"email": "a@b.com"}],
                }
            },
            self.endpoint_path,
            user=admin,
        )
        assert result.errors is None
        assert result.data["inviteClientUsers"]["success"] is False
