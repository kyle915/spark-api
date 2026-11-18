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


# ---------------------------------------------------------------------------
# Brand Ambassador (BA) personal data.
#
# Brand ("client") users must not be able to list or scrape BA personal data
# (phone, email, address, location, profile text, ...). The web app talks to
# ONE schema (clients) for admins and brands alike, so this is decided per
# VIEWER, not per schema:
#
# - ``viewer_is_admin`` / ``is_brand_user``: DB-backed (resolve_request_user_
#   access), cached on the request so a list of 200 rows costs one lookup.
# - ``HideBaPii``: a field extension that returns a blank value instead of the
#   real one unless the viewer is an Ignite admin or the BA themselves. Blanks
#   keep the field's GraphQL type (``""`` for String!, ``[]`` for lists,
#   ``None`` for nullables), so a brand screen that selects the field still
#   renders; it just gets nothing.
# - ``HideEmailLikeName``: for display-name fields that fall back to the BA's
#   login email when no name is on file (walk-up logins embed the phone number
#   in that placeholder email). Non-admins get "Ambassador" instead.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from strawberry.extensions import FieldExtension  # noqa: E402
from strawberry.utils.await_maybe import await_maybe  # noqa: E402

_BA_PII_CACHE_ATTR = "_spark_ba_pii_cache"


def _request_from_info(info):
    context = getattr(info, "context", None)
    if isinstance(context, dict):
        return context.get("request")
    return getattr(context, "request", None)


def _ba_pii_cache(info) -> dict | None:
    """Per-request cache dict, keyed to the viewer so a reused request object
    can't hand one user's verdict to another."""
    request = _request_from_info(info)
    if request is None:
        return None
    user = getattr(request, "user", None)
    viewer_key = getattr(user, "pk", None)
    cache = getattr(request, _BA_PII_CACHE_ATTR, None)
    if not isinstance(cache, dict) or cache.get("viewer_key") != viewer_key:
        cache = {"viewer_key": viewer_key, "ba_users": {}}
        try:
            setattr(request, _BA_PII_CACHE_ATTR, cache)
        except Exception:
            return None
    return cache


async def _viewer_access(info) -> tuple[int | None, bool, str | None]:
    """(viewer_pk, is_admin, role_slug) for the request user, DB-backed and
    cached per request. Anonymous -> (None, False, None)."""
    request = _request_from_info(info)
    user = getattr(request, "user", None) if request is not None else None
    if not user or not getattr(user, "is_authenticated", False):
        return (None, False, None)
    cache = _ba_pii_cache(info)
    if cache is None:
        rs, st, su, em = await resolve_request_user_access(user)
        return (getattr(user, "pk", None), _is_admin_access(rs, st, su, em), rs)

    task = cache.get("viewer_task")
    if task is None:

        async def _resolve():
            rs, st, su, em = await resolve_request_user_access(user)
            return (getattr(user, "pk", None), _is_admin_access(rs, st, su, em), rs)

        # One lookup however many masked fields resolve concurrently.
        task = asyncio.ensure_future(_resolve())
        cache["viewer_task"] = task
    return await asyncio.shield(task)


async def viewer_is_admin(info) -> bool:
    """True when the request user is an Ignite admin (``_is_admin_access``)."""
    _pk, is_admin, _rs = await _viewer_access(info)
    return is_admin


async def is_brand_user(info) -> bool:
    """True for an authenticated, non-admin brand user (role slug "client")."""
    pk, is_admin, rs = await _viewer_access(info)
    return pk is not None and not is_admin and rs == "client"


async def _viewer_ambassador_id(info) -> int | None:
    """The request user's own Ambassador pk, if they are a BA (cached)."""
    pk, _is_admin, _rs = await _viewer_access(info)
    if pk is None:
        return None
    cache = _ba_pii_cache(info)
    if cache is not None and "viewer_ambassador_id" in cache:
        return cache["viewer_ambassador_id"]

    @sync_to_async
    def _load():
        from ambassadors.models import Ambassador

        return (
            Ambassador.objects.filter(user_id=pk).values_list("id", flat=True).first()
        )

    amb_id = await _load()
    if cache is not None:
        cache["viewer_ambassador_id"] = amb_id
    return amb_id


