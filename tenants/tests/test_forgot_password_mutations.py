import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from tenants.models import PasswordResetCode
from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestForgotPasswordMutations(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_forgot_password_creates_code_and_sends_email(self):
        user = await sync_to_async(self.create_user)(
            username="recover@test.com",
            email="recover@test.com",
            role=self.roles["client"],
            password="oldpassword123",
        )

        mutation = """
        mutation ForgotPassword($input: ForgotPasswordInput!) {
            forgotPassword(input: $input) {
                success
                message
                clientMutationId
            }
        }
        """
        variables = {
            "input": {
                "email": user.email,
                "clientMutationId": "fp-1",
            }
        }

        with patch(
            "tenants.mutations.ForgotPasswordCodeMailer.send_async",
            new_callable=AsyncMock,
        ) as mocked_send:
            result = await self._execute_mutation(
                mutation, variables, self.endpoint_path
            )

        assert result.errors is None
        assert result.data["forgotPassword"]["success"] is True
        assert result.data["forgotPassword"]["clientMutationId"] == "fp-1"
        mocked_send.assert_awaited_once()

        code = await sync_to_async(
            lambda: PasswordResetCode.objects.filter(user=user)
            .order_by("-created_at")
            .first()
        )()
        assert code is not None
        assert len(code.code) == 4
        assert code.code.isdigit()
        assert code.is_used is False

    @pytest.mark.asyncio
    async def test_reset_password_with_valid_code(self):
        user = await sync_to_async(self.create_user)(
            username="reset@test.com",
            email="reset@test.com",
            role=self.roles["ambassador"],
            password="oldpassword123",
        )
        reset_code = await sync_to_async(PasswordResetCode.objects.create)(
            user=user,
            code="1234",
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        mutation = """
        mutation ResetPassword($input: ResetPasswordWithCodeInput!) {
            resetPasswordWithCode(input: $input) {
                success
                message
            }
        }
        """
        variables = {
            "input": {
                "email": user.email,
                "code": "1234",
                "password1": "newpassword123",
                "password2": "newpassword123",
            }
        }

        result = await self._execute_mutation(mutation, variables, self.endpoint_path)

        assert result.errors is None
        assert result.data["resetPasswordWithCode"]["success"] is True

        await sync_to_async(user.refresh_from_db)()
        assert await sync_to_async(user.check_password)("newpassword123")
        await sync_to_async(reset_code.refresh_from_db)()
        assert reset_code.is_used is True

    @pytest.mark.asyncio
    async def test_reset_password_with_invalid_code(self):
        user = await sync_to_async(self.create_user)(
            username="invalid@test.com",
            email="invalid@test.com",
            role=self.roles["client"],
            password="oldpassword123",
        )

        mutation = """
        mutation ResetPassword($input: ResetPasswordWithCodeInput!) {
            resetPasswordWithCode(input: $input) {
                success
                message
            }
        }
        """
        variables = {
            "input": {
                "email": user.email,
                "code": "0000",
                "password1": "newpassword123",
                "password2": "newpassword123",
            }
        }

        result = await self._execute_mutation(mutation, variables, self.endpoint_path)

        assert result.errors is None
        assert result.data["resetPasswordWithCode"]["success"] is False
        assert (
            result.data["resetPasswordWithCode"]["message"] == "Invalid code or email."
        )
        await sync_to_async(user.refresh_from_db)()
        assert await sync_to_async(user.check_password)("oldpassword123")
