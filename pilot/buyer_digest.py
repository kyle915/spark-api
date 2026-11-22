"""Scheduled weekly buyer store-report digest (email + CSV attachment).

Distinct from the client field-marketing weekly digest
(``tenants`` / ``recaps.weekly_digest``). This wraps
``build_weekly_buyer_store_report`` for brand ops / finance recipients.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from pilot.actions import (
    build_weekly_buyer_store_report,
    export_weekly_buyer_report_csv,
)
from pilot.models import WorkspaceMembership, WorkspaceProfile
from pilot.permissions import ResolvedWorkspaceAccess
from pilot.pos import system_workspace_access
from pilot.workspaces import ensure_workspace_profile
from tenants.models import parse_recipient_emails


def buyer_digest_recipients(profile: WorkspaceProfile) -> list[str]:
    """Resolve recipient emails for the weekly buyer digest.

    Prefer an explicit override list on the workspace profile; otherwise use
    active brand_admin + finance_reviewer membership emails.
    """
    override = parse_recipient_emails(profile.buyer_digest_recipient_emails or "")
    if override:
        return override
    emails: list[str] = []
    seen: set[str] = set()
    for mem in (
        WorkspaceMembership.objects.select_related("user")
        .filter(
            tenant=profile.tenant,
            status=WorkspaceMembership.STATUS_ACTIVE,
            workspace_role__in={
                WorkspaceMembership.ROLE_BRAND_ADMIN,
                WorkspaceMembership.ROLE_FINANCE_REVIEWER,
            },
        )
        .order_by("id")
    ):
        email = (getattr(mem.user, "email", "") or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


def set_buyer_weekly_digest(
    access: ResolvedWorkspaceAccess,
    *,
    enabled: bool,
    recipient_emails: str | None = None,
) -> WorkspaceProfile:
    if not access.can_manage_pos and not access.is_brand_admin:
        raise PermissionError(
            "You do not have permission to configure the weekly buyer digest."
        )
    profile = ensure_workspace_profile(access.tenant)
    profile.buyer_weekly_digest_enabled = bool(enabled)
    update_fields = ["buyer_weekly_digest_enabled", "updated_at"]
    if recipient_emails is not None:
        # Normalize to comma-separated unique emails.
        parsed = parse_recipient_emails(recipient_emails)
        profile.buyer_digest_recipient_emails = ", ".join(parsed)
        update_fields.append("buyer_digest_recipient_emails")
    if enabled and not buyer_digest_recipients(profile):
        raise ValueError(
            "Add at least one recipient (buyer digest emails or a brand admin "
            "/ finance reviewer membership) before enabling."
        )
    profile.save(update_fields=update_fields)
    return profile


def build_buyer_digest_payload(
    access: ResolvedWorkspaceAccess,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, Any]:
    report = build_weekly_buyer_store_report(
        access,
        period_start=period_start,
        period_end=period_end,
    )
    rows = report.get("rows") or []
    csv_text = export_weekly_buyer_report_csv(report)
    return {
        "report": report,
        "rows": rows,
        "row_count": len(rows),
        "has_content": bool(rows) or bool(report.get("total_visits")),
        "csv_text": csv_text,
        "period_start": report.get("period_start"),
        "period_end": report.get("period_end"),
        "total_visits": report.get("total_visits") or 0,
        "total_supported_cost": report.get("total_supported_cost") or "0.00",
        "pos_available": bool(report.get("pos_available")),
    }


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_tenant(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "brand").lower()).strip("-")
    return slug[:48] or "brand"


def send_buyer_digest_for_tenant(
    tenant,
    *,
    dry_run: bool = False,
    force: bool = False,
    only_email: str = "",
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, Any]:
    """Build + optionally email one tenant's weekly buyer digest."""
    from pilot.envelopes import BuyerWeeklyDigestMailer

    profile = ensure_workspace_profile(tenant)
    if not profile.buyer_weekly_digest_enabled:
        return {"status": "skipped_opt_out"}

    recipients = buyer_digest_recipients(profile)
    if only_email:
        matched = [
            r for r in recipients if r.strip().lower() == only_email.strip().lower()
        ]
        if not matched:
            return {"status": "skipped_only_email_missing", "recipients": recipients}
        recipients = matched[:1]
    if not recipients:
        return {"status": "skipped_no_recipients"}

    access = system_workspace_access(tenant)
    # Default window: trailing 7 days ending yesterday (Mon cron → prior week).
    eff_end = period_end or (timezone.localdate() - timedelta(days=1))
    eff_start = period_start or (eff_end - timedelta(days=6))
    payload = build_buyer_digest_payload(
        access, period_start=eff_start, period_end=eff_end
    )
    if not payload["has_content"] and not force:
        return {
            "status": "skipped_quiet",
            "period_start": str(eff_start),
            "period_end": str(eff_end),
        }

    if dry_run:
        return {
            "status": "dry_run",
            "recipients": recipients,
            "row_count": payload["row_count"],
            "total_visits": payload["total_visits"],
            "period_start": str(eff_start),
            "period_end": str(eff_end),
        }

    filename = (
        f"buyer-store-report_{_slugify_tenant(tenant.name)}_"
        f"{eff_start.isoformat()}_{eff_end.isoformat()}.csv"
    )
    mailer = BuyerWeeklyDigestMailer(
        recipients=recipients,
        tenant_name=tenant.name or "",
        period_start=eff_start,
        period_end=eff_end,
        row_count=payload["row_count"],
        total_visits=int(payload["total_visits"] or 0),
        total_supported_cost=str(payload["total_supported_cost"]),
        pos_available=bool(payload["pos_available"]),
        csv_text=payload["csv_text"],
        csv_filename=filename,
    )
    # send_now avoids RQ/Redis dependency on Cloud Run cron paths.
    # Mailer returns False on provider rejection, None when nothing was sent
    # (empty/suppressed recipients). Only True means a successful dispatch.
    sent = mailer.send_now()
    if sent is not True:
        return {
            "status": "failed_send" if sent is False else "skipped_not_sent",
            "recipients": recipients,
            "row_count": payload["row_count"],
            "period_start": str(eff_start),
            "period_end": str(eff_end),
        }
    return {
        "status": "sent",
        "recipients": recipients,
        "row_count": payload["row_count"],
        "period_start": str(eff_start),
        "period_end": str(eff_end),
    }
