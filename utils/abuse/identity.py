"""Who is calling: real client IP, user agent, and a cached user identity.

Client IP on Cloud Run
----------------------
Google's front end APPENDS the address it received the connection from to
``X-Forwarded-For``. Anything to the left of that was sent by the client and
is spoofable (``curl -H 'X-Forwarded-For: 1.2.3.4'``). So the trustworthy
value is the entry ``ABUSE_TRUSTED_PROXY_COUNT`` from the RIGHT:

- 1 (default): service hit directly at ``*.run.app`` / a domain mapping —
  the last entry is the real client.
- 2: an external HTTPS load balancer sits in front (it appends its own hop).

``events/checkin_views._client_ip`` takes the FIRST entry, which is the
spoofable one; new code should use :func:`client_ip`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.conf import settings


def _meta(request) -> dict:
    meta = getattr(request, "META", None)
    if meta is None:
        meta = getattr(getattr(request, "wsgi_request", None), "META", None)
    return meta or {}


def client_ip(request) -> str:
    meta = _meta(request)
    xff = meta.get("HTTP_X_FORWARDED_FOR", "") or ""
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    trusted = max(int(getattr(settings, "ABUSE_TRUSTED_PROXY_COUNT", 1) or 1), 1)
    if hops:
        idx = len(hops) - trusted
        return hops[idx] if idx >= 0 else hops[0]
    return (meta.get("REMOTE_ADDR") or "").strip() or "unknown"


def user_agent(request) -> str:
    return (_meta(request).get("HTTP_USER_AGENT") or "")[:300]


@dataclass(frozen=True)
class Identity:
    user_id: int | None
    email: str
    tenant_id: int | None
    is_admin: bool
    role_slug: str | None

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    @property
    def is_brand(self) -> bool:
        """Authenticated, not an Ignite/staff/spark-admin user."""
        return self.user_id is not None and not self.is_admin


ANONYMOUS = Identity(None, "", None, False, None)

# Per-process cache: admin status / tenant rarely change and this runs on
# every GraphQL request. 60s staleness is acceptable for rate-limit tiering.
_CACHE: dict[tuple, tuple[float, Identity]] = {}
_TTL = 60.0


def _load_identity(user) -> Identity:
    from tenants.models import TenantedUser
    from utils.graphql.permissions import _demote_if_excluded, _is_admin_access

    pk = getattr(user, "pk", None)
    if pk is None:
        return ANONYMOUS
    from django.contrib.auth import get_user_model

    db_user = get_user_model().objects.select_related("role").filter(pk=pk).first()
    if db_user is None:
        return ANONYMOUS
    role_slug = (getattr(db_user.role, "slug", "") or "").lower() if db_user.role else None
    role_slug, is_staff, is_super, email = _demote_if_excluded(
        role_slug, bool(db_user.is_staff), bool(db_user.is_superuser),
        (db_user.email or "").lower(),
    )
    tenant_id = (
        TenantedUser.objects.filter(user_id=pk, is_active=True)
        .order_by("tenant_id")
        .values_list("tenant_id", flat=True)
        .first()
    )
    return Identity(
        user_id=pk,
        email=email,
        tenant_id=tenant_id,
        is_admin=_is_admin_access(role_slug, is_staff, is_super, email),
        role_slug=role_slug,
    )


def resolve_identity(user) -> Identity:
    """Sync. Anonymous/None users → ANONYMOUS. Cached 60s per user id."""
    if user is None or not getattr(user, "is_authenticated", False):
        return ANONYMOUS
    # Key on the request user's own columns too, so a role / staff / email
    # change on the JWT-loaded row takes effect immediately.
    cache_key = (
        getattr(user, "pk", None),
        (getattr(user, "email", "") or "").lower(),
        getattr(user, "role_id", None),
        bool(getattr(user, "is_staff", False)),
        bool(getattr(user, "is_superuser", False)),
    )
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    ident = _load_identity(user)
    if len(_CACHE) > 5000:
        _CACHE.clear()
    _CACHE[cache_key] = (now, ident)
    return ident


aresolve_identity = sync_to_async(resolve_identity)


def clear_identity_cache() -> None:
    _CACHE.clear()
