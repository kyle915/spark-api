"""Notify Ignite ops when a client edits event/request notes.

Fires from ``updateEvent`` / ``updateRequest`` / ``updateEventWithRequest``
when the actor is a client-role user and the notes text actually changed.
Best-effort: never raises into the mutation path.
"""

from __future__ import annotations

import logging
from html import escape as _esc
from typing import Any

from django.conf import settings
from django.utils import timezone as dj_tz

from tenants.models import Role

logger = logging.getLogger(__name__)

# Cap email body so a huge paste doesn't blow Resend limits.
_MAX_NOTES_CHARS = 4000


def normalize_notes(value: str | None) -> str:
    return (value or "").strip()


def notes_changed(before: str | None, after: str | None) -> bool:
    return normalize_notes(before) != normalize_notes(after)


def is_client_user(user: Any) -> bool:
    """True when the actor is a client-role Spark user (not ops/BA)."""
    if user is None:
        return False
    role = getattr(user, "role", None)
    slug = (getattr(role, "slug", None) or "").strip().lower()
    if slug == Role.CLIENT_SLUG:
        return True
    # Fallback when role relation isn't select_related.
    from utils.utils import ROLE_ID

    return getattr(user, "role_id", None) == ROLE_ID.Client


def _admin_base_url() -> str:
    return str(
        getattr(settings, "ADMIN_FRONTEND_URL", "https://admin.igniteproductions.co")
    ).rstrip("/")


def _event_url(event) -> str:
    uuid = getattr(event, "uuid", None)
    if not uuid:
        return ""
    return f"{_admin_base_url()}/event/view/{uuid}"


def _request_url(request) -> str:
    uuid = getattr(request, "uuid", None)
    if not uuid:
        return ""
    return f"{_admin_base_url()}/request/view/{uuid}"


def _tenant_name(tenant) -> str:
    return (getattr(tenant, "name", None) or "").strip() or "—"


def _venue_label(*, event=None, request=None) -> str:
    retailer = None
    if event is not None:
        retailer = getattr(event, "retailer", None)
    if retailer is None and request is not None:
        retailer = getattr(request, "retailer", None)
    name = (getattr(retailer, "name", None) or "").strip()
    if name:
        return name
    for obj in (event, request):
        if obj is None:
            continue
        for attr in ("name", "address"):
            value = (getattr(obj, attr, None) or "").strip()
            if value:
                return value
    return "—"


def _date_label(*, event=None, request=None) -> str:
    for obj in (event, request):
        if obj is None:
            continue
        date = getattr(obj, "date", None)
        if date is None:
            continue
        try:
            return date.strftime("%Y-%m-%d")
        except Exception:
            return str(date)[:32]
    return "—"


def _clip(text: str) -> str:
    if len(text) <= _MAX_NOTES_CHARS:
        return text
    return text[: _MAX_NOTES_CHARS - 1] + "…"


