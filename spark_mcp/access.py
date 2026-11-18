"""Who may connect an AI app, to which brand, and token validation."""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.utils import timezone

from . import conf
from .models import McpConnection, McpToken, digest


def _role_slug(db_user) -> str:
    return (getattr(getattr(db_user, "role", None), "slug", None) or "").lower()


def _is_ambassador(db_user) -> bool:
    from tenants.models import Role
    from utils.utils import ROLE_ID

    return db_user.role_id == ROLE_ID.Ambassadors or _role_slug(db_user) == Role.AMBASSADOR_SLUG


def is_admin(user) -> bool:
    """Ignite staff for MCP purposes (seeing and revoking everyone's
    connections): is_staff, is_superuser, or the spark-admin role, read from
    the database. Deliberately NOT the app-wide rule, which also trusts an
    @igniteproductions.co email address that anyone can sign up with.
    Ambassador-role accounts are never staff here."""
    from tenants.models import Role

    if not user:
        return False
    db_user = get_user_model().objects.select_related("role").filter(pk=user.pk).first()
    if db_user is None or not db_user.is_active or _is_ambassador(db_user):
        return False
    return bool(
        db_user.is_staff or db_user.is_superuser or _role_slug(db_user) == Role.SPARK_ADMIN_SLUG
    )


def is_brand_user(user) -> bool:
    from tenants.models import Role

    db_user = get_user_model().objects.select_related("role").filter(pk=user.pk).first()
    return db_user is not None and _role_slug(db_user) == Role.CLIENT_SLUG


def is_verified(user) -> bool:
    """The gate the app's own sign-in uses: gqlauth's tokenAuth refuses
    anyone whose ``UserStatus.verified`` is not True ("Please verify your
    account"). A missing status row counts as unverified, as it does there."""
    from gqlauth.models import UserStatus

    return bool(user) and UserStatus.objects.filter(user_id=user.pk, verified=True).exists()


def eligible_tenants(user):
    """Brands this person may connect: the brands they are an active member
    of (TenantedUser). That holds for Ignite staff too: there is no
    "any brand" access through an AI app; staff connect the brands they're
    members of, like everyone else. Brand ambassadors, unverified accounts
    and inactive accounts get none."""
    from tenants.models import Tenant

    none = Tenant.objects.none()
    if not user:
        return none
    db_user = get_user_model().objects.select_related("role").filter(pk=user.pk).first()
    # Ambassadors are refused before anything else is considered.
    if db_user is None or not db_user.is_active or _is_ambassador(db_user):
        return none
    if not is_verified(db_user):
        return none
    if not (is_brand_user(db_user) or is_admin(db_user)):
        return none
    allow = conf.allowed_tenant_ids()
    if allow is not None and not allow:
        return none  # rollout list empty: no brand may connect
    qs = Tenant.objects.filter(
        tenanted_users__user_id=db_user.pk, tenanted_users__is_active=True
    ).distinct()
    if allow is not None:
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