async def user_is_ambassador(info, user_id) -> bool:
    """True when ``user_id`` belongs to a BA (has an Ambassador row or the
    ambassador role). Cached per request."""
    if user_id is None:
        return False
    cache = _ba_pii_cache(info)
    known = cache["ba_users"] if cache is not None else {}
    if user_id in known:
        return known[user_id]

    @sync_to_async
    def _load():
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        User = get_user_model()
        return (
            User.objects.filter(pk=user_id)
            .filter(Q(ambassador__isnull=False) | Q(role__slug="ambassador"))
            .exists()
        )

    result = await _load()
    known[user_id] = result
    return result


async def can_view_ba_pii(
    info, *, owner_user_id=None, owner_ambassador_id=None
) -> bool:
    """Admins see BA personal data; so does the BA it belongs to. Nobody else
    (brand users, other BAs, anonymous callers)."""
    pk, is_admin, _rs = await _viewer_access(info)
    if is_admin:
        return True
    if pk is None:
        return False
    if owner_user_id is not None and owner_user_id == pk:
        return True
    if owner_ambassador_id is not None:
        return owner_ambassador_id == await _viewer_ambassador_id(info)
    return False


def _blank_value(blank):
    return blank() if callable(blank) else blank


def _dict_attr(name):
    """Owner getter reading a local column without triggering a lazy load
    (a deferred column read inside an async resolver would raise)."""

    def _get(source):
        value = getattr(source, "__dict__", {}).get(name)
        if value is None and isinstance(source, dict):
            value = source.get(name)
        return value

    return _get


class HideBaPii(FieldExtension):
    """Blank a BA personal-data field unless the viewer is an Ignite admin or
    the BA themselves.

    ``blank`` is the value non-admins get (a callable is called per use, so
    ``list`` gives a fresh ``[]``). ``owner_user`` / ``owner_ambassador`` map
    the parent object to the BA's user pk / Ambassador pk for the self-view
    exception. ``only_if_ambassador_user`` maps the parent to a user pk and
    masks only when that user is a BA (for user types shared by brand users,
    admins and BAs alike).
    """

    def __init__(
        self,
        blank=None,
        *,
        owner_user=None,
        owner_ambassador=None,
        only_if_ambassador_user=None,
    ):
        super().__init__()
        self.blank = blank
        self.owner_user = owner_user
        self.owner_ambassador = owner_ambassador
        self.only_if_ambassador_user = only_if_ambassador_user

    async def _allowed(self, source, info) -> bool:
        owner_user_id = self.owner_user(source) if self.owner_user else None
        owner_ambassador_id = (
            self.owner_ambassador(source) if self.owner_ambassador else None
        )
        if await can_view_ba_pii(
            info,
            owner_user_id=owner_user_id,
            owner_ambassador_id=owner_ambassador_id,
        ):
            return True
        if self.only_if_ambassador_user is not None:
            target = self.only_if_ambassador_user(source)
            return not await user_is_ambassador(info, target)
        return False

    async def resolve_async(self, next_, source, info, **kwargs):
        if await self._allowed(source, info):
            return await await_maybe(next_(source, info, **kwargs))
        return _blank_value(self.blank)


BA_NAME_PLACEHOLDER = "Ambassador"


class HideEmailLikeName(FieldExtension):
    """For BA display names that fall back to the login email: non-admins get
    ``BA_NAME_PLACEHOLDER`` instead of anything containing "@"."""

    async def resolve_async(self, next_, source, info, **kwargs):
        value = await await_maybe(next_(source, info, **kwargs))
        if isinstance(value, str) and "@" in value:
            if not await viewer_is_admin(info):
                return BA_NAME_PLACEHOLDER
        return value


def ba_pii_field(blank=None, **kwargs):
    """``strawberry.field`` for a plain (non-Django) BA personal-data
    attribute. Pass ``default=`` / ``default_factory=`` as usual."""
    return strawberry.field(extensions=[HideBaPii(blank)], **kwargs)
