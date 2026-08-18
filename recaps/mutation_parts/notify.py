"""Approve-notify + recap data-quality helpers."""
import logging
import re

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from events.torch_portal import is_torch_tenant, torch_recap_submit_lists
from recaps import models
from recaps.envelopes import (
    RecapApprovedNotificationMailer,
    RecapReadyForReviewAdminMailer,
)
from recaps.mutation_parts.pdf_helpers import (
    _ensure_recap_pdf_for_notify,
    _resolve_recap_pdf_attachment,
)
from tenants.models import Role, Tenant, TenantedUser
from utils.cloud_tasks import enqueue
from utils.onesignal import OneSignalError, one_signal_client

User = get_user_model()
logger = logging.getLogger("recaps.mutations")

# Kyle: do not send Girl Beer recap-approved client mail. Stamp
# client_notified_at so catch-up / future approves skip SMTP.
GIRL_BEER_TENANT_SLUGS = frozenset({"girl-beer", "girlbeer"})


def recap_tenant(recap: models.Recap | models.CustomRecap):
    tenant = getattr(recap, "tenant", None)
    if tenant is not None:
        return tenant
    event = getattr(recap, "event", None)
    return getattr(event, "tenant", None) if event is not None else None


def is_girl_beer_recap(recap: models.Recap | models.CustomRecap) -> bool:
    tenant = recap_tenant(recap)
    if tenant is None:
        return False
    slug = (getattr(tenant, "slug", None) or "").strip().lower()
    name = (getattr(tenant, "name", None) or "").strip().lower()
    url_name = (getattr(tenant, "request_url_name", None) or "").strip().lower()
    if slug in GIRL_BEER_TENANT_SLUGS:
        return True
    if name == "girl beer":
        return True
    if url_name in GIRL_BEER_TENANT_SLUGS or url_name.endswith("-girl-beer"):
        return True
    return False


def stamp_client_notified(
    recap: models.Recap | models.CustomRecap, *, reason: str = ""
) -> bool:
    """Stamp client_notified_at without bumping updated_at (QuerySet.update)."""
    if getattr(recap, "client_notified_at", None):
        return False
    now = timezone.now()
    updated = type(recap).objects.filter(
        pk=recap.pk, client_notified_at__isnull=True
    ).update(client_notified_at=now)
    if updated:
        recap.client_notified_at = now
        logger.info(
            "Stamped client_notified_at recap=%s model=%s reason=%s",
            recap.pk,
            type(recap).__name__,
            reason,
        )
    return bool(updated)


_RECAP_APPROVED_SELECT_RELATED = (
    "event",
    "event__tenant",
    "event__rmm_asigned",
    "event__timezone",
    "event__request",
    "event__request__created_by",
    "job",
    "retailer",
    "timezone",
    "ambassador",
    "ambassador__user",
)


def _load_recap_for_approved_notify(recap_id: int, recap_kind: str):
    model = models.CustomRecap if recap_kind == "custom" else models.Recap
    return model.objects.select_related(*_RECAP_APPROVED_SELECT_RELATED).get(
        id=recap_id
    )


def _thread_recap_approved_notify(
    recap_id: int, recap_kind: str, *, html_only: bool = False
) -> None:
    """Sync entry for thread fallback / one-shot resend.

    Must not run sync ORM inside ``asyncio.run`` — that raises
    ``SynchronousOnlyOperation`` and is why client "recap is ready" mail
    never left after approve (Spark alert since 2026-08-15).

    ``html_only=True`` skips PDF generate/attach so a recovery blast
    can ship the working link-only email (recap #727 sample) without
    waiting on a deploy or OOMing Cloud Run.
    """
    from asgiref.sync import async_to_sync
    from django.db import close_old_connections

    from utils.db import fresh_db_connection

    def _run():
        recap = _load_recap_for_approved_notify(recap_id, recap_kind)
        if is_girl_beer_recap(recap):
            stamp_client_notified(recap, reason="girl-beer")
            return
        if getattr(recap, "client_notified_at", None):
            return
        if not html_only:
            async_to_sync(_ensure_recap_pdf_for_notify)(recap)
        async_to_sync(_notify_recap_approved_to_rmm_or_clients)(
            recap, html_only=html_only
        )

    close_old_connections()
    try:
        fresh_db_connection(_run)()
    finally:
        close_old_connections()


