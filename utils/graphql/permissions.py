import logging

import strawberry
from strawberry.permission import BasePermission
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# A *trusted* @igniteproductions.co account is an Ignite admin and gets full
# access. The domain alone is NOT enough: public sign-up paths let anyone type
# an address they don't own, so the grant also needs an account that proved
# its inbox (gqlauth UserStatus.verified), is active, and is not a brand
# ambassador. See ``ignite_domain_account_trusted``.
IGNITE_EMAIL_DOMAIN = "@igniteproductions.co"
IGNITE_DOMAIN = IGNITE_EMAIL_DOMAIN.lstrip("@")

# Roles that may carry the domain grant. Ambassadors are self-serve sign-ups
# (public signup, walk-up, SSO auto-create), so an ambassador account on the
# Ignite domain is never treated as staff.
_DOMAIN_GRANT_ROLES = frozenset({"client", "spark-admin"})

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


def normalize_email(email) -> str:
    """Canonical form for identity comparisons: trimmed, lower-cased, with a
    trailing dot on the domain dropped (``a@b.co.`` delivers to ``a@b.co``)."""
    email = (email or "").strip().lower()
    while email.endswith("."):
        email = email[:-1]
    return email


def is_ignite_domain_email(email) -> bool:
    """True for any address on Ignite's domain or a subdomain of it, however
    it is cased, padded or dotted (``Eve@IgniteProductions.CO.``,
    ``eve@mail.igniteproductions.co``). Plus-addressing is irrelevant: the
    domain part is what decides."""
    email = normalize_email(email)
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    return domain == IGNITE_DOMAIN or domain.endswith("." + IGNITE_DOMAIN)


RESERVED_EMAIL_MESSAGE = (
    "This email can't be used to sign up here. Use a different email address."
)


def reserved_signup_email_error(email) -> str | None:
    """Neutral refusal for self-serve sign-ups on Ignite's own domain.

    Staff accounts are provisioned by an admin (or via Ignite's Google
    Workspace SSO), never through a public form, so a public form that sees
    the domain is someone else typing an address they don't own."""
    return RESERVED_EMAIL_MESSAGE if is_ignite_domain_email(email) else None


class UntrustedIgniteEmail(str):
    """An Ignite-domain address whose account has NOT proven it is staff.

    ``resolve_request_user_access`` returns the email in this wrapper when the
    DB row is unverified, inactive, or an ambassador, so every admin check
    downstream (``_is_admin_access`` / ``email_grants_ignite_admin``, which
    only receive the email) refuses the domain grant without each of the ~40
    call sites having to change. It compares and prints as the plain string,
    so callers that use the email for anything else are unaffected."""

    ignite_domain_untrusted = True


def ignite_domain_account_trusted(*, role_slug, is_active, verified) -> bool:
    """Whether an Ignite-domain account may use the domain admin grant."""
    return bool(
        is_active
        and verified
        and (role_slug or "").lower() in _DOMAIN_GRANT_ROLES
    )


def email_grants_ignite_admin(email) -> bool:
    """True if this email confers Ignite-admin access by domain — i.e. it is on
    @igniteproductions.co, is not on the explicit exclude list, and did not
    come back from ``resolve_request_user_access`` flagged untrusted. Use this
    everywhere instead of a bare ``endswith()`` so the exclude list and the
    trust check are honored uniformly across the schema."""
    if getattr(email, "ignite_domain_untrusted", False):
        return False
    email = normalize_email(email)
    return is_ignite_domain_email(email) and email not in IGNITE_ADMIN_EXCLUDE


def user_grants_ignite_admin(user) -> bool:
    """Sync, DB-backed Ignite-admin check for code holding a User object
    (not a request): staff / superuser / spark-admin, or a trusted
    Ignite-domain account. Call from sync code only (it queries)."""
    if user is None or not getattr(user, "pk", None):
        return False
    from django.contrib.auth import get_user_model
    from gqlauth.models import UserStatus

    db_user = (
        get_user_model().objects.select_related("role").filter(pk=user.pk).first()
    )
    if db_user is None:
        return False
    role_slug = (getattr(db_user.role, "slug", "") or "").lower() or None
    email = (db_user.email or "").lower()
    if is_ignite_domain_email(email) and not ignite_domain_account_trusted(
        role_slug=role_slug,
        is_active=db_user.is_active,
        verified=UserStatus.objects.filter(user_id=db_user.pk, verified=True).exists(),
    ):
        email = UntrustedIgniteEmail(email)
    return _is_admin_access(role_slug, db_user.is_staff, db_user.is_superuser, email)


