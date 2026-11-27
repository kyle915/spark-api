"""Saved Report Views & Scheduled Custom Digest Exports (Friday broker/distributor emails).

Allows regional managers and brand admins to save filtered report slices
(e.g. ``"Texas Whole Foods · Last 90d"``) and optionally schedule automated
email digests with filtered CSV attachments.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from pilot.actions import (
    build_weekly_buyer_store_report,
    export_weekly_buyer_report_csv,
)
from pilot.models import SavedReportView
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from pilot.pos import system_workspace_access
from tenants.models import Tenant, parse_recipient_emails

VALID_SURFACES = {
    SavedReportView.SURFACE_BUYER_REPORT,
    SavedReportView.SURFACE_PROGRAM_REPORT,
    SavedReportView.SURFACE_SHELF_VOICE,
    SavedReportView.SURFACE_RETAILER_POS,
}
VALID_CADENCES = {
    SavedReportView.CADENCE_WEEKLY,
    SavedReportView.CADENCE_BIWEEKLY,
    SavedReportView.CADENCE_MONTHLY,
}
VALID_FORMATS = {
    SavedReportView.FORMAT_CSV,
    SavedReportView.FORMAT_SUMMARY,
}
DAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(val: str) -> str:
    return _SLUG_RE.sub("-", (val or "view").lower()).strip("-")[:48] or "view"


def sanitize_saved_view_filters(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only whitelisted scalar filter keys so stored JSON stays bounded & clean."""
    if not isinstance(raw, dict):
        return {}
    allowed_keys = (
        "range",
        "customStart",
        "customEnd",
        "retailerFilter",
        "marketFilter",
        "providerUuid",
        "storeFilter",
        "storeQuery",
        "targetMsrp",
    )
    out: dict[str, Any] = {}
    for k in allowed_keys:
        v = raw.get(k)
        if v is None:
            continue
        s = str(v).strip()[:120]
        if s:
            out[k] = s
    return out


def resolve_saved_view_window(
    filters: dict[str, Any],
    *,
    today: date | None = None,
) -> tuple[date | None, date | None, str]:
    """Resolve a saved view's relative or custom range into inclusive (start, end, label)."""
    ref = today or timezone.localdate()
    rng = str(filters.get("range") or "90d").strip().lower()
    if rng in {"30", "30d"}:
        start = ref - timedelta(days=29)
        return start, ref, f"{start.isoformat()} → {ref.isoformat()} (Last 30d)"
    if rng in {"90", "90d"}:
        start = ref - timedelta(days=89)
        return start, ref, f"{start.isoformat()} → {ref.isoformat()} (Last 90d)"
    if rng in {"180", "180d"}:
        start = ref - timedelta(days=179)
        return start, ref, f"{start.isoformat()} → {ref.isoformat()} (Last 180d)"
    if rng in {"12m", "365", "365d"}:
        start = ref - timedelta(days=364)
        return start, ref, f"{start.isoformat()} → {ref.isoformat()} (Last 12m)"
    if rng == "ytd":
        start = date(ref.year, 1, 1)
        return start, ref, f"{start.isoformat()} → {ref.isoformat()} (YTD)"
    if rng == "all":
        return None, None, "All time"
    if rng == "custom":
        cs = str(filters.get("customStart") or "").strip()[:10]
        ce = str(filters.get("customEnd") or "").strip()[:10]
        try:
            d1 = date.fromisoformat(cs) if cs else None
            d2 = date.fromisoformat(ce) if ce else None
            if d1 and d2 and d1 <= d2:
                return d1, d2, f"{d1.isoformat()} → {d2.isoformat()}"
        except ValueError:
            pass
    start = ref - timedelta(days=89)
    return start, ref, f"{start.isoformat()} → {ref.isoformat()} (Last 90d)"


