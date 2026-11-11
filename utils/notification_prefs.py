"""Per-person email preferences (opt-out).

Each optional email belongs to a category, keyed by its template. A person
can mute a category for themselves; everyone else on the same email still
gets it. Emails whose template is not listed here — sign-in, invites,
password resets, shift and job emails, anything built from raw HTML — are
never filtered.

Fails OPEN everywhere: if the lookup breaks, the email goes out as before.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    description: str
    templates: tuple[str, ...]


CATEGORIES: tuple[Category, ...] = (
    Category(
        key="request_updates",
        label="Request updates",
        description="A request was submitted, approved or assigned.",
        templates=(
            "events.templates.emails.request_created_notification",
            "events.templates.emails.request_created_admin_notification",
            "events.templates.emails.request_auto_approved_requestor_notification",
            "events.templates.emails.request_approved_notification",
            "events.templates.emails.event_approved_notification",
            "events.templates.emails.rmm_assigned_request",
        ),
    ),
    Category(
        key="recap_review",
        label="Recaps ready for review",
        description="An ambassador filed a recap that needs a look.",
        templates=(
            "recaps.templates.emails.recap_ready_for_review_admin_notification",
        ),
    ),
    Category(
        key="digests",
        label="Digests and roll-ups",
        description="Weekly digest, daily retail roll-up and summary emails.",
        templates=(
            "events.templates.emails.client_weekly_digest",
            "recaps.templates.emails.daily_retail_rollup",
            "digest.templates.emails.admin_digest",
            "digest.templates.emails.executive_summary",
        ),
    ),
    Category(
        key="mentions",
        label="Mentions in notes",
        description="Someone mentioned you in a note on a request.",
        templates=("events.templates.emails.note_mention",),
    ),
    Category(
        key="staffing",
        label="Ambassador applications",
        description="A new ambassador signed up or applied to an event.",
        templates=(
            "ambassadors.templates.emails.new_ambassador_alert",
            "ambassadors.templates.emails.notify_application_to_client",
            "ambassadors.templates.emails.event_application",
            "jobboard.templates.emails.job_board_booking_internal",
        ),
    ),
)

CATEGORY_BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}
_CATEGORY_BY_TEMPLATE: dict[str, str] = {
    t: c.key for c in CATEGORIES for t in c.templates
}


def category_for_template(template: str | None) -> str | None:
    return _CATEGORY_BY_TEMPLATE.get((template or "").strip())


def _muted_lower(category: str, emails: list[str]) -> set[str]:
    """Lower-cased addresses, out of ``emails``, whose owner muted
    ``category``. Sync ORM."""
    from django.db.models import Q

    from tenants.models import NotificationPreference

    wanted = {e.strip().lower() for e in emails if e and e.strip()}
    if not wanted:
        return set()
    match = Q()
    for e in wanted:
        match |= Q(user__email__iexact=e)
    rows = NotificationPreference.objects.filter(
        match, category=category, email_enabled=False
    ).values_list("user__email", flat=True)
    return {(e or "").strip().lower() for e in rows} & wanted


def _muted_lower_any_context(category: str, emails: list[str]) -> set[str]:
    """Run the lookup whether or not an event loop is running here. Async
    resolvers send mail too, and the sync ORM refuses to run on a loop
    thread, so hop to a short-lived worker thread in that case."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _muted_lower(category, emails)

    def _in_thread() -> set[str]:
        from django.db import close_old_connections

        close_old_connections()
        try:
            return _muted_lower(category, emails)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_in_thread).result(timeout=10)


def drop_muted_recipients(envelope) -> None:
    """Remove recipients who muted this email's category, in place.

    No-op for emails outside every category. Never raises.
    """
    category = category_for_template(getattr(envelope, "template", ""))
    if not category:
        return
    to_emails = list(getattr(envelope, "to_emails", None) or [])
    cc_emails = list(getattr(envelope, "cc_emails", None) or [])
    if not to_emails and not cc_emails:
        return
    try:
        muted = _muted_lower_any_context(category, to_emails + cc_emails)
    except Exception as exc:  # noqa: BLE001 - never block mail on a lookup
        logger.warning("notification preference lookup failed (%s): %s", category, exc)
        return
    if not muted:
        return
    keep = lambda emails: [  # noqa: E731
        e for e in emails if (e or "").strip().lower() not in muted
    ]
    envelope.to_emails = keep(to_emails)
    envelope.cc_emails = keep(cc_emails)
    # A muted "to" must not leave a cc-only message that providers reject:
    # promote the remaining cc so the people who still want it get it.
    if not envelope.to_emails and envelope.cc_emails:
        envelope.to_emails, envelope.cc_emails = envelope.cc_emails, []
    logger.info(
        "notification preference: dropped %d recipient(s) for %s",
        len(muted),
        category,
    )
