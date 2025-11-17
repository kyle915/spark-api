import strawberry
from typing import Any
from graphql import GraphQLError
from asgiref.sync import sync_to_async

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

from tenants.models import Tenant

User = get_user_model()


class SparkGraphQLMixin:
    """Mixin for Spark GraphQL operations."""

    def is_spark_schema_request(
        self,
        info: strawberry.Info,
        user: User | None = None,
    ) -> bool:
        """Determine if the current request is made by a Spark admin user."""
        if not user:
            request = getattr(info.context, "request", None)
            user = getattr(request, "user", None) if request else None

        if not user or not getattr(user, "is_authenticated", False):
            return False

        role = getattr(user, "role", None)
        slug = getattr(role, "slug", None) if role else None
        return (slug or "").lower() == "spark-admin"

    async def get_user_tenant(
        self,
        info: strawberry.Info,
        tenant_id: int | str | None = None,
        tenant_uuid: str | None = None,
        user: User | None = None,
    ) -> Tenant:
        """Get the tenant for the user.

        Args:
            info (strawberry.Info): The GraphQL resolve info.
            tenant_id (int | None, optional): The tenant id. Defaults to None.
            tenant_uuid (str | None, optional): The tenant uuid. Defaults to None.
        Returns:
            Tenant: The tenant for the user.
        """
        user = user or await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)
        has_explicit_tenant = tenant_id is not None or tenant_uuid is not None

        if is_spark_request and has_explicit_tenant:
            tenant = await self._get_tenant_without_membership(
                tenant_id=tenant_id,
                tenant_uuid=tenant_uuid,
            )
        else:
            tenant = await self.get_tenant(
                user,
                tenant_id=tenant_id,
                tenant_uuid=tenant_uuid,
            )

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
                "Authentication required. Please provide a valid Auth token."
            )
        return user

    async def get_tenant(
        self,
        user: User,
        tenant_id: int | None = None,
        tenant_uuid: str | None = None,
    ) -> Tenant:
        """Get the tenant for the user.

        Args:
            user (User): The authenticated user.
            tenant_id (int | None, optional): The tenant id. Defaults to None.
            tenant_uuid (str | None, optional): The tenant uuid. Defaults to None.

        Returns:
            Tenant: The tenant for the user.
        """
        try:
            return await sync_to_async(user.get_tenant)(
                tenant_id,
                tenant_uuid,
            )
        except Exception as e:
            raise GraphQLError("It looks like you are not a member of this tenant.")

    async def _get_tenant_without_membership(
        self,
        tenant_id: int | str | None = None,
        tenant_uuid: str | None = None,
    ) -> Tenant:
        """Fetch a tenant directly without checking user membership."""
        filters: dict[str, Any] = {}

        if tenant_id is not None:
            try:
                filters["id"] = int(tenant_id)
            except (TypeError, ValueError):
                raise GraphQLError("Invalid tenant ID.")
        elif tenant_uuid:
            filters["uuid"] = tenant_uuid
        else:
            raise GraphQLError("Tenant identifier is required.")

        try:
            return await sync_to_async(Tenant.objects.get)(**filters)
        except Tenant.DoesNotExist:
            raise GraphQLError("Tenant not found.")
