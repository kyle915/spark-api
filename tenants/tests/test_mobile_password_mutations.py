import pytest
from asgiref.sync import sync_to_async

from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestMobileChangeOwnPassword(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Mobile Password Tenant")
        self.user = self.create_user(
            username="mobile_ambassador@test.com",
            email="mobile_ambassador@test.com",
            role=self.roles["ambassador"],
            password="oldpassword123",
            is_active=True,
        )
        self.create_tenanted_user(self.user, self.tenant, is_active=True)
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.mutation = """
            mutation ChangeUserPassword($input: ChangeUserPasswordInput!) {
                changeUserPassword(input: $input) {
                    success
                    message
                }
            }
        """

    @pytest.mark.asyncio
    async def test_mobile_ambassador_can_change_own_password(self):
        result = await self._execute_mutation(
            self.mutation,
            {
                "input": {
                    "password1": "newpassword123",
                    "password2": "newpassword123",
                }
            },
            self.endpoint_path,
            user=self.user,
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["changeUserPassword"]["success"] is True

        await sync_to_async(self.user.refresh_from_db)()
        assert await sync_to_async(self.user.check_password)("newpassword123")
