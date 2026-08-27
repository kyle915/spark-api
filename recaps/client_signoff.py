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


def _tenant_brand_name(recap) -> str:
    """Display name for the client brand (Tenant.name), same as recap emails."""
    event = getattr(recap, "event", None)
    tenant = getattr(event, "tenant", None) if event is not None else None
    if tenant is None:
        tenant = getattr(recap, "tenant", None)
    return (getattr(tenant, "name", None) or "").strip()


def _admin_recap_url(recap, *, kind: str) -> str:
    """Admin Spark permalink ops use to open the signed-off recap.

    Same path scheme as RecapReadyForReviewAdminMailer:
    custom → /recap/view-custom/:uuid, legacy → /recap/view/:uuid.
    """
    from django.conf import settings

    uuid = getattr(recap, "uuid", None)
    if not uuid:
        return ""
    base = str(
        getattr(
            settings,
            "ADMIN_FRONTEND_URL",
            "https://admin.igniteproductions.co",
        )
    ).rstrip("/")
    path = (
        f"/recap/view-custom/{uuid}"
        if kind == "custom"
        else f"/recap/view/{uuid}"
    )
    return f"{base}{path}"


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
        brand = _tenant_brand_name(recap)
        status = recap.client_signoff_status
        label = "Looks good" if status == LOOKS_GOOD else "Need more photos"
        comment = (recap.client_signoff_comment or "").strip()
        if admins:
            if brand:
                lead = (
                    f"Client sign-off on <strong>{_esc(brand)}</strong> — "
                    f"<strong>{_esc(name)}</strong> ({kind})."
                )
                subject = f"Recap sign-off — {label} — {brand} — {name}"
            else:
                lead = f"Client sign-off on <strong>{_esc(name)}</strong> ({kind})."
                subject = f"Recap sign-off — {label} — {name}"
            recap_url = _admin_recap_url(recap, kind=kind)
            link_html = (
                f"<p><a href=\"{_esc(recap_url)}\">Open recap in Spark</a></p>"
                if recap_url
                else ""
            )
            html = (
                f"<div style='font-family:sans-serif;font-size:14px'>"
                f"<p>{lead}</p>"
                f"<p><strong>{_esc(label)}</strong></p>"
                + (f"<p>“{_esc(comment[:800])}”</p>" if comment else "")
                + link_html
                + "</div>"
            )

            class _Mailer(Mailer):
                def envelope(self) -> Envelope:
                    return Envelope(
                        subject=subject,
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
        # Best-effort push: WARNING, not exception — must not page the
        # error monitor for a BA notify hiccup.
        logger.warning("recap client sign-off BA notify failed", exc_info=True)
