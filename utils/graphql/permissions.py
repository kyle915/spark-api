import logging

import strawberry
from strawberry.permission import BasePermission
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# Every @igniteproductions.co user is an Ignite admin and gets full access.
IGNITE_EMAIL_DOMAIN = "@igniteproductions.co"


def _is_admin_access(role_slug, is_staff, is_super, email) -> bool:
    """True for anyone who should see everything: platform admins
    (is_staff/superuser), spark-admins, or any @igniteproductions.co email."""
    email = (email or "").lower()
    return bool(
        is_staff
        or is_super
        or role_slug == "spark-admin"
        or email.endswith(IGNITE_EMAIL_DOMAIN)
    )


async def resolve_request_user_access(user):
    """Authoritatively resolve (role_slug, is_staff, is_super, email) for a
    request user.

    The JWT request.user does not reliably hydrate its role FK / flags inside
    async resolvers, so reading user.role directly returned empty and denied
    genuine spark-admins (the tracker / Invite-BA / client-view blanks). We
    re-read from the DB by pk (then by email as a fallback), and log a warning
    if we can't resolve a DB row so the cause is visible.
    """

    @sync_to_async
    def _resolve():
        from django.contrib.auth import get_user_model

        User = get_user_model()
        db_user = None
        pk = getattr(user, "pk", None)
        req_email = (getattr(user, "email", "") or "").strip()
        try:
            if pk is not None:
                db_user = (
                    User.objects.select_related("role").filter(pk=pk).first()
                )
            if db_user is None and req_email:
                db_user = (
                    User.objects.select_related("role")
                    .filter(email__iexact=req_email)
                    .first()
                )
        except Exception:
            db_user = None

        if db_user is not None:
            return (
                (getattr(db_user.role, "slug", "") or "").lower()
                if db_user.role
                else None,
                bool(db_user.is_staff),
                bool(db_user.is_superuser),
                (db_user.email or "").lower(),
            )

        # Couldn't resolve a DB row — log it and fall back to whatever the
        # request object exposes so an @igniteproductions.co email still works.
        logger.warning(
            "resolve_request_user_access: no DB user "
            "(pk=%r email=%r type=%s authed=%s)",
            pk,
            req_email,
            type(user).__name__,
            getattr(user, "is_authenticated", None),
        )
        return (
            (getattr(getattr(user, "role", None), "slug", "") or "").lower()
            or None,
            bool(getattr(user, "is_staff", False)),
            bool(getattr(user, "is_superuser", False)),
            req_email.lower(),
        )

    result = await _resolve()
    role_slug, is_staff, is_super, email = result
    # Diagnostic: if an authenticated user is about to be denied, log the
    # resolved identity so we can see WHY (e.g. token bound to a non-admin
    # pk). Temporary, low volume (only fires on a would-be denial).
    if not (
        _is_admin_access(role_slug, is_staff, is_super, email)
        or role_slug == "client"
    ):
        logger.warning(
            "ACCESS-DENY-DIAG: authed user not granted — "
            "req(pk=%r email=%r type=%s) resolved(role=%r staff=%s super=%s email=%r)",
            getattr(user, "pk", None),
            getattr(user, "email", None),
            type(user).__name__,
            role_slug,
            is_staff,
            is_super,
            email,
        )
    return result


class StrictIsAuthenticated(BasePermission):
    message = "Authentication required."

    def has_permission(self, source, info, **kwargs):
        user = info.context.request.user
        return user and user.is_authenticated


class IsClientOrSparkAdmin(BasePermission):
    """Allow clients, spark-admins, and platform/Ignite admins."""
    message = "You do not have permission to perform this action. Client or Spark Admin access required."

    async def has_permission(self, source, info, **kwargs) -> bool:
        user = info.context.request.user
        if not user or not user.is_authenticated:
            return False
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )
        return _is_admin_access(role_slug, is_staff, is_super, email) or (
            role_slug == "client"
        )
