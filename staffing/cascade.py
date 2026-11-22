"""No-Show Backup Cascade watchdog & fail-open alert dispatcher.

Detects:
1. Approved shifts missing check-in ``checkin_grace_minutes`` (default 15)
   past ``event.start_time`` while the shift is still active.
2. Approved shifts still unconfirmed within ``unconfirmed_lead_minutes``
   (default 120) before ``event.start_time``.

For each flagged shift (idempotently per ``(event, ambassador_event, reason)``):
- Atomically reserves a ``NoShowCascadeAlert`` row (``select_for_update`` on
  the event) **before** notifying anyone.
- Pulls top-ranked backup BAs from :func:`staffing.matching.compute_smart_ba_matches`.
- Dispatches email (via ``utils.mailer.Mailer`` / Resend) + SMS alerts to
  backup BAs and ops contacts, respecting Ambassador SMS opt-outs and Quo
  conversation STOP checks, cleanly failing open with a structured
  ``delivery_audit`` entry whenever SMS or email is not configured.
"""

from __future__ import annotations

import html as html_lib
import logging
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ambassadors.models import Ambassador, AmbassadorEvent, Attendance
from ambassadors.shift_sms import (
    _record_opt_out,
    normalize_us_phone,
    quo_says_opted_out,
    send_quo_sms,
    sms_configured,
)
from events.cancelled import active_event_q
from events.models import Event
from staffing.matching import compute_smart_ba_matches
from staffing.models import NoShowCascadeAlert
from utils.mailer import Envelope, Mailer

logger = logging.getLogger(__name__)


def _email_configured() -> bool:
    """True when Resend/mailpit is ready, or Django locmem is active in tests."""
    backend = (getattr(settings, "EMAIL_BACKEND", "") or "").lower()
    if "locmem" in backend:
        return True
    driver = (getattr(settings, "MAIL_DRIVER", "") or "").lower()
    if driver == "resend" and bool((getattr(settings, "RESEND_API_KEY", "") or "").strip()):
        return True
    if driver in {"mailpit", "smtp", ""}:
        # Mailpit / local drivers are fine for non-prod; Resend is the prod path.
        return True
    return False


def _ops_alert_emails(event: Event) -> list[str]:
    configured = list(getattr(settings, "CHECKIN_HOURS_NOTIFY_EMAILS", []) or [])
    out: list[str] = []
    seen: set[str] = set()
    for raw in configured:
        addr = (raw or "").strip()
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


