"""Who may connect an AI app, to which brand, and token validation."""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.utils import timezone

from . import conf
from .models import McpConnection, McpToken, digest


def is_admin(user) -> bool:
    """Ignite admin, decided from the database (same rule as the API)."""
    from utils.graphql.permissions import _is_admin_access

    db_user = get_user_model().objects.select_related("role").filter(pk=user.pk).first()
    if db_user is None:
        return False
    role_slug = (getattr(db_user.role, "slug", None) or "").lower()
    return _is_admin_access(role_slug, db_user.is_staff, db_user.is_superuser, db_user.email)


def is_brand_user(user) -> bool:
    db_user = get_user_model().objects.select_related("role").filter(pk=user.pk).first()
    role_slug = (getattr(getattr(db_user, "role", None), "slug", None) or "").lower()
    return role_slug == "client"


def eligible_tenants(user):
    """Brands this person may connect. Brand (client) users: brands they are
    an active member of. Ignite admins: any brand (for support and testing).
    Everyone else — brand ambassadors included — none."""
    from tenants.models import Tenant

    if not user or not user.is_active:
        return Tenant.objects.none()
    if is_admin(user):
        qs = Tenant.objects.all()
    elif is_brand_user(user):
        qs = Tenant.objects.filter(
            tenanted_users__user_id=user.pk, tenanted_users__is_active=True
        ).distinct()
    else:
        return Tenant.objects.none()
    allow = conf.allowed_tenant_ids()
    if allow:
        qs = qs.filter(id__in=allow)
    return qs.order_by("name")


def user_may_use(connection: McpConnection) -> bool:
    """Re-checked on every call: removing someone from a brand, deactivating
    them, or dropping the brand from the rollout list cuts the app off."""
    return eligible_tenants(connection.user).filter(id=connection.tenant_id).exists()


@dataclass
class BearerResult:
    connection: McpConnection | None
    error: str = ""
    description: str = ""


def authenticate_bearer(header: str) -> BearerResult:
    if not header or not header.lower().startswith("bearer "):
        return BearerResult(None, "", "Sign-in required.")
    raw = header[7:].strip()
    if not raw:
        return BearerResult(None, "invalid_token", "Empty token.")
    token = (
        McpToken.objects.select_related(
            "connection", "connection__client", "connection__tenant", "connection__user"
        )
        .filter(token_hash=digest(raw), kind=McpToken.KIND_ACCESS)
        .first()
    )
    now = timezone.now()
    if token is None or token.revoked_at is not None or token.expires_at <= now:
        return BearerResult(None, "invalid_token", "The access token is invalid or expired.")
    if token.resource.rstrip("/").lower() != conf.resource_url().rstrip("/").lower():
        return BearerResult(None, "invalid_token", "The access token is for a different resource.")
    connection = token.connection
    if not connection.is_active or connection.client.disabled:
        return BearerResult(None, "invalid_token", "This connection was revoked.")
    if not user_may_use(connection):
        return BearerResult(
            None, "invalid_token", "This account no longer has access to this brand."
        )
    return BearerResult(connection)
