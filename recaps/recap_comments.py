"""Client comment/question thread on a shared recap.

Mirrors ``recaps.client_signoff``: store the row, then best-effort notify
Ignite ops (Spark admins + assigned RMM) via the existing Mailer pattern.
Never raises on the notify path.
"""

from __future__ import annotations

import logging
import re

from events.torch_portal import omit_torch_recap_skips
from recaps.models import RecapComment

logger = logging.getLogger(__name__)

MAX_BODY = 2000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def comments_payload(kind: str, recap) -> list[dict]:
    qs = RecapComment.objects.filter(
        custom_recap_id=recap.pk if kind == "custom" else None,
        recap_id=recap.pk if kind != "custom" else None,
    ).order_by("created_at", "id")
    return [
        {
            "id": str(c.uuid),
            "authorName": c.author_name or "",
            "body": c.body,
            "createdAt": c.created_at.isoformat() if c.created_at else None,
        }
        for c in qs
    ]


def create_comment(recap, *, kind: str, author_name=None, author_email=None, body=None) -> RecapComment:
    body = (body or "").strip()
    if not body:
        raise ValueError("A comment is required.")
    if len(body) > MAX_BODY:
        raise ValueError(f"Comment is too long (max {MAX_BODY} characters).")
    name = (author_name or "").strip()[:120]
    email = (author_email or "").strip()[:254]
    if email and not _EMAIL_RE.match(email):
        raise ValueError("That email doesn't look right.")
    kwargs = {"author_name": name, "author_email": email, "body": body}
    if kind == "custom":
        comment = RecapComment.objects.create(custom_recap=recap, **kwargs)
    else:
        comment = RecapComment.objects.create(recap=recap, **kwargs)
    return comment


def notify_ops_comment(recap, *, kind: str, comment: RecapComment) -> None:
    """Best-effort email to Spark admins + mapped RMM. Never raises."""
    try:
        from html import escape as _esc

        from events.mutations import _get_spark_admin_emails
        from recaps.client_signoff import _admin_recap_url, _tenant_brand_name
        from utils.mailer import Envelope, Mailer

        admins = list(_get_spark_admin_emails() or [])
        event = getattr(recap, "event", None)
        rmm = getattr(event, "rmm_asigned", None) if event else None
        rmm_email = getattr(rmm, "email", None) if rmm else None
        if rmm_email and rmm_email not in admins:
            admins.append(rmm_email)
        tenant = getattr(event, "tenant", None) if event is not None else None
        if tenant is None:
            tenant = getattr(recap, "tenant", None)
        admins = omit_torch_recap_skips(admins, tenant)
        if not admins:
            return
        name = getattr(recap, "name", None) or "Recap"
        brand = _tenant_brand_name(recap)
        subject = (
            f"Recap question — {brand} — {name}" if brand else f"Recap question — {name}"
        )
        recap_url = _admin_recap_url(recap, kind=kind)
        author = comment.author_name or comment.author_email or "A client"
        html = (
            "<div style='font-family:sans-serif;font-size:14px'>"
            f"<p>New client comment on <strong>{_esc(name)}</strong> ({kind})"
            + (f" for <strong>{_esc(brand)}</strong>" if brand else "")
            + f" from {_esc(author)}:</p>"
            f"<p>“{_esc(comment.body[:800])}”</p>"
            + (f"<p><a href=\"{_esc(recap_url)}\">Open recap in Spark</a></p>" if recap_url else "")
            + "</div>"
        )

        class _Mailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(subject=subject, html=html, to_emails=admins)

        _Mailer().send_now()
    except Exception:
        logger.exception("recap comment notify failed")
