import strawberry

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from tenants.models import Tenant

User = get_user_model()


class SparkGraphQLMixin:
    """Mixin for Spark GraphQL operations."""

    async def get_user_tenant(self, info: strawberry.Info, tenant_id: int | None = None) -> Tenant:
        """Get the tenant for the user.

        Args:
            info (strawberry.Info): The info object.
            tenant_id (int | None, optional): The tenant id. Defaults to None.
        Returns:
            Tenant: The tenant for the user.
        """
        user = await self.get_user(info)
        tenant = await self.get_tenant(user, tenant_id)
        return tenant

    async def get_user(self, info: strawberry.Info) -> User:
        """Get the user for the request.

        Args:
            info (strawberry.Info): The info object.

        Returns:
            User: The user for the request.
        """
        user = info.context.request.user
        if not user or not user.is_authenticated or isinstance(user, AnonymousUser):
            raise GraphQLError(
                "Authentication required. Please provide a valid Auth token.")
        return user

    async def get_tenant(
        self,
        user: User,
        tenant_id: int | None = None
    ) -> Tenant:
        """Get the tenant for the user.

        Args:
            info (strawberry.Info): The info object.
            tenant_id (int | None, optional): The tenant id. Defaults to None.

        Returns:
            Tenant: The tenant for the user.
        """
        tenant = await sync_to_async(user.get_tenant)(tenant_id)
        if not tenant:
            raise GraphQLError(
                f"No active tenant found for user {user.username} with id {tenant_id}.")
        return tenant