async def _kick_recap_approved_notify(
    recap, recap_kind: str, *, html_only: bool = False
) -> None:
    """Enqueue PDF + client email, or send on this request if the queue is off."""
    if await sync_to_async(is_girl_beer_recap)(recap):
        await sync_to_async(stamp_client_notified)(recap, reason="girl-beer")
        logger.info(
            "Girl Beer recap %s: marked client_notified_at, skipped SMTP",
            getattr(recap, "id", None),
        )
        return
    payload = {
        "recap_id": recap.id,
        "recap_kind": recap_kind,
        "html_only": html_only,
    }
    path = "/api/tasks/recap-approved-notify"
    enqueued = await sync_to_async(enqueue)(path, payload)
    if enqueued:
        return
    # Production Cloud Tasks env is still unset. Approve used to spawn a
    # daemon thread that died in Django ORM before mailer.send(). Send
    # inline so the client actually gets "recap is ready".
    if not html_only:
        await _ensure_recap_pdf_for_notify(recap)
    await _notify_recap_approved_to_rmm_or_clients(recap, html_only=html_only)


def is_torch_portal_recap(recap: models.Recap | models.CustomRecap) -> bool:
    """True for Torch portal activations — not standing BA / other brands.

    Prefer an Event linked to a request (the public form create). If we
    cannot tie to a request, still notify when the Torch recap has a
    requestor email (3rd-party TH-AGENCY filings that carry one).
    """
    if is_girl_beer_recap(recap):
        return False
    tenant = recap_tenant(recap)
    if not is_torch_tenant(tenant):
        return False
    try:
        req = getattr(recap.event, "request", None)
    except Exception:
        req = None
    if req is not None:
        return True
    return bool(_collect_requestor_recipients(recap))


async def _kick_torch_portal_recap_submit_notify(recap, recap_kind: str) -> None:
    """On recap submit: email Torch portal requestor + Liberty + events + Nevena.

    Link-only (html_only) so Cloud Tasks JSON stays serializable — no raw
    PDF bytes. Stamps client_notified_at so a later admin approve does not
    re-blast. Girl Beer and non-Torch recaps are no-ops.
    """
    try:
        if await sync_to_async(is_girl_beer_recap)(recap):
            return
        if not await sync_to_async(is_torch_portal_recap)(recap):
            return
        await _kick_recap_approved_notify(recap, recap_kind, html_only=True)
    except Exception:
        logger.exception(
            "torch portal recap submit notify failed recap=%s",
            getattr(recap, "id", None),
        )


