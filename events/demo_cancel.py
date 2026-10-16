"""Cancel a scheduled demo: update request/event status and email Ignite + client.

When a client or admin cancels a demo with a reason, the request transitions to
``declined`` (or ``cancelled``) with ``decline_reason`` set, any linked events
transition to ``suspended`` (or ``declined``) with open jobs closed and assigned
ambassadors notified, and both Ignite and the requestor/brand receive the
cancellation email with the comment.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from asgiref.sync import async_to_sync
from django.db import transaction
from graphql import GraphQLError
from strawberry.relay import to_base64

from events import models
from events.envelopes import _admin_request_url, _apply_request_tz
from events.request_auto_approve import tenant_request_cc_emails
from jobs.models import Job
from utils.graphql.mixins import resolve_id_to_int
from utils.mailer import Envelope, Mailer, is_placeholder_recipient_email

logger = logging.getLogger(__name__)

_SCAN_LIMIT = 8000


class DemoCancelError(Exception):
    """A cancel the caller can fix: missing reason, unknown request, ambiguous code."""


@dataclass
class DemoCancelResult:
    request_code: str
    message: str


class DemoCancelRequestedMailer(Mailer):
    def __init__(self, *, to_emails: list[str], subject: str, context: dict):
        self._to_emails = to_emails
        self._subject = subject
        self._context = context

    def envelope(self) -> Envelope:
        return Envelope(
            subject=self._subject,
            template="events.templates.emails.demo_cancel_requested",
            to_emails=self._to_emails,
            context=self._context,
        )


def request_display_code(pk: int) -> str:
    """Same REQ- label the tracker shows: last 4 of the Relay id."""
    return f"REQ-{to_base64('Request', int(pk))[-4:].upper()}"


def request_demo_cancellation(
    *,
    request_id: str | int | None,
    lookup: str | None,
    reason: str | None,
    tenant_id: int | None | list[int] | set[int],
    actor,
) -> DemoCancelResult:
    """Resolve the request, cancel it + linked events, and email Ignite + client.

    ``tenant_id`` None means the caller is a Spark admin and can reach any
    brand. A set id (or collection of ids) limits the lookup to those brands.
    """
    clean_reason = (reason or "").strip()
    if len(clean_reason) < 3:
        raise DemoCancelError("Add a reason so Ignite knows why this demo should be cancelled.")

    request = resolve_cancel_request(
        request_id=request_id,
        lookup=lookup,
        tenant_id=tenant_id,
    )
    if tenant_id is not None and getattr(actor, "id", None):
        is_admin = False
        try:
            role = getattr(actor, "role", None)
            is_admin = bool(
                getattr(actor, "is_superuser", False)
                or getattr(actor, "is_staff", False)
                or getattr(role, "_is_spark_admin", False)
            )
        except Exception:
            is_admin = False
        if not is_admin and isinstance(tenant_id, (list, set, tuple)):
            try:
                actor.get_tenant(tenant_id=request.tenant_id)
            except Exception as exc:
                raise DemoCancelError("That request isn't on your account.") from exc

    code = request_display_code(request.id)
    store = _store_label(request)
    to_emails = _cancel_recipients(request)
    if not to_emails:
        raise DemoCancelError("No Ignite recipients are on file, so the email was not sent.")

    asked_by = (getattr(actor, "get_full_name", lambda: "")() or "").strip()
    asked_email = (getattr(actor, "email", None) or "").strip()
    if not asked_by:
        asked_by = asked_email or "Someone"

    brand = ""
    try:
        brand = (getattr(request.tenant, "name", None) or "").strip()
    except Exception:
        brand = ""

    suspended_event_ids: list[int] = []
    with transaction.atomic():
        prev_status_slug = ""
        try:
            if request.status_id:
                prev_obj = models.RequestStatus.objects.filter(id=request.status_id).first()
                prev_status_slug = (getattr(prev_obj, "slug", "") or "").strip()
        except Exception:
            prev_status_slug = ""

        req_status = (
            models.RequestStatus.objects.filter(
                tenant_id=request.tenant_id, slug__in=["declined", "cancelled", "canceled"]
            )
            .order_by("id")
            .first()
        )
        request.decline_reason = clean_reason
        update_fields = ["decline_reason", "updated_at"]
        if req_status is not None and request.status_id != req_status.id:
            request.status = req_status
            update_fields.append("status")
        if getattr(actor, "id", None):
            request.updated_by = actor
            update_fields.append("updated_by")
        request.save(update_fields=update_fields)

        ev_status = (
            models.EventStatus.objects.filter(
                tenant_id=request.tenant_id, slug__in=["suspended", "declined", "cancelled", "canceled"]
            )
            .order_by("id")
            .first()
        )
        events = list(models.Event.objects.filter(request_id=request.id))
        for ev in events:
            ev_fields = ["updated_at"]
            if ev_status is not None and ev.status_id != ev_status.id:
                ev.status = ev_status
                ev_fields.append("status")
            if getattr(actor, "id", None):
                ev.updated_by = actor
                ev_fields.append("updated_by")
            if len(ev_fields) > 1:
                ev.save(update_fields=ev_fields)
            suspended_event_ids.append(ev.id)

        if suspended_event_ids:
            try:
                Job.objects.filter(event_id__in=suspended_event_ids, closed=False).update(
                    closed=True, ongoing=False
                )
            except Exception:
                logger.exception("Closing jobs failed for cancelled request %s", request.id)

        try:
            models.RequestActivityLog.objects.create(
                tenant_id=request.tenant_id,
                request=request,
                kind=models.RequestActivityLog.KIND_NOTE_ADDED,
                actor_user=actor if getattr(actor, "id", None) else None,
                summary=f"Demo cancelled ({prev_status_slug or '—'} → {getattr(req_status, 'slug', 'declined')}): {clean_reason[:160]}",
                metadata={
                    "reason": clean_reason[:2000],
                    "request_code": code,
                    "from": prev_status_slug,
                    "to": getattr(req_status, "slug", "declined"),
                },
            )
        except Exception:
            pass

    for ev_id in suspended_event_ids:
        try:
            from events.mutations import _notify_assigned_ambassadors_for_suspended_event

            async_to_sync(_notify_assigned_ambassadors_for_suspended_event)(ev_id)
        except Exception:
            logger.exception("Failed notifying ambassadors for cancelled event %s", ev_id)

    subject = f"Demo cancelled: {code} — {store}"[:180]
    DemoCancelRequestedMailer(
        to_emails=to_emails,
        subject=subject,
        context={
            "request_code": code,
            "store": store,
            "brand": brand or "—",
            "when_label": _when_label(request),
            "address": (request.address or "").strip() or "—",
            "reason": clean_reason,
            "asked_by": asked_by,
            "asked_email": asked_email or "—",
            "admin_url": _admin_request_url(request),
        },
    ).send()

    return DemoCancelResult(
        request_code=code,
        message=f"{code} has been cancelled and Ignite + the brand have been emailed.",
    )


def resolve_cancel_request(
    *,
    request_id: str | int | None,
    lookup: str | None,
    tenant_id: int | None | list[int] | set[int],
) -> models.Request:
    qs = models.Request.objects.filter(deleted_at__isnull=True)
    if tenant_id is not None:
        if isinstance(tenant_id, (list, set, tuple)):
            qs = qs.filter(tenant_id__in=list(tenant_id))
        else:
            qs = qs.filter(tenant_id=tenant_id)

    raw_id = request_id
    if raw_id not in (None, ""):
        try:
            pk = resolve_id_to_int(raw_id)
        except (TypeError, ValueError, GraphQLError) as exc:
            raise DemoCancelError("That request id isn't valid.") from exc
        found = _load(qs, pk)
        if found:
            return found
        deleted = models.Request.objects.filter(id=pk, deleted_at__isnull=False).exists()
        if deleted:
            raise DemoCancelError("That request was deleted.")
        raise DemoCancelError("That request isn't on your account.")

    raw = (lookup or "").strip()
    if not raw:
        raise DemoCancelError("Search for the demo or paste its request number.")

    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        parsed = None
    if parsed is not None:
        found = qs.filter(uuid=parsed).select_related(
            "retailer", "tenant", "timezone", "created_by", "rmm_asigned"
        ).first()
        if not found:
            raise DemoCancelError("No request matches that id.")
        return found

    if not raw.upper().startswith("REQ-") and len(raw) > 12 and not raw.isdigit():
        try:
            pk = resolve_id_to_int(raw)
        except (TypeError, ValueError, GraphQLError):
            pk = None
        if pk is not None:
            found = _load(qs, pk)
            if found:
                return found

    code = raw.upper()
    if code.startswith("REQ-"):
        code = code[4:].strip()
    if not code:
        raise DemoCancelError("Paste the request number, like REQ-925.")

    if code.isdigit() and len(code) >= 5:
        exact = _load(qs, int(code))
        if exact:
            return exact

    matches = _suffix_matches(qs, code)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise DemoCancelError(
            "More than one request matches that number. Search by the store name and pick one."
        )
    raise DemoCancelError(
        "No request matches that. Search by the store name, or paste the request number from the tracker."
    )


def _load(qs, pk: int) -> models.Request | None:
    return qs.select_related(
        "retailer", "tenant", "timezone", "created_by", "rmm_asigned"
    ).filter(id=pk).first()


def _suffix_matches(qs, code: str) -> list[models.Request]:
    code_u = code.upper()
    pks = list(qs.order_by("-id").values_list("id", flat=True)[:_SCAN_LIMIT])
    hit_ids: list[int] = []
    for pk in pks:
        suffix = request_display_code(pk)[4:]
        if str(pk).upper().endswith(code_u) or suffix == code_u:
            hit_ids.append(pk)
        if len(hit_ids) > 5:
            break
    if not hit_ids:
        return []
    rows = {
        row.id: row
        for row in qs.select_related(
            "retailer", "tenant", "timezone", "created_by", "rmm_asigned"
        ).filter(id__in=hit_ids)
    }
    return [rows[pk] for pk in hit_ids if pk in rows]


def _store_label(request: models.Request) -> str:
    store = (request.retailer_name or "").strip()
    if not store:
        try:
            store = (getattr(request.retailer, "name", None) or "").strip()
        except Exception:
            store = ""
    if not store:
        store = (request.name or "").strip()
    return store or "Demo"


def _when_label(request: models.Request) -> str:
    local = _apply_request_tz(request.date, request)
    if not local:
        return "Date not set"
    return local.strftime("%b %-d, %Y · %-I:%M %p")


def _ignite_recipients(tenant=None) -> list[str]:
    from events.mutations import _get_request_cc_emails

    return _get_request_cc_emails(tenant=tenant)


def _cancel_recipients(request: models.Request) -> list[str]:
    """Combine Ignite recipients + requestor/client + RMM + tenant CC emails."""
    tenant = getattr(request, "tenant", None)
    ignite = _ignite_recipients(tenant=tenant)
    if not ignite:
        return []

    extra: list[str] = []
    for raw in (
        getattr(request, "requestor_email", None),
        getattr(request, "client_email", None),
        getattr(getattr(request, "created_by", None), "email", None),
        getattr(getattr(request, "rmm_asigned", None), "email", None),
    ):
        e = (raw or "").strip()
        if e and not is_placeholder_recipient_email(e):
            extra.append(e)

    if tenant is not None:
        try:
            for e in tenant_request_cc_emails(tenant):
                clean = (e or "").strip()
                if clean and not is_placeholder_recipient_email(clean):
                    extra.append(clean)
        except Exception:
            pass

    out: list[str] = []
    seen: set[str] = set()
    for e in [*ignite, *extra]:
        norm = (e or "").strip()
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out