def describe_saved_view_filters(filters: dict[str, Any]) -> str:
    parts: list[str] = []
    if filters.get("retailerFilter"):
        parts.append(f"Retailer: {filters['retailerFilter']}")
    if filters.get("marketFilter"):
        parts.append(f"Market: {filters['marketFilter']}")
    if filters.get("storeFilter") and filters["storeFilter"] != "all":
        parts.append(f"Status: {filters['storeFilter']}")
    if filters.get("targetMsrp"):
        parts.append(f"Target MSRP: ${filters['targetMsrp']}")
    if filters.get("storeQuery"):
        parts.append(f"Search: {filters['storeQuery']}")
    return " · ".join(parts) if parts else "All stores & markets"


def is_saved_view_due_today(
    view: SavedReportView,
    *,
    today: date | None = None,
) -> bool:
    """Return True if an enabled scheduled view should fire on ``today``."""
    if not view.schedule_enabled:
        return False
    ref = today or timezone.localdate()
    if view.schedule_day_of_week != ref.weekday():
        return False
    if view.last_sent_at is not None:
        last_day = timezone.localtime(view.last_sent_at).date()
        delta_days = (ref - last_day).days
        if delta_days < 1:
            return False
        if view.schedule_cadence == SavedReportView.CADENCE_BIWEEKLY and delta_days < 12:
            return False
        if view.schedule_cadence == SavedReportView.CADENCE_MONTHLY and delta_days < 25:
            return False
    return True


def list_saved_report_views(
    access: ResolvedWorkspaceAccess,
    *,
    surface: str | None = None,
) -> list[SavedReportView]:
    qs = SavedReportView.objects.filter(tenant=access.tenant).order_by("name", "id")
    if surface and surface.strip():
        qs = qs.filter(surface=surface.strip())
    return list(qs)


def upsert_saved_report_view(
    access: ResolvedWorkspaceAccess,
    *,
    view_uuid: str | None = None,
    name: str,
    surface: str = SavedReportView.SURFACE_BUYER_REPORT,
    filters: dict[str, Any] | None = None,
    schedule_enabled: bool = False,
    schedule_cadence: str = SavedReportView.CADENCE_WEEKLY,
    schedule_day_of_week: int = 4,
    export_format: str = SavedReportView.FORMAT_CSV,
    recipient_emails: str = "",
) -> SavedReportView:
    clean_name = (name or "").strip()[:120]
    if not clean_name:
        raise ValueError("Give your saved view a name.")

    clean_surface = (surface or SavedReportView.SURFACE_BUYER_REPORT).strip()
    if clean_surface not in VALID_SURFACES:
        raise ValueError(f"Unsupported report surface: {clean_surface!r}")

    clean_cadence = (schedule_cadence or SavedReportView.CADENCE_WEEKLY).strip()
    if clean_cadence not in VALID_CADENCES:
        raise ValueError(f"Unsupported schedule cadence: {clean_cadence!r}")

    clean_format = (export_format or SavedReportView.FORMAT_CSV).strip()
    if clean_format not in VALID_FORMATS:
        raise ValueError(f"Unsupported export format: {clean_format!r}")

    day_int = int(schedule_day_of_week)
    if day_int < 0 or day_int > 6:
        raise ValueError("Schedule day of week must be between 0 (Mon) and 6 (Sun).")

    parsed_emails = parse_recipient_emails(recipient_emails or "")
    if schedule_enabled and not parsed_emails:
        raise ValueError(
            "Add at least one recipient email (e.g. distributor or broker) before enabling scheduled digests."
        )

    clean_filters = sanitize_saved_view_filters(filters)

    if view_uuid:
        view = SavedReportView.objects.filter(
            tenant=access.tenant, uuid=view_uuid
        ).first()
        if view is None:
            raise ValueError("Saved view not found in this workspace.")
        view.name = clean_name
        view.surface = clean_surface
        view.filters = clean_filters
        view.schedule_enabled = bool(schedule_enabled)
        view.schedule_cadence = clean_cadence
        view.schedule_day_of_week = day_int
        view.export_format = clean_format
        view.recipient_emails = ", ".join(parsed_emails)
        view.save()
        action = "saved_view.updated"
    else:
        view = SavedReportView.objects.create(
            tenant=access.tenant,
            name=clean_name,
            surface=clean_surface,
            filters=clean_filters,
            schedule_enabled=bool(schedule_enabled),
            schedule_cadence=clean_cadence,
            schedule_day_of_week=day_int,
            export_format=clean_format,
            recipient_emails=", ".join(parsed_emails),
            created_by=access.user if getattr(access.user, "pk", None) else None,
        )
        action = "saved_view.created"

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="reporting",
        action=action,
        summary=(
            f"{'Updated' if view_uuid else 'Created'} saved view '{view.name}'"
            f" ({'scheduled ' + DAY_LABELS[day_int] if view.schedule_enabled else 'manual'})."
        ),
        metadata={"view_uuid": str(view.uuid), "surface": view.surface},
    )
    return view