def trusted_ignite_domain_users():
    """Queryset of Ignite-domain accounts that may use the domain grant
    (active, verified, client/spark-admin role). For roll-ups like the
    request CC list that must not include a self-registered lookalike."""
    from django.contrib.auth import get_user_model

    return (
        get_user_model()
        .objects.filter(
            is_active=True,
            status__verified=True,
            role__slug__in=sorted(_DOMAIN_GRANT_ROLES),
        )
        .filter(email__iregex=r"@([a-z0-9-]+\.)*igniteproductions\.co\.?$")
        .exclude(email__in=sorted(IGNITE_ADMIN_EXCLUDE))
    )


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
    (is_staff/superuser), spark-admins, or a trusted @igniteproductions.co
    account (verified, active, not an ambassador — see
    ``resolve_request_user_access``).

    An address on IGNITE_ADMIN_EXCLUDE is denied outright, overriding every
    other signal (incl. a stale is_staff/spark-admin role), so a removed
    admin can't slip back in through any single grant."""
    untrusted = bool(getattr(email, "ignite_domain_untrusted", False))
    email = normalize_email(email)
    if email in IGNITE_ADMIN_EXCLUDE:
        return False
    if is_staff or is_super or role_slug == "spark-admin":
        return True
    # Domain grant: never for an ambassador (self-serve sign-up) or an account
    # resolve_request_user_access flagged as unverified/inactive.
    if untrusted or (role_slug or "").lower() == "ambassador":
        return False
    return email_grants_ignite_admin(email)


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
            role_slug = (
                (getattr(db_user.role, "slug", "") or "").lower()
                if db_user.role
                else None
            )
            email = (db_user.email or "").lower()
            if is_ignite_domain_email(email):
                from gqlauth.models import UserStatus

                verified = UserStatus.objects.filter(
                    user_id=db_user.pk, verified=True
                ).exists()
                if not ignite_domain_account_trusted(
                    role_slug=role_slug,
                    is_active=db_user.is_active,
                    verified=verified,
                ):
                    email = UntrustedIgniteEmail(email)
            return _demote_if_excluded(
                role_slug,
                bool(db_user.is_staff),
                bool(db_user.is_superuser),
                email,
            )

        # Couldn't resolve a DB row — log it and fall back to whatever the
        # request object exposes. Without a row we can't check verification,
        # so an Ignite-domain email gets no domain grant on this path.
        logger.warning(
            "resolve_request_user_access: no DB user "
            "(pk=%r email=%r type=%s authed=%s)",
            pk,
            req_email,
            type(user).__name__,
            getattr(user, "is_authenticated", None),
        )
        fallback_email = req_email.lower()
        if is_ignite_domain_email(fallback_email):
            fallback_email = UntrustedIgniteEmail(fallback_email)
        return _demote_if_excluded(
            (getattr(getattr(user, "role", None), "slug", "") or "").lower()
            or None,
            bool(getattr(user, "is_staff", False)),
            bool(getattr(user, "is_superuser", False)),
            fallback_email,
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


class IsSparkAdminAccess(BasePermission):
    """Allow only Spark/Ignite admins (``_is_admin_access``, DB-backed).

    For BA operational data (pay, hours, live location) that brand/client
    users must not see. Re-reads the role from the DB, so a spark-admin
    whose JWT user didn't hydrate its role is not spuriously denied.
    """

    message = "Only Ignite admins have access to this."

    async def has_permission(self, source, info, **kwargs) -> bool:
        user = info.context.request.user
        if not user or not user.is_authenticated:
            return False
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )
        return _is_admin_access(role_slug, is_staff, is_super, email)