def _ops_recipient_emails(tenant) -> list[str]:
    """Active Ignite staff: spark-admin roll-up + every @igniteproductions.co.

    Matches the request-CC "notify ops" pattern, then applies central
    outbound policy (CC suppress + Torch suppress).
    """
    from events.mutations import _get_ignite_domain_emails, _get_spark_admin_emails
    from events.outbound_policy import apply_outbound_policy

    combined = list(_get_spark_admin_emails() or []) + list(
        _get_ignite_domain_emails() or []
    )
    deduped: list[str] = []
    seen: set[str] = set()
    for email in combined:
        normalized = (email or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return apply_outbound_policy(deduped, tenant=tenant, kind="generic")


def _actor_email(user: Any) -> str:
    return (getattr(user, "email", None) or "").strip() or "unknown"


def _actor_label(user: Any) -> str:
    try:
        full = (user.get_full_name() or "").strip()
    except Exception:
        full = ""
    email = _actor_email(user)
    if full and email and full.lower() != email.lower():
        return f"{full} ({email})"
    return full or email


def notify_ops_client_notes_edit(
    *,
    user: Any,
    tenant: Any,
    notes_before: str | None,
    notes_after: str | None,
    event=None,
    request=None,
    source: str = "event",
) -> bool:
    """Email Ignite ops about a client notes change. Returns True if sent.

    No-ops when notes are unchanged, the actor isn't a client, or no
    recipients remain after suppress. Never raises.
    """
    try:
        if not is_client_user(user):
            return False
        if not notes_changed(notes_before, notes_after):
            return False

        recipients = _ops_recipient_emails(tenant)
        if not recipients:
            return False

        before = _clip(normalize_notes(notes_before) or "(empty)")
        after = _clip(normalize_notes(notes_after) or "(empty)")
        brand = _tenant_name(tenant)
        venue = _venue_label(event=event, request=request)
        when_date = _date_label(event=event, request=request)
        edited_at = dj_tz.now().strftime("%Y-%m-%d %H:%M UTC")
        actor = _actor_label(user)
        actor_email = _actor_email(user)

        event_link = _event_url(event) if event is not None else ""
        request_link = _request_url(request) if request is not None else ""
        if not request_link and event is not None:
            linked = getattr(event, "request", None)
            if linked is not None:
                request_link = _request_url(linked)

        event_id = getattr(event, "id", None) if event is not None else None
        request_id = getattr(request, "id", None) if request is not None else None
        if request_id is None and event is not None:
            request_id = getattr(event, "request_id", None)

        field_label = "event notes" if source == "event" else "request notes"
        subject = f"Client edited {field_label} — {brand} — {venue}"

        id_bits: list[str] = []
        if event_id is not None:
            id_bits.append(f"Event #{event_id}")
        if request_id is not None:
            id_bits.append(f"Request #{request_id}")
        ids_line = " · ".join(id_bits) if id_bits else "—"

        link_bits: list[str] = []
        if event_link:
            link_bits.append(
                f'<a href="{_esc(event_link)}">Open event in Spark</a>'
            )
        if request_link:
            link_bits.append(
                f'<a href="{_esc(request_link)}">Open request in Spark</a>'
            )
        links_html = (" · ".join(link_bits)) if link_bits else ""

        html = (
            "<div style='font-family:sans-serif;font-size:14px;line-height:1.45'>"
            f"<p>A client updated <strong>{_esc(field_label)}</strong> on "
            f"<strong>{_esc(brand)}</strong>.</p>"
            "<table style='border-collapse:collapse;margin:12px 0'>"
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>Tenant</td>"
            f"<td>{_esc(brand)}</td></tr>"
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>IDs</td>"
            f"<td>{_esc(ids_line)}</td></tr>"
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>Venue</td>"
            f"<td>{_esc(venue)}</td></tr>"
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>Date</td>"
            f"<td>{_esc(when_date)}</td></tr>"
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>Edited by</td>"
            f"<td>{_esc(actor)}</td></tr>"
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>When</td>"
            f"<td>{_esc(edited_at)}</td></tr>"
            "</table>"
            + (f"<p>{links_html}</p>" if links_html else "")
            + "<p style='margin-bottom:4px'><strong>Before</strong></p>"
            f"<pre style='white-space:pre-wrap;background:#f4f4f5;padding:12px;"
            f"border-radius:6px'>{_esc(before)}</pre>"
            "<p style='margin-bottom:4px'><strong>After</strong></p>"
            f"<pre style='white-space:pre-wrap;background:#f4f4f5;padding:12px;"
            f"border-radius:6px'>{_esc(after)}</pre>"
            "</div>"
        )

        from utils.mailer import Envelope, Mailer

        class _Mailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(
                    subject=subject,
                    html=html,
                    to_emails=recipients,
                    headers={"Reply-To": actor_email or "staffing@igniteproductions.co"},
                )

        _Mailer().send_now()
        return True
    except Exception:
        logger.exception("client notes edit notify failed")
        return False


async def maybe_notify_and_log_client_notes_edit(
    *,
    user: Any,
    tenant: Any,
    notes_before: str | None,
    notes_after: str | None,
    event=None,
    request=None,
    source: str = "event",
) -> None:
    """Async wrapper: notify ops + best-effort activity-log the notes change."""
    from asgiref.sync import sync_to_async

    if not is_client_user(user) or not notes_changed(notes_before, notes_after):
        return

    await sync_to_async(notify_ops_client_notes_edit)(
        user=user,
        tenant=tenant,
        notes_before=notes_before,
        notes_after=notes_after,
        event=event,
        request=request,
        source=source,
    )

    # Activity log lives on Request — use the linked request when present.
    log_request = request
    if log_request is None and event is not None:
        log_request = getattr(event, "request", None)
        if log_request is None and getattr(event, "request_id", None):
            try:
                from events.models import Request

                log_request = await sync_to_async(
                    Request.objects.select_related("tenant").filter(
                        id=event.request_id
                    ).first
                )()
            except Exception:
                log_request = None
    if log_request is None:
        return
    try:
        from events.activity_log import alog
        from events.models import RequestActivityLog

        field = "event_notes" if source == "event" else "notes"
        await alog(
            request=log_request,
            actor_user=user,
            kind=RequestActivityLog.KIND_UPDATED,
            summary=f"Client edited {field.replace('_', ' ')}",
            metadata={
                "fields": [field],
                "notes_before": _clip(normalize_notes(notes_before)),
                "notes_after": _clip(normalize_notes(notes_after)),
                "source": source,
            },
        )
    except Exception:
        logger.exception("client notes edit activity log failed")