def _send_email_fail_open(
    *,
    to_email: str,
    subject: str,
    body: str,
    role: str,
    now_iso: str,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {
            "channel": "email",
            "recipient": to_email,
            "role": role,
            "status": "dry_run",
            "detail": "Dry run — email not sent",
            "at": now_iso,
        }
    if not _email_configured():
        return {
            "channel": "email",
            "recipient": to_email,
            "role": role,
            "status": "skipped_not_configured",
            "detail": "Email provider not configured (fail-open)",
            "at": now_iso,
        }
    try:
        body_html = (
            '<pre style="font-family:inherit;white-space:pre-wrap;margin:0">'
            f"{html_lib.escape(body)}</pre>"
        )

        class _CascadeMailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(
                    subject=subject,
                    html=body_html,
                    to_emails=[to_email],
                )

        # House Resend/mailpit driver — NOT django.core.mail (no SMTP on Cloud Run).
        result = _CascadeMailer().send_now()
        if result is False:
            return {
                "channel": "email",
                "recipient": to_email,
                "role": role,
                "status": "failed",
                "detail": "Mail driver did not accept the message",
                "at": now_iso,
            }
        return {
            "channel": "email",
            "recipient": to_email,
            "role": role,
            "status": "sent" if result is not None else "skipped_no_recipients",
            "detail": "Dispatched via MailDrivers",
            "at": now_iso,
        }
    except Exception as exc:  # noqa: BLE001 — fail open with audit log
        logger.warning("noshow cascade email failed to=%s: %s", to_email, exc)
        return {
            "channel": "email",
            "recipient": to_email,
            "role": role,
            "status": "failed",
            "detail": str(exc)[:240],
            "at": now_iso,
        }


def _send_sms_fail_open(
    *,
    phone: str | None,
    recipient_label: str,
    body: str,
    role: str,
    now_iso: str,
    dry_run: bool,
    ambassador: Ambassador | None = None,
) -> dict[str, Any]:
    normalized = normalize_us_phone(phone)
    recipient = normalized or recipient_label
    if dry_run:
        return {
            "channel": "sms",
            "recipient": recipient,
            "role": role,
            "status": "dry_run",
            "detail": "Dry run — SMS not sent",
            "at": now_iso,
        }
    if ambassador is not None and getattr(ambassador, "sms_opt_out", False):
        return {
            "channel": "sms",
            "recipient": recipient,
            "role": role,
            "status": "skipped_opt_out",
            "detail": "Ambassador SMS opt-out on file",
            "at": now_iso,
        }
    if not normalized:
        return {
            "channel": "sms",
            "recipient": recipient_label,
            "role": role,
            "status": "skipped_no_phone",
            "detail": "No valid US phone on file",
            "at": now_iso,
        }
    if not sms_configured():
        return {
            "channel": "sms",
            "recipient": normalized,
            "role": role,
            "status": "skipped_not_configured",
            "detail": "SMS provider not configured (fail-open)",
            "at": now_iso,
        }
    try:
        stopped = quo_says_opted_out(normalized)
    except Exception:  # noqa: BLE001 — hold the text; fail open with audit
        logger.warning(
            "noshow cascade: could not read Quo replies for %s",
            recipient_label,
            exc_info=True,
        )
        return {
            "channel": "sms",
            "recipient": normalized,
            "role": role,
            "status": "held_opt_out_unknown",
            "detail": "Could not verify Quo conversation opt-out; SMS held",
            "at": now_iso,
        }
    if stopped:
        if ambassador is not None:
            _record_opt_out(ambassador)
        return {
            "channel": "sms",
            "recipient": normalized,
            "role": role,
            "status": "skipped_opt_out",
            "detail": "Quo conversation shows STOP",
            "at": now_iso,
        }
    try:
        msg_id = send_quo_sms(to=normalized, body=body)
        return {
            "channel": "sms",
            "recipient": normalized,
            "role": role,
            "status": "sent",
            "detail": msg_id or "Dispatched",
            "at": now_iso,
        }
    except Exception as exc:  # noqa: BLE001 — fail open with audit log
        logger.warning("noshow cascade sms failed to=%s: %s", normalized, exc)
        return {
            "channel": "sms",
            "recipient": normalized,
            "role": role,
            "status": "failed",
            "detail": str(exc)[:240],
            "at": now_iso,
        }


def _active_alert_qs(
    *,
    event_id: int,
    trigger_reason: str,
    ambassador_event: AmbassadorEvent | None,
):
    qs = NoShowCascadeAlert.objects.filter(
        event_id=event_id,
        trigger_reason=trigger_reason,
    ).exclude(status=NoShowCascadeAlert.STATUS_DISMISSED)
    if ambassador_event is not None:
        qs = qs.filter(ambassador_event_id=ambassador_event.id)
    else:
        qs = qs.filter(ambassador_event__isnull=True)
    return qs.order_by("-created_at")


def trigger_noshow_cascade_for_event(
    *,
    event: Event,
    trigger_reason: str = NoShowCascadeAlert.REASON_MANUAL,
    ambassador_event: AmbassadorEvent | None = None,
    triggered_by=None,
    backup_limit: int = 5,
    notes: str = "",
    dry_run: bool = False,
    now: datetime | None = None,
    force: bool = False,
) -> tuple[NoShowCascadeAlert | None, bool, str]:
    """Create and dispatch a ``NoShowCascadeAlert`` for an event.

    Returns ``(alert_or_none, created_new, message)``.
    Idempotent by default: if an alert already exists for the same
    ``(event, ambassador_event, trigger_reason)`` (unless ``force=True``),
    returns ``(existing, False, ...)`` without re-dispatching.

    Reservations are atomic: the event row is locked and an OPEN alert is
    inserted **before** any SMS/email is sent.
    """
    now = now or timezone.now()
    now_iso = now.isoformat()

    minutes_past: int | None = None
    if getattr(event, "start_time", None) is not None:
        minutes_past = int(round((now - event.start_time).total_seconds() / 60.0))

    if dry_run:
        matches = compute_smart_ba_matches(
            tenant_id=event.tenant_id,
            event_id=event.id,
            limit=max(1, min(int(backup_limit), 15)),
            now=now,
        )
        ops_emails = _ops_alert_emails(event)
        return (
            None,
            False,
            f"Dry run: would notify {len(matches)} backup BA(s) and {len(ops_emails)} ops contact(s).",
        )

    reserved: NoShowCascadeAlert | None = None
    created_new = False
    with transaction.atomic():
        # Serialize overlapping cron + manual triggers for this event.
        Event.objects.select_for_update().filter(pk=event.id).first()
        if not force:
            existing = _active_alert_qs(
                event_id=event.id,
                trigger_reason=trigger_reason,
                ambassador_event=ambassador_event,
            ).first()
            if existing is not None:
                return (
                    existing,
                    False,
                    "Backup cascade alert already active for this shift.",
                )

        reserved = NoShowCascadeAlert.objects.create(
            tenant_id=event.tenant_id,
            event=event,
            ambassador_event=ambassador_event,
            missing_ambassador=(
                ambassador_event.ambassador if ambassador_event is not None else None
            ),
            triggered_by=(
                triggered_by
                if triggered_by and getattr(triggered_by, "is_authenticated", False)
                else None
            ),
            trigger_reason=trigger_reason,
            status=NoShowCascadeAlert.STATUS_OPEN,
            minutes_past_start=minutes_past,
            notified_backup_bas=[],
            delivery_audit=[],
            ops_notified_emails=[],
            notes=(notes or "").strip()[:1000],
        )
        created_new = True

    matches = compute_smart_ba_matches(
        tenant_id=event.tenant_id,
        event_id=event.id,
        limit=max(1, min(int(backup_limit), 15)),
        now=now,
    )

    ba_ids = [
        int(m["ba_numeric_id"])
        for m in matches
        if m.get("ba_numeric_id") is not None
    ]
    ambassadors_by_id = {
        int(ba.id): ba
        for ba in Ambassador.objects.filter(id__in=ba_ids).select_related("user")
    }

    backup_snapshots = [
        {
            "baId": m["ba_id"],
            "baUuid": m["ba_uuid"],
            "name": m["name"],
            "score": m["score"],
            "reasons": m["reasons"],
            "summaryReason": m["summary_reason"],
            "email": m["email"] or "",
            "phone": m["phone"] or "",
        }
        for m in matches
    ]

    brand_name = getattr(getattr(event, "tenant", None), "name", "Spark")
    venue = (getattr(event, "name", "") or "Upcoming shift").strip()
    reason_label = dict(NoShowCascadeAlert.REASON_CHOICES).get(
        trigger_reason, trigger_reason
    )

    delivery_audit: list[dict[str, Any]] = []
    for ba in matches:
        amb = ambassadors_by_id.get(int(ba.get("ba_numeric_id") or 0))
        sms_text = (
            f"Spark Backup Alert: {brand_name} needs backup coverage at {venue}. "
            f"Match: {ba['summary_reason']}. Reply or open Spark if you can cover."
        )
        delivery_audit.append(
            _send_sms_fail_open(
                phone=ba.get("phone"),
                recipient_label=ba["name"],
                body=sms_text,
                role="backup_ba",
                now_iso=now_iso,
                dry_run=False,
                ambassador=amb,
            )
        )
        if ba.get("email"):
            delivery_audit.append(
                _send_email_fail_open(
                    to_email=ba["email"],
                    subject=f"[Spark Backup Needed] {brand_name} — {venue}",
                    body=(
                        f"Hi {ba['name']},\n\n"
                        f"A shift at {venue} ({brand_name}) needs immediate backup coverage "
                        f"({reason_label}).\n"
                        f"Why you were selected: {ba['summary_reason']}.\n"
                    ),
                    role="backup_ba",
                    now_iso=now_iso,
                    dry_run=False,
                )
            )

    ops_emails = _ops_alert_emails(event)
    backup_lines = "\n".join(
        f"  - {b['name']} (score {b['score']}): {b['summaryReason']}"
        for b in backup_snapshots
    ) or "  (No backup BAs found in workspace pool)"
    for ops_email in ops_emails:
        delivery_audit.append(
            _send_email_fail_open(
                to_email=ops_email,
                subject=f"[Spark No-Show Cascade] {brand_name} — {venue}",
                body=(
                    f"No-show backup cascade triggered for {venue} ({brand_name}).\n"
                    f"Reason: {reason_label}\n"
                    f"Top backup BAs alerted ({len(backup_snapshots)}):\n{backup_lines}\n"
                ),
                role="ops",
                now_iso=now_iso,
                dry_run=False,
            )
        )

    assert reserved is not None
    reserved.notified_backup_bas = backup_snapshots
    reserved.delivery_audit = delivery_audit
    reserved.ops_notified_emails = ops_emails
    reserved.status = NoShowCascadeAlert.STATUS_DISPATCHED
    reserved.dispatched_at = now
    reserved.save(
        update_fields=[
            "notified_backup_bas",
            "delivery_audit",
            "ops_notified_emails",
            "status",
            "dispatched_at",
            "updated_at",
        ]
    )
    return (
        reserved,
        created_new,
        f"Backup cascade dispatched to {len(backup_snapshots)} backup BA(s).",
    )


def evaluate_noshow_cascades(
    *,
    now: datetime | None = None,
    checkin_grace_minutes: int = 15,
    unconfirmed_lead_minutes: int = 120,
    lookback_hours: int = 6,
    backup_limit: int = 5,
    dry_run: bool = False,
    tenant_id: int | None = None,
) -> dict[str, Any]:
    """Scan active shifts and trigger idempotent backup cascades."""
    now = now or timezone.now()
    grace = max(1, int(checkin_grace_minutes))
    lead = max(1, int(unconfirmed_lead_minutes))
    lookback = max(1, int(lookback_hours))

    base_qs = (
        AmbassadorEvent.objects.select_related(
            "event", "event__tenant", "ambassador", "ambassador__user"
        )
        .filter(
            active_event_q(),
            is_approved=True,
            event__isnull=False,
            ambassador__isnull=False,
        )
        .order_by("event__start_time", "id")
    )
    if tenant_id is not None:
        base_qs = base_qs.filter(tenant_id=tenant_id)

    missing_checkin_rows = list(
        base_qs.filter(
            event__start_time__lte=now - timedelta(minutes=grace),
            event__start_time__gte=now - timedelta(hours=lookback),
        )
    )
    # Keep only shifts still in progress (end_time null or in the future)
    missing_checkin_rows = [
        ae
        for ae in missing_checkin_rows
        if getattr(ae.event, "end_time", None) is None or ae.event.end_time > now
    ]

    unconfirmed_rows = list(
        base_qs.filter(
            confirmed_at__isnull=True,
            event__start_time__gt=now,
            event__start_time__lte=now + timedelta(minutes=lead),
        )
    )

    all_event_ids = {
        ae.event_id for ae in (*missing_checkin_rows, *unconfirmed_rows)
    }
    checked_in_pairs: set[tuple[int, int]] = set()
    if all_event_ids:
        checked_in_pairs = {
            (int(a), int(e))
            for a, e in Attendance.objects.filter(
                event_id__in=all_event_ids
            ).values_list("ambassador_id", "event_id")
            if a is not None and e is not None
        }

    triggered = 0
    already_alerted = 0
    would_trigger = 0
    alert_uuids: list[str] = []

    flagged: list[tuple[AmbassadorEvent, str]] = []
    for ae in missing_checkin_rows:
        if (int(ae.ambassador_id), int(ae.event_id)) not in checked_in_pairs:
            flagged.append((ae, NoShowCascadeAlert.REASON_MISSING_CHECKIN_15M))
    for ae in unconfirmed_rows:
        if (int(ae.ambassador_id), int(ae.event_id)) not in checked_in_pairs:
            flagged.append((ae, NoShowCascadeAlert.REASON_UNCONFIRMED_NEAR_START))

    for ae, reason in flagged:
        alert, created, _msg = trigger_noshow_cascade_for_event(
            event=ae.event,
            trigger_reason=reason,
            ambassador_event=ae,
            backup_limit=backup_limit,
            dry_run=dry_run,
            now=now,
            force=False,
        )
        if dry_run:
            if alert is not None:
                already_alerted += 1
            else:
                would_trigger += 1
        elif created and alert is not None:
            triggered += 1
            alert_uuids.append(str(alert.uuid))
        else:
            already_alerted += 1

    return {
        "checked_missing_checkin": len(missing_checkin_rows),
        "checked_unconfirmed": len(unconfirmed_rows),
        "flagged": len(flagged),
        "triggered": triggered,
        "would_trigger": would_trigger,
        "already_alerted": already_alerted,
        "dry_run": dry_run,
        "alert_uuids": alert_uuids,
    }
