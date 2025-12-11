import strawberry
from strawberry.permission import BasePermission
from asgiref.sync import sync_to_async


class StrictIsAuthenticated(BasePermission):
    message = "Authentication required."

    def has_permission(self, source, info, **kwargs):
        user = info.context.request.user
        return user and user.is_authenticated


class IsClientOrSparkAdmin(BasePermission):
    """Permission class to check if user is client or spark-admin."""
    message = "You do not have permission to perform this action. Client or Spark Admin access required."

    async def has_permission(self, source, info, **kwargs) -> bool:
        user = info.context.request.user
        if not user or not user.is_authenticated:
            return False

        @sync_to_async
        def get_role_slug():
            return getattr(user.role, "slug", "").lower() if user.role else ""

        role_slug = await get_role_slug()
        return role_slug in ["client", "spark-admin"]
