"""Email Ignite that a scheduled demo should be cancelled.

The request stays as it is. Ops reads the email and takes the BA off the
schedule. Sales (and admins) can point at a request by its Relay id, or by
a pasted REQ code, numeric id, or uuid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from graphql import GraphQLError
from strawberry.relay import to_base64

from events import models
from events.envelopes import _admin_request_url, _apply_request_tz
from utils.graphql.mixins import resolve_id_to_int
from utils.mailer import Envelope, Mailer

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
    tenant_id: int | None,
    actor,
) -> DemoCancelResult:
    """Resolve the request, email every Ignite user, and note the ask.

    ``tenant_id`` None means the caller is a Spark admin and can reach any
    brand. A set id limits the lookup to that brand.
    """
    clean_reason = (reason or "").strip()
    if len(clean_reason) < 3:
        raise DemoCancelError("Add a reason so Ignite knows why this demo should be cancelled.")

    request = resolve_cancel_request(
        request_id=request_id,
        lookup=lookup,
        tenant_id=tenant_id,
    )
    code = request_display_code(request.id)
    store = _store_label(request)
    to_emails = _ignite_recipients()
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

    subject = f"Cancel demo {code} — {store}"[:180]
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

    try:
        models.RequestActivityLog.objects.create(
            tenant_id=request.tenant_id,
            request=request,
            kind=models.RequestActivityLog.KIND_NOTE_ADDED,
            actor_user=actor if getattr(actor, "id", None) else None,
            summary=f"Demo cancel requested: {clean_reason[:180]}",
            metadata={
                "reason": clean_reason[:2000],
                "request_code": code,
            },
        )
    except Exception:
        pass

    return DemoCancelResult(
        request_code=code,
        message=f"Ignite has been emailed about {code}.",
    )


def resolve_cancel_request(
    *,
    request_id: str | int | None,
    lookup: str | None,
    tenant_id: int | None,
) -> models.Request:
    qs = models.Request.objects.filter(deleted_at__isnull=True)
    if tenant_id is not None:
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
        found = qs.filter(uuid=parsed).select_related("retailer", "tenant", "timezone").first()
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
    return qs.select_related("retailer", "tenant", "timezone").filter(id=pk).first()


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
        for row in qs.select_related("retailer", "tenant", "timezone").filter(id__in=hit_ids)
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


def _ignite_recipients() -> list[str]:
    from events.mutations import _get_request_cc_emails

    return _get_request_cc_emails()
