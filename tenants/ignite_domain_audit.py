"""Read-only audit: was the Ignite-domain admin grant (or the open register
mutations) abused?

Until the fix, ``_is_admin_access`` treated ANY ``@igniteproductions.co``
address as a cross-tenant admin, and public sign-up paths accepted that
domain without proving the inbox (createPublicAmbassador / walkupSignUp /
register returned or skipped the activation step). The ``register`` mutations
also let an anonymous caller pick the spark-admin role for any email.

This builds a report of what the database can show:

- every account on the Ignite domain (or a subdomain / case / trailing-dot
  variant), with the signals that matter: role, verified, active, staff
  flags, created/last login, brand memberships, refresh-token activity, an
  Ambassador profile (the public-signup fingerprint), and whether it had admin
  under the OLD rule vs the NEW rule. ``suspicious`` = ambassador role OR
  unverified OR inactive OR has an Ambassador profile OR a case-duplicate of
  another account. ``loses_admin`` = staff-looking accounts the new rule no
  longer treats as admin (fix with ``verify_user`` BEFORE shipping);
- spark-admin role / is_staff accounts on OTHER domains (the register
  role-pick hole) — id, email domain only, dates, verified;
- every spark_mcp McpConnection with its tool-call count and last use.

READ-ONLY. Emails are printed only for Ignite-domain accounts (staff domain);
everyone else appears as an id (plus the email domain for elevated accounts).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from django.apps import apps
from django.db.models import Count, Max

from utils.graphql.permissions import (
    IGNITE_ADMIN_EXCLUDE,
    UntrustedIgniteEmail,
    _is_admin_access,
    ignite_domain_account_trusted,
    is_ignite_domain_email,
    normalize_email,
)

IGNITE_EMAIL_REGEX = r"@([a-z0-9-]+\.)*igniteproductions\.co\.?\s*$"


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _old_rule_admin(role_slug, is_staff, is_super, email) -> bool:
    """The pre-fix rule: staff / superuser / spark-admin / ANY domain email."""
    email = normalize_email(email)
    if email in IGNITE_ADMIN_EXCLUDE:
        return False
    return bool(
        is_staff or is_super or role_slug == "spark-admin" or email.endswith("@igniteproductions.co")
    )


def build_ignite_domain_audit(*, cap: int = 500) -> dict[str, Any]:
    User = apps.get_model("tenants", "User")
    TenantedUser = apps.get_model("tenants", "TenantedUser")
    UserStatus = apps.get_model("gqlauth", "UserStatus")
    RefreshToken = apps.get_model("gqlauth", "RefreshToken")
    Ambassador = apps.get_model("ambassadors", "Ambassador")

    users = list(
        User.objects.filter(email__iregex=IGNITE_EMAIL_REGEX)
        .select_related("role")
        .order_by("id")
    )
    ids = [u.id for u in users]
    verified = set(
        UserStatus.objects.filter(user_id__in=ids, verified=True).values_list("user_id", flat=True)
    )
    has_amb = set(Ambassador.objects.filter(user_id__in=ids).values_list("user_id", flat=True))
    memberships: dict[int, list[dict]] = defaultdict(list)
    for uid, tid, tname, active in TenantedUser.objects.filter(user_id__in=ids).values_list(
        "user_id", "tenant_id", "tenant__name", "is_active"
    ):
        memberships[uid].append({"tenant_id": tid, "tenant": tname, "active": active})
    tokens = {
        row["user_id"]: row
        for row in RefreshToken.objects.filter(user_id__in=ids)
        .values("user_id")
        .annotate(count=Count("id"), last=Max("created"))
    }
    norm_counts = Counter(
        normalize_email(e)
        for e in User.objects.filter(email__iregex=IGNITE_EMAIL_REGEX).values_list("email", flat=True)
    )

    rows = []
    for u in users:
        role = (getattr(u.role, "slug", "") or "").lower() or None
        is_verified = u.id in verified
        trusted = ignite_domain_account_trusted(
            role_slug=role, is_active=u.is_active, verified=is_verified
        )
        email_for_rule = (u.email or "").lower()
        if not trusted:
            email_for_rule = UntrustedIgniteEmail(email_for_rule)
        old_admin = _old_rule_admin(role, u.is_staff, u.is_superuser, u.email)
        new_admin = _is_admin_access(role, u.is_staff, u.is_superuser, email_for_rule)
        reasons = []
        if role == "ambassador":
            reasons.append("ambassador-role")
        if not is_verified:
            reasons.append("unverified")
        if not u.is_active:
            reasons.append("inactive")
        if u.id in has_amb:
            reasons.append("has-ambassador-profile")
        if norm_counts[normalize_email(u.email)] > 1:
            reasons.append("case-duplicate")
        if (u.email or "") != normalize_email(u.email):
            reasons.append("non-canonical-email")
        tok = tokens.get(u.id) or {}
        rows.append(
            {
                "id": u.id,
                "email": u.email,
                "role": role,
                "is_active": u.is_active,
                "is_staff": u.is_staff,
                "is_superuser": u.is_superuser,
                "verified": is_verified,
                "created_at": _iso(getattr(u, "date_joined", None)),
                "last_login": _iso(u.last_login),
                "refresh_tokens": tok.get("count", 0),
                "last_refresh_token_at": _iso(tok.get("last")),
                "memberships": memberships.get(u.id, []),
                "admin_before_fix": old_admin,
                "admin_after_fix": new_admin,
                "suspicious": bool(reasons),
                "reasons": reasons,
            }
        )

    suspicious = [r for r in rows if r["suspicious"]]
    loses_admin = [
        r
        for r in rows
        if r["admin_before_fix"] and not r["admin_after_fix"] and not r["suspicious"]
    ]
    loses_admin_any = [r for r in rows if r["admin_before_fix"] and not r["admin_after_fix"]]

    # Elevated accounts on OTHER domains: the register(role: SPARK) hole made
    # spark-admins for any address. Domain only, no local part.
    elevated_other = []
    elevated_qs = (
        User.objects.filter(role__slug="spark-admin")
        .exclude(email__iregex=IGNITE_EMAIL_REGEX)
        .union(
            User.objects.filter(is_staff=True).exclude(email__iregex=IGNITE_EMAIL_REGEX),
        )
    )
    elevated_ids = [u.id for u in elevated_qs]
    ev_verified = set(
        UserStatus.objects.filter(user_id__in=elevated_ids, verified=True).values_list(
            "user_id", flat=True
        )
    )
    for u in User.objects.filter(id__in=elevated_ids).select_related("role").order_by("id"):
        domain = (u.email or "").rsplit("@", 1)[-1].lower() if "@" in (u.email or "") else ""
        elevated_other.append(
            {
                "id": u.id,
                "email_domain": domain,
                "role": (getattr(u.role, "slug", "") or "").lower() or None,
                "is_staff": u.is_staff,
                "is_superuser": u.is_superuser,
                "is_active": u.is_active,
                "verified": u.id in ev_verified,
                "created_at": _iso(getattr(u, "date_joined", None)),
                "last_login": _iso(u.last_login),
            }
        )

    mcp = _mcp_section(cap=cap)

    return {
        "summary": {
            "ignite_domain_accounts": len(rows),
            "ignite_domain_suspicious": len(suspicious),
            "ignite_domain_suspicious_with_login": sum(
                1 for r in suspicious if r["last_login"] or r["refresh_tokens"]
            ),
            "staff_accounts_losing_admin_after_fix": len(loses_admin),
            "elevated_accounts_other_domains": len(elevated_other),
            "mcp_connections": mcp["count"],
            "mcp_tool_calls": mcp["tool_calls"],
        },
        "ignite_domain_suspicious": suspicious[:cap],
        "staff_losing_admin_after_fix": loses_admin[:cap],
        "all_losing_admin_after_fix_ids": [r["id"] for r in loses_admin_any][:cap],
        "ignite_domain_accounts": rows[:cap],
        "elevated_accounts_other_domains": elevated_other[:cap],
        "mcp": mcp,
    }


def _mcp_section(*, cap: int) -> dict[str, Any]:
    try:
        McpConnection = apps.get_model("spark_mcp", "McpConnection")
        McpToolCall = apps.get_model("spark_mcp", "McpToolCall")
    except LookupError:
        return {"count": 0, "tool_calls": 0, "connections": [], "note": "spark_mcp not installed"}

    calls = {
        row["connection_id"]: row
        for row in McpToolCall.objects.values("connection_id").annotate(
            n=Count("id"), last=Max("created_at")
        )
    }
    conns = []
    for c in (
        McpConnection.objects.select_related("user", "user__role", "tenant", "client")
        .order_by("id")[:cap]
    ):
        email = (getattr(c.user, "email", "") or "")
        call = calls.get(c.id) or {}
        conns.append(
            {
                "id": c.id,
                "user_id": c.user_id,
                # Staff-domain emails only; everyone else stays an id.
                "user_email": email if is_ignite_domain_email(email) else None,
                "user_role": (getattr(getattr(c.user, "role", None), "slug", "") or "") or None,
                "tenant_id": c.tenant_id,
                "tenant": getattr(c.tenant, "name", None),
                "client_name": getattr(c.client, "client_name", "") or "",
                "created_at": _iso(c.created_at),
                "last_used_at": _iso(c.last_used_at),
                "revoked_at": _iso(c.revoked_at),
                "tool_calls": call.get("n", 0),
                "last_tool_call_at": _iso(call.get("last")),
            }
        )
    return {
        "count": McpConnection.objects.count(),
        "tool_calls": McpToolCall.objects.count(),
        "orphan_tool_calls": (calls.get(None) or {}).get("n", 0),
        "connections": conns,
    }