def delete_saved_report_view(
    access: ResolvedWorkspaceAccess,
    *,
    view_uuid: str,
) -> bool:
    view = SavedReportView.objects.filter(
        tenant=access.tenant, uuid=view_uuid
    ).first()
    if view is None:
        return False
    name = view.name
    view.delete()
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="reporting",
        action="saved_view.deleted",
        summary=f"Deleted saved view '{name}'.",
        metadata={"view_uuid": str(view_uuid)},
    )
    return True


def _build_shelf_voice_csv(hub: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Store",
            "Market",
            "State",
            "Audited Visits",
            "OSA %",
            "Latest Status",
            "Missing Tag Visits",
            "OOS Gap Visits",
            "Latest Facings",
            "Latest Price",
            "Price Drift %",
            "Stockout Signal",
            "Competitor Promo",
        ]
    )
    for row in hub.get("stores") or []:
        writer.writerow(
            [
                row.get("store_name") or "",
                row.get("market") or "",
                row.get("state_code") or "",
                row.get("visit_count") or 0,
                f"{row['osa_rate_pct']}%" if row.get("osa_rate_pct") is not None else "",
                row.get("latest_status") or "",
                row.get("missing_tag_visits") or 0,
                row.get("oos_gap_visits") or 0,
                row.get("latest_facings") if row.get("latest_facings") is not None else "",
                f"${row['latest_price']}" if row.get("latest_price") else "",
                f"{row['price_drift_pct']}%" if row.get("price_drift_pct") is not None else "",
                row.get("stockout_signal") or "",
                row.get("competitor_promo") or "",
            ]
        )
    return buf.getvalue()


