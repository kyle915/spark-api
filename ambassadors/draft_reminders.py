"""One reminder for a walk-up recap draft that never got submitted.

A BA who clocked out — or whose shift day simply passed — with answers
still sitting in ``WalkupRecapDraft`` gets a single email a few hours
later linking the standing check-in on the client host, where the same
draft restores. Filed recaps never trigger it, an empty draft never
triggers it, and ``reminder_sent_at`` stamps the one send so hourly
cron runs cannot repeat it.
"""

from __future__ import annotations

import html
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def draft_has_answers(payload: dict | None) -> bool:
    """Python twin of the check-in page's `draftHasAnswers`."""
    if not isinstance(payload, dict):
        return False
    if any(
        isinstance(p, dict) and (p.get("blobName") or p.get("id"))
        for p in payload.get("photos") or []
    ):
        return True
    if str(payload.get("engagements") or "").strip():
        return True
    if payload.get("usedCorpoCard") in (True, False):
        return True
    store = payload.get("store") if isinstance(payload.get("store"), dict) else {}
    if str(store.get("name") or "").strip() or str(store.get("number") or "").strip():
        return True
    if any(str(v or "").strip() for v in (payload.get("samples") or {}).values()):
        return True
    co = payload.get("coBrands") if isinstance(payload.get("coBrands"), dict) else {}
    if co.get("enabled") in (True, False) or co.get("selected"):
        return True
    for value in (payload.get("values") or {}).values():
        if isinstance(value, list):
            if value:
                return True
        elif str(value or "").strip():
            return True
    return False


def standing_checkin_url(event) -> str | None:
    """Standing tenant code wins; fall back to the event's own walk-up code."""
    tenant = getattr(event, "tenant", None)
    code = (getattr(tenant, "checkin_code", None) or "").strip()
    if not code:
        code = (getattr(event, "walkup_code", None) or "").strip()
    if not code:
        return None
    base = (getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "") or "").rstrip("/")
    if not base:
        base = "https://client.igniteproductions.co"
    return f"{base}/checkin/{code}"


def _shift_passed(event, now) -> bool:
    for stamp in (
        getattr(event, "end_time", None),
        getattr(event, "date", None),
        getattr(event, "start_time", None),
    ):
        if stamp is not None:
            return stamp <= now
    return False


def _send_email(*, to_email: str, subject: str, body_html: str) -> None:
    from utils.mailer import Envelope, Mailer

    class _DraftReminderMailer(Mailer):
        def envelope(self) -> Envelope:
            return Envelope(subject=subject, html=body_html, to_emails=[to_email])

    _DraftReminderMailer().send_now()


def send_walkup_draft_reminders(
    *,
    now=None,
    grace_hours: int = 3,
    max_age_hours: int = 168,
    dry_run: bool = False,
    send_email=None,
) -> dict:
    """Send at most one reminder per unsubmitted, answered draft.

    Returns a counts dict; ``sent`` only counts stamped sends (or, in
    dry-run, drafts that would be sent).
    """
    from ambassadors.checkin_web import clock_state
    from ambassadors.models import WalkupRecapDraft
    from recaps.filed import has_filed_recap
    from utils.mailer import valid_recipient_emails

    now = now or timezone.now()
    grace_hours = max(0, int(grace_hours))
    max_age_hours = max(grace_hours + 1, int(max_age_hours))
    send = send_email or _send_email

    drafts = list(
        WalkupRecapDraft.objects.select_related(
            "event", "event__tenant", "ambassador", "ambassador__user"
        )
        .filter(
            reminder_sent_at__isnull=True,
            updated_at__lte=now - timedelta(hours=grace_hours),
            updated_at__gte=now - timedelta(hours=max_age_hours),
        )
        .order_by("updated_at")
    )

    counts = {
        "sent": 0,
        "candidates": len(drafts),
        "skipped_filed": 0,
        "skipped_empty": 0,
        "skipped_open_shift": 0,
        "skipped_no_contact": 0,
        "failed": 0,
    }

    for draft in drafts:
        if not draft_has_answers(draft.payload):
            counts["skipped_empty"] += 1
            continue
        # A filed shift-scope recap means this draft's job is done. An
        # "additional" draft is a second filing: filing the first recap
        # does not clear it (submit clears the scope it filed), so its
        # presence still means unsubmitted answers.
        if draft.scope == "shift" and has_filed_recap(
            ambassador_id=draft.ambassador_id, event_id=draft.event_id
        ):
            counts["skipped_filed"] += 1
            continue

        state = clock_state(
            ambassador_id=draft.ambassador_id, event_id=draft.event_id
        )
        clocked_out = state.get("state") == "clocked_out"
        if not clocked_out and not _shift_passed(draft.event, now):
            counts["skipped_open_shift"] += 1
            continue

        url = standing_checkin_url(draft.event)
        user = getattr(draft.ambassador, "user", None)
        emails = valid_recipient_emails([getattr(user, "email", "") or ""])
        if not url or not emails:
            counts["skipped_no_contact"] += 1
            continue

        event = draft.event
        venue = html.escape((getattr(event, "name", None) or "your shift")[:120])
        first = html.escape(
            (getattr(user, "first_name", "") or "").strip() or "there"
        )
        subject = "Your recap draft is still waiting"
        body_html = f"""
        <p>Hi {first},</p>
        <p>Your recap for <strong>{venue}</strong> was saved as a draft but
        never submitted. Open your check-in link and it will be right where
        you left it:</p>
        <p><a href="{html.escape(url)}">Finish your recap</a></p>
        <p style="color:#888">This is the only reminder we'll send about
        this draft.</p>
        """

        if dry_run:
            counts["sent"] += 1
            continue
        try:
            send(to_email=emails[0], subject=subject, body_html=body_html)
        except Exception:  # noqa: BLE001 — one BA must not abort the run
            logger.warning(
                "walk-up draft reminder failed draft=%s", draft.id, exc_info=True
            )
            counts["failed"] += 1
            continue
        WalkupRecapDraft.objects.filter(id=draft.id).update(
            reminder_sent_at=timezone.now()
        )
        counts["sent"] += 1

    return counts
