"""Client recap sign-off — Looks good / Need more photos.

Stores status + comment on Recap / CustomRecap and pings ops. Used by
the public /r/:token POST and the logged-in GraphQL mutation.
"""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone as dj_tz

logger = logging.getLogger(__name__)

LOOKS_GOOD = "looks_good"
NEED_MORE_PHOTOS = "need_more_photos"
ALLOWED = {LOOKS_GOOD, NEED_MORE_PHOTOS}


def apply_signoff(recap, *, status: str, comment: str | None) -> Any:
    status = (status or "").strip()
    if status not in ALLOWED:
        raise ValueError("status must be looks_good or need_more_photos")
    recap.client_signoff_status = status
    recap.client_signoff_comment = (comment or "").strip()
    recap.client_signoff_at = dj_tz.now()
    recap.save(
        update_fields=[
            "client_signoff_status",
            "client_signoff_comment",
            "client_signoff_at",
            "updated_at",
        ]
    )
    return recap


def notify_ops_signoff(recap, *, kind: str) -> None:
    """Best-effort email to Spark admins + mapped RMM. Never raises."""
    try:
        from html import escape as _esc

        from events.mutations import _get_spark_admin_emails
        from utils.mailer import Envelope, Mailer

        admins = list(_get_spark_admin_emails() or [])
        event = getattr(recap, "event", None)
        rmm = getattr(event, "rmm_asigned", None) if event else None
        rmm_email = getattr(rmm, "email", None) if rmm else None
        if rmm_email and rmm_email not in admins:
            admins.append(rmm_email)
        name = getattr(recap, "name", None) or "Recap"
        status = recap.client_signoff_status
        label = "Looks good" if status == LOOKS_GOOD else "Need more photos"
        comment = (recap.client_signoff_comment or "").strip()
        if admins:
            html = (
                f"<div style='font-family:sans-serif;font-size:14px'>"
                f"<p>Client sign-off on <strong>{_esc(name)}</strong> ({kind}).</p>"
                f"<p><strong>{_esc(label)}</strong></p>"
                + (f"<p>“{_esc(comment[:800])}”</p>" if comment else "")
                + "</div>"
            )

            class _Mailer(Mailer):
                def envelope(self) -> Envelope:
                    return Envelope(
                        subject=f"Recap sign-off — {label} — {name}",
                        html=html,
                        to_emails=admins,
                    )

            _Mailer().send_now()
    except Exception:
        logger.exception("recap client sign-off notify failed")
    if getattr(recap, "client_signoff_status", None) == NEED_MORE_PHOTOS:
        notify_ambassador_need_photos(recap)


def _ambassador_for_recap(recap):
    """The BA on the recap row, else the first booked BA on the event."""
    amb = getattr(recap, "ambassador", None)
    if amb is not None:
        return amb
    event = getattr(recap, "event", None)
    if event is None:
        return None
    try:
        from ambassadors.models import AmbassadorEvent

        row = (
            AmbassadorEvent.objects.select_related("ambassador", "ambassador__user")
            .filter(event_id=event.id)
            .first()
        )
        return getattr(row, "ambassador", None) if row else None
    except Exception:
        logger.exception("recap sign-off: could not resolve event roster")
        return None


def notify_ambassador_need_photos(recap) -> None:
    """Email + push the assigned BA. Best-effort; never raises.

    There is no SMS gateway in Spark — the BA app push is the text
    equivalent. Email covers BAs who never installed the app.
    """
    try:
        from html import escape as _esc

        from ambassadors.push import _send_push_to_user_sync
        from utils.mailer import Envelope, Mailer

        amb = _ambassador_for_recap(recap)
        if amb is None:
            return
        user = getattr(amb, "user", None)
        to_email = (
            getattr(user, "email", None)
            or getattr(amb, "email", None)
            or ""
        ).strip()
        user_id = getattr(user, "id", None) or getattr(amb, "user_id", None)
        name = getattr(recap, "name", None) or "your recap"
        event = getattr(recap, "event", None)
        venue = (getattr(event, "name", None) or name)[:80]
        comment = (getattr(recap, "client_signoff_comment", None) or "").strip()
        body = (
            f"The client asked for more photos on {venue}."
            + (f" Note: {comment[:200]}" if comment else "")
        )
        if to_email:
            html = (
                f"<div style='font-family:Georgia,serif;font-size:15px;color:#0a0d09'>"
                f"<p>The client reviewed <strong>{_esc(name)}</strong> and "
                f"needs more photos.</p>"
                + (f"<p>“{_esc(comment[:800])}”</p>" if comment else "")
                + "<p>Please add them on the recap when you can.</p></div>"
            )

            class _BaMailer(Mailer):
                def envelope(self) -> Envelope:
                    return Envelope(
                        subject=f"More photos needed — {name}",
                        html=html,
                        to_emails=[to_email],
                    )

            _BaMailer().send_now()
        if user_id:
            _send_push_to_user_sync(
                int(user_id),
                title="More photos needed",
                body=body[:180],
                data={
                    "screen": "recap",
                    "eventUuid": str(getattr(event, "uuid", "") or ""),
                },
            )
    except Exception:
        logger.exception("recap client sign-off BA notify failed")