def send_saved_report_view_digest(
    view: SavedReportView,
    *,
    dry_run: bool = False,
    force: bool = False,
    override_email: str = "",
) -> dict[str, Any]:
    """Execute a saved view's filter slice and send its formatted digest email."""
    from pilot.envelopes import SavedViewDigestMailer

    tenant = view.tenant
    recipients = (
        parse_recipient_emails(override_email)
        if override_email.strip()
        else parse_recipient_emails(view.recipient_emails or "")
    )
    if not recipients:
        return {"status": "skipped_no_recipients", "recipients": []}

    filters = view.filters if isinstance(view.filters, dict) else {}
    period_start, period_end, period_label = resolve_saved_view_window(filters)
    filter_summary = describe_saved_view_filters(filters)
    access = system_workspace_access(tenant)

    metrics: list[tuple[str, str]]
    csv_text: str | None = None
    row_count = 0

    if view.surface == SavedReportView.SURFACE_SHELF_VOICE:
        from recaps.shelf_voice_hub import build_shelf_voice_hub

        hub = build_shelf_voice_hub(tenant.id, period_start, period_end)
        stores = hub.get("stores") or []
        row_count = len(stores)
        metrics = [
            ("Audited shelf visits", str(hub.get("audited_visits") or 0)),
            (
                "On-shelf availability (OSA)",
                f"{hub['osa_rate_pct']}%" if hub.get("osa_rate_pct") is not None else "—",
            ),
            (
                "Price-tag compliance",
                f"{hub['price_tag_rate_pct']}%"
                if hub.get("price_tag_rate_pct") is not None
                else "—",
            ),
            ("Missing price tags", str(hub.get("missing_tag_count") or 0)),
            ("OOS shelf gaps", str(hub.get("oos_gaps_count") or 0)),
            (
                "Median shelf price",
                f"${hub['median_price']}" if hub.get("median_price") else "—",
            ),
            ("Competitor market signals", str(len(hub.get("competitor_markets") or []))),
        ]
        if view.export_format == SavedReportView.FORMAT_CSV:
            csv_text = _build_shelf_voice_csv(hub)
        has_content = bool(
            (hub.get("audited_visits") or 0) > 0
            or (hub.get("competitor_markets") or [])
            or (hub.get("competitor_promos") or [])
        )
    else:
        report = build_weekly_buyer_store_report(
            access,
            period_start=period_start,
            period_end=period_end,
            retailer_filter=filters.get("retailerFilter") or None,
            market_filter=filters.get("marketFilter") or None,
            provider_uuid=filters.get("providerUuid") or None,
        )
        rows = report.get("rows") or []
        row_count = len(rows)
        metrics = [
            ("Stores in slice", str(row_count)),
            ("Visits in window", str(report.get("total_visits") or 0)),
            ("Supported brand cost", f"${report.get('total_supported_cost') or '0.00'}"),
            (
                "Retailer POS data",
                "Available" if report.get("pos_available") else "Withheld (no POS file)",
            ),
        ]
        if view.export_format == SavedReportView.FORMAT_CSV:
            csv_text = export_weekly_buyer_report_csv(report)
        has_content = bool(row_count or (report.get("total_visits") or 0))

    if not has_content and not force:
        if not dry_run:
            view.last_send_status = "skipped_empty"
            view.last_send_error = ""
            view.save(update_fields=["last_send_status", "last_send_error", "updated_at"])
        return {
            "status": "skipped_empty",
            "recipients": recipients,
            "row_count": row_count,
            "period_label": period_label,
        }

    if dry_run:
        return {
            "status": "dry_run",
            "recipients": recipients,
            "row_count": row_count,
            "period_label": period_label,
        }

    filename = f"{_slug(tenant.name)}_{_slug(view.name)}.csv"
    mailer = SavedViewDigestMailer(
        recipients=recipients,
        tenant_name=tenant.name or "Brand",
        view_name=view.name,
        filter_summary=filter_summary,
        period_label=period_label,
        metrics=metrics,
        csv_text=csv_text,
        csv_filename=filename,
    )
    sent = mailer.send_now()
    now = timezone.now()
    if sent is True:
        view.last_sent_at = now
        view.last_send_status = "sent"
        view.last_send_error = ""
        view.save(
            update_fields=[
                "last_sent_at",
                "last_send_status",
                "last_send_error",
                "updated_at",
            ]
        )
        return {
            "status": "sent",
            "recipients": recipients,
            "row_count": row_count,
            "period_label": period_label,
        }

    view.last_send_status = "failed"
    view.last_send_error = "Mail provider did not accept delivery."
    view.save(update_fields=["last_send_status", "last_send_error", "updated_at"])
    return {
        "status": "failed",
        "recipients": recipients,
        "row_count": row_count,
        "period_label": period_label,
    }


def send_due_saved_view_digests(
    *,
    dry_run: bool = False,
    force: bool = False,
    tenant_id: int | None = None,
    view_uuid: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    """Run scheduled saved-view digests due on ``today`` across active tenants."""
    qs = SavedReportView.objects.select_related("tenant").filter(
        schedule_enabled=True,
        tenant__in=Tenant.active(),
    )
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    if view_uuid:
        qs = qs.filter(uuid=view_uuid)

    ref = today or timezone.localdate()
    summary = {"checked": 0, "sent": 0, "skipped": 0, "failed": 0, "details": []}
    for view in qs.iterator():
        if not force and not is_saved_view_due_today(view, today=ref):
            continue
        summary["checked"] += 1
        res = send_saved_report_view_digest(view, dry_run=dry_run, force=force)
        st = res.get("status", "failed")
        if st in {"sent", "dry_run"}:
            summary["sent"] += 1
        elif st.startswith("skipped"):
            summary["skipped"] += 1
        else:
            summary["failed"] += 1
        summary["details"].append(
            {
                "view_uuid": str(view.uuid),
                "name": view.name,
                "tenant_id": view.tenant_id,
                **res,
            }
        )
    return summary
