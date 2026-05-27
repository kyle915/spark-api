import strawberry
from strawberry.permission import BasePermission
from asgiref.sync import sync_to_async


class StrictIsAuthenticated(BasePermission):
    message = "Authentication required."

    def has_permission(self, source, info, **kwargs):
        user = info.context.request.user
        return user and user.is_authenticated


class IsClientOrSparkAdmin(BasePermission):
    """Permission class to check if user is client or spark-admin (or a
    platform admin via is_staff / is_superuser)."""
    message = "You do not have permission to perform this action. Client or Spark Admin access required."

    async def has_permission(self, source, info, **kwargs) -> bool:
        user = info.context.request.user
        if not user or not user.is_authenticated:
            return False

        @sync_to_async
        def check_access():
            # Platform admins always pass — matches the tenants resolver.
            if getattr(user, "is_staff", False) or getattr(
                user, "is_superuser", False
            ):
                return True
            # Re-read the role authoritatively by PK. The JWT request.user
            # doesn't reliably hydrate its role FK, so reading user.role
            # directly returned "" and denied genuine spark-admins — that's
            # the "Invite BA goes blank" bug (the modal's ambassadors query
            # was rejected, threw, and unmounted the page).
            pk = getattr(user, "pk", None)
            if pk is None:
                return False
            try:
                from django.contrib.auth import get_user_model

                db_user = (
                    get_user_model()
                    .objects.select_related("role")
                    .filter(pk=pk)
                    .first()
                )
                slug = (
                    getattr(getattr(db_user, "role", None), "slug", "") or ""
                ).lower()
                return slug in ("client", "spark-admin")
            except Exception:
                return False

        return await check_access()
