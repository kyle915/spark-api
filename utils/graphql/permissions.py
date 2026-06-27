import logging

import strawberry
from strawberry.permission import BasePermission
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# Every @igniteproductions.co user is an Ignite admin and gets full access.
IGNITE_EMAIL_DOMAIN = "@igniteproductions.co"

# Individual @igniteproductions.co addresses explicitly REMOVED from the
# automatic Ignite-admin grant. The domain above normally confers full
# platform-admin access (see _is_admin_access + the inline endswith() checks
# across the schema); listing an address here demotes just that one person to
# a normal user, without changing the domain rule for everyone else.
# Lower-cased. Fully reversible — delete the entry to restore access.
# (The DB side — is_staff / is_superuser for Django /admin/ — is cleared
# separately via the demote-admin cron endpoint.)
IGNITE_ADMIN_EXCLUDE: set[str] = {
    "madison@igniteproductions.co",
}


def email_grants_ignite_admin(email) -> bool:
    """True if this email confers Ignite-admin access by domain — i.e. it ends
    in @igniteproductions.co AND is not on the explicit exclude list. Use this
    everywhere instead of a bare ``endswith()`` so the exclude list is honored
    uniformly across the schema."""
    email = (email or "").lower()
    return email.endswith(IGNITE_EMAIL_DOMAIN) and email not in IGNITE_ADMIN_EXCLUDE


def _demote_if_excluded(role_slug, is_staff, is_super, email):
    """If ``email`` is on IGNITE_ADMIN_EXCLUDE, strip every elevated signal
    (role, is_staff, is_superuser) so each downstream permission check treats
    them as a plain authenticated user — even if their DB row still carries a
    spark-admin role or a stale staff flag. The email itself is returned
    unchanged (the domain-admin condition is neutralized separately via
    ``email_grants_ignite_admin`` / ``_is_admin_access``)."""
    if (email or "").lower() in IGNITE_ADMIN_EXCLUDE:
        return (None, False, False, email)
    return (role_slug, is_staff, is_super, email)


def _is_admin_access(role_slug, is_staff, is_super, email) -> bool:
    """True for anyone who should see everything: platform admins
    (is_staff/superuser), spark-admins, or any @igniteproductions.co email.

    An address on IGNITE_ADMIN_EXCLUDE is denied outright, overriding every
    other signal (incl. a stale is_staff/spark-admin role), so a removed
    admin can't slip back in through any single grant."""
    email = (email or "").lower()
    if email in IGNITE_ADMIN_EXCLUDE:
        return False
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
            return _demote_if_excluded(
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
        return _demote_if_excluded(
            (getattr(getattr(user, "role", None), "slug", "") or "").lower()
            or None,
            bool(getattr(user, "is_staff", False)),
            bool(getattr(user, "is_superuser", False)),
            req_email.lower(),
        )

    return await _resolve()


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