def _collect_requestor_recipients(
    recap: models.Recap | models.CustomRecap,
) -> list[tuple[str, str]]:
    """Pull the original request's requestor (created_by + the
    requestor_email override). Returns a list of (email, first_name)
    tuples, deduped, ready to merge into the approval recipient set.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        req = getattr(recap.event, "request", None)
    except Exception:
        req = None
    if not req:
        return out

    def add(email: str | None, first: str | None):
        e = (email or "").strip()
        if not e or e.lower() in seen:
            return
        seen.add(e.lower())
        out.append((e, (first or "").strip()))

    # `requestor_email` is the public-form override — wins if set.
    add(getattr(req, "requestor_email", None), None)
    # Authenticated creator (admin/client portal submission).
    cb = getattr(req, "created_by", None)
    if cb:
        add(getattr(cb, "email", None), getattr(cb, "first_name", None))
    return out


async def _resolve_recap_requestor_recipients(
    recap: models.Recap | models.CustomRecap,
) -> list[tuple[str, str]]:
    return await sync_to_async(_collect_requestor_recipients)(recap)


def _collect_recap_approved_recipients(
    recap: models.Recap | models.CustomRecap,
) -> tuple[list[tuple[str, str]], str]:
    """Same recipient set as approve-notify: RMM + client-role users +
    Tenant.recap_recipient_emails + requestor. Returns (recipients, reply_to).

    Torch portal recaps use the four-party list (requestor + Liberty +
    events + Nevena) instead of the RMM / client-role blast.
    """
    if is_torch_portal_recap(recap):
        requestors = [email for email, _first in _collect_requestor_recipients(recap)]
        to_emails, cc_emails = torch_recap_submit_lists(requestors)
        recipients = [(email, "") for email in [*to_emails, *cc_emails]]
        return recipients, "events@igniteproductions.co"

    event = recap.event
    rmm_user = getattr(event, "rmm_asigned", None)
    fallback_reply_to = "events@igniteproductions.co"
    reply_to_email = (
        getattr(rmm_user, "email", None) or ""
    ).strip() or fallback_reply_to

    recipients: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _push(email: str | None, first: str | None):
        e = (email or "").strip()
        if not e or e.lower() in seen:
            return
        seen.add(e.lower())
        recipients.append((e, (first or "").strip()))

    if rmm_user and rmm_user.email:
        _push(rmm_user.email, rmm_user.first_name)

    # Always include the tenant's client contacts (the brand) — not only as
    # an RMM fallback. The client should receive the approved recap whether
    # or not an RMM is assigned, and regardless of how the BA/recap was
    # created (e.g. an admin manually filing for an externally-staffed BA).
    client_rows = list(
        TenantedUser.objects.filter(
            tenant_id=event.tenant_id,
            is_active=True,
            user__role__slug=Role.CLIENT_SLUG,
        ).values("user__email", "user__first_name")
    )
    for row in client_rows:
        _push(row.get("user__email"), row.get("user__first_name"))

    # Add the tenant's explicitly-configured recap recipients. Brands
    # without a client-role user still need the approved recap to reach
    # a human, so staff can list addresses on Tenant.recap_recipient_emails
    # (comma/newline/semicolon-separated). Parsed best-effort and deduped
    # through the same _push() as the RMM/client/requestor rows.
    configured = (
        Tenant.objects.filter(id=event.tenant_id)
        .values_list("recap_recipient_emails", flat=True)
        .first()
    )
    for token in re.split(r"[,\n;]+", configured or ""):
        candidate = token.strip()
        if "@" in candidate and "." in candidate:
            _push(candidate, None)

    # Add the original requestor — same activation owner the admin
    # CC's on the request approval email. Closes the loop: requestor
    # → request approved → recap filed → recap approved.
    for email, first in _collect_requestor_recipients(recap):
        _push(email, first)

    return recipients, reply_to_email


async def _notify_recap_approved_to_rmm_or_clients(
    recap: models.Recap | models.CustomRecap,
    *,
    html_only: bool = False,
) -> None:
    if await sync_to_async(is_girl_beer_recap)(recap):
        await sync_to_async(stamp_client_notified)(recap, reason="girl-beer")
        return
    if getattr(recap, "client_notified_at", None):
        return

    recipients, reply_to_email = await sync_to_async(
        _collect_recap_approved_recipients
    )(recap)
    if not recipients:
        await sync_to_async(stamp_client_notified)(recap, reason="no-recipients")
        return

    # Resolve PDF once and reuse — saves one GCS fetch per recipient.
    # Recovery blasts can skip the attach so a bytes-serialization bug
    # (or a Redis-down PDF render) cannot 23x the Spark alert again.
    attachments = (
        None if html_only else await _resolve_recap_pdf_attachment(recap)
    )

    for email, first_name in recipients:
        mailer = RecapApprovedNotificationMailer(
            recap=recap,
            to_emails=[email],
            recipient_first_name=first_name or None,
            reply_to_email=reply_to_email,
            attachments=attachments,
        )
        try:
            await sync_to_async(mailer.send)()
        except Exception:
            logger.exception(
                "Failed to send recap-approved email to %s for recap=%s",
                email,
                getattr(recap, "id", None),
            )

    # Stamp even if some recipients failed so a catch-up cannot re-spam
    # the ones that already got mail.
    await sync_to_async(stamp_client_notified)(recap, reason="sent")


async def _notify_recap_approved_to_ambassador_by_push(
    recap: models.Recap,
) -> None:
    ambassador = getattr(recap, "ambassador", None)
    user = getattr(ambassador, "user", None)
    if not user:
        return

    deep_link = f"spark://recaps/{recap.id}"

    try:
        await one_signal_client.send_push(
            external_ids=[str(user.uuid)],
            title="Recap approved",
            message=f"Your recap for {recap.name} was approved.",
            url=deep_link,
            data={
                "type": "recap_approved",
                "recap_id": str(recap.id),
                "deep_link": deep_link,
            },
        )
    except OneSignalError as exc:
        logger.warning(
            "Failed to send OneSignal recap approval push for recap=%s: %s",
            recap.id,
            exc,
        )


async def _notify_recap_ready_for_review_to_admins(
    recap: models.Recap | models.CustomRecap,
    created_by: User | None,
) -> None:
    if not created_by:
        return

    role_slug = await sync_to_async(
        lambda: User.objects.filter(id=created_by.id)
        .values_list("role__slug", flat=True)
        .first()
    )()
    role_slug = (role_slug or "").strip()

    if role_slug == Role.AMBASSADOR_SLUG:
        # BA filed it from the app → notify the admin review list (the
        # original behavior). Recipients come from RECAP_REVIEW_COPY_EMAILS.
        recipients = [
            email.strip()
            for email in getattr(settings, "RECAP_REVIEW_COPY_EMAILS", [])
            if (email or "").strip()
        ]
    else:
        # An admin filed the recap on a BA's behalf. The review list doesn't
        # need a "ready for review" alert (an admin already handled it), but
        # the admin who filed it still expects a confirmation. Scope the
        # email to that filer ONLY — never a team broadcast — so this can't
        # flood the review list on imports / multi-recap entry.
        filer_email = (getattr(created_by, "email", "") or "").strip()
        recipients = [filer_email] if filer_email else []

    if not recipients:
        return

    # Name shown in the email is the BA the recap is FOR (linked ambassador
    # or write-in external BA), falling back to the creator's own name.
    def _ba_label() -> str:
        try:
            amb = getattr(recap, "ambassador", None)
            user = getattr(amb, "user", None) if amb else None
            if user:
                full = (user.get_full_name() or "").strip()
                if full:
                    return full
        except Exception:
            pass
        return (getattr(recap, "external_ba_name", "") or "").strip()

    ba_name = await sync_to_async(_ba_label)()
    if not ba_name:
        ba_name = (
            created_by.get_full_name().strip()
            if hasattr(created_by, "get_full_name")
            else ""
        ) or created_by.email

    mailer = RecapReadyForReviewAdminMailer(
        recap=recap,
        to_emails=recipients,
        ambassador_name=ba_name,
    )
    # Best-effort: a mail failure must never break recap creation/approval.
    try:
        await sync_to_async(mailer.send)()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "recap ready-for-review email failed for recap %s: %s",
            getattr(recap, "id", None),
            exc,
        )


def _compute_recap_data_quality_flags(custom_recap: models.CustomRecap) -> list[str]:
    """Run the sacred KPI matcher over a just-saved custom recap and return
    the implausibility reasons (empty = looks fine). Stamps
    ``data_quality_flags`` on the row so the flag is durable + visible.
    Sync — call via sync_to_async. Best-effort: never raises."""
    from recaps.report_service import (
        CampaignReportKpis,
        _accumulate_custom,
        implausibility_reasons,
    )

    try:
        kpis = CampaignReportKpis()
        _accumulate_custom(custom_recap, kpis)
        reasons = implausibility_reasons(kpis)
    except Exception as exc:  # noqa: BLE001 — a matcher hiccup must not block filing
        logger.warning(
            "recap data-quality guard failed for recap %s: %s",
            getattr(custom_recap, "id", None),
            exc,
        )
        return []

    flags_text = "; ".join(reasons)
    # Only write when it changed (avoid a needless UPDATE on the happy path).
    if (custom_recap.data_quality_flags or "") != flags_text:
        custom_recap.data_quality_flags = flags_text
        try:
            custom_recap.save(update_fields=["data_quality_flags"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "recap data-quality flag save failed for recap %s: %s",
                getattr(custom_recap, "id", None),
                exc,
            )
    return reasons


def _send_recap_data_quality_alert(
    custom_recap: models.CustomRecap, reasons: list[str]
) -> None:
    """Email the Ignite team that a recap was just filed with suspect
    numbers. Sync + best-effort (never raises) — call via sync_to_async."""
    import html as _html

    try:
        from tenants.support import _resolve_ignite_recipients
        from utils.mailer import Envelope, Mailer

        recipients = _resolve_ignite_recipients()
        if not recipients:
            return
        ev = getattr(custom_recap, "event", None)
        tenant_name = getattr(getattr(ev, "tenant", None), "name", "") or ""
        event_name = getattr(ev, "name", "") or "(no event)"
        amb = getattr(custom_recap, "ambassador", None)
        user = getattr(amb, "user", None) if amb else None
        who = (
            (user.get_full_name() or "").strip() if user else ""
        ) or (getattr(custom_recap, "external_ba_name", "") or "").strip() or "?"
        reason_items = "".join(f"<li>{_html.escape(r)}</li>" for r in reasons)
        body = f"""
        <h2 style="margin:0 0 8px">A recap was just filed with suspect numbers</h2>
        <p style="margin:0 0 12px;color:#555">
          Recap <b>#{custom_recap.id}</b> ({_html.escape(event_name)} ·
          {_html.escape(tenant_name)} · {_html.escape(who)}) parsed into values
          that can't be right for a single event. Review and correct the field
          value before these numbers reach a client report.
        </p>
        <ul style="margin:0 0 12px">{reason_items}</ul>
        <p style="color:#888;margin-top:12px">Recap submit-time guard · fix the
          recap's field value to clear the flag.</p>
        """
        subject = f"[Spark] Recap #{custom_recap.id} filed with suspect numbers"

        class _GuardMailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(subject=subject, html=body, to_emails=recipients)

        _GuardMailer().send_now()
    except Exception as exc:  # noqa: BLE001 — alert failure must not break filing
        logger.warning(
            "recap data-quality alert email failed for recap %s: %s",
            getattr(custom_recap, "id", None),
            exc,
        )


async def _guard_recap_data_quality(custom_recap: models.CustomRecap) -> None:
    """Submit-time data guard: the moment a custom recap is filed, flag it if
    its parsed KPIs are implausible (conversion >100%, absurd counts) and
    alert the Ignite team immediately — so a fat-fingered number is caught at
    the source instead of a week later in the audit. Best-effort throughout;
    a guard failure never breaks recap creation."""
    reasons = await sync_to_async(_compute_recap_data_quality_flags)(custom_recap)
    if not reasons:
        return
    await sync_to_async(_send_recap_data_quality_alert)(custom_recap, reasons)
