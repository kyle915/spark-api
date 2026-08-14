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
        if not admins:
            return
        name = getattr(recap, "name", None) or "Recap"
        status = recap.client_signoff_status
        label = "Looks good" if status == LOOKS_GOOD else "Need more photos"
        comment = (recap.client_signoff_comment or "").strip()
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
