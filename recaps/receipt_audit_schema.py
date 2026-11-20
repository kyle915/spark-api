"""GraphQL for receipt readings, spend and fraud flags. STAFF ONLY.

Every field here answers only for Ignite staff (``_is_admin_access``, read
from the database — see ``resolve_request_user_access``). A brand user, an
ambassador or anyone else gets null / an empty list, for their own recaps
too. These types are deliberately kept out of the brand-facing recap types,
brand emails, the AI connector (``spark_mcp``) and brand chat.
"""

from __future__ import annotations

import logging
from datetime import date

import strawberry
from asgiref.sync import sync_to_async
from django.utils import timezone

from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

logger = logging.getLogger(__name__)

MAX_FLAGS = 500


async def _is_staff(info: strawberry.Info) -> bool:
    try:
        user = info.context.request.user
        role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
        return _is_admin_access(role_slug, is_staff, is_super, email)
    except Exception:  # noqa: BLE001
        return False


def _money(value) -> str | None:
    return None if value is None else f"{value:.2f}"


def _iso(dt) -> str | None:
    try:
        return dt.isoformat() if dt else None
    except Exception:  # noqa: BLE001
        return None


def _parse_date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()) if raw else None
    except ValueError:
        return None


def _optional_id(raw) -> int | None:
    if raw in (None, ""):
        return None
    return resolve_id_to_int(str(raw).strip())


# ─── Types ──────────────────────────────────────────────────────────


@strawberry.type
class ReceiptLineItem:
    name: str
    qty: float | None
    unit_price: str | None
    amount: str
    is_brand: bool


@strawberry.type
class RecapReceiptReading:
    id: strawberry.ID
    file_id: strawberry.ID
    file_url: str | None
    status: str
    merchant: str
    store_number: str
    address: str
    city: str
    state_code: str
    ticket_number: str
    register: str
    receipt_date: str
    purchase_time: str
    subtotal: str | None
    fees: str | None
    tax: str | None
    total: str | None
    card_last4: str
    confidence: float | None
    arithmetic_ok: bool | None
    arithmetic_note: str
    line_items: list[ReceiptLineItem]
    model: str
    read_at: str | None
    error: str
    counted_here: bool
    also_on_recap_uuids: list[str]


@strawberry.type
class RecapStaffFlag:
    id: strawberry.ID
    recap_id: strawberry.ID
    recap_uuid: str
    tenant_id: strawberry.ID
    tenant_name: str
    event_name: str
    event_date: str
    kind: str
    kind_label: str
    severity: str
    explanation: str
    other_recap_uuid: str | None
    created_at: str | None
    resolved_at: str | None
    resolved_by_name: str | None
    resolution_note: str
    auto_cleared: bool


@strawberry.type
class ReceiptSpendTotals:
    product: str
    other: str
    unitemized: str
    fees: str
    tax: str
    total: str
    receipts: int


@strawberry.type
class RecapReceiptAudit:
    readings: list[RecapReceiptReading]
    flags: list[RecapStaffFlag]
    totals: ReceiptSpendTotals
    waiting_count: int
    flags_enabled: bool


@strawberry.type
class ReceiptSpendRecapRow:
    recap_id: strawberry.ID
    recap_uuid: str
    event_name: str
    event_date: str
    open_flags: list[str]
    totals: ReceiptSpendTotals


@strawberry.type
class ReceiptSpendSummary:
    totals: ReceiptSpendTotals
    recaps: list[ReceiptSpendRecapRow]
    receipt_rows: int
    csv: str | None


@strawberry.input
class ResolveRecapFlagInput:
    flag_id: strawberry.ID
    note: str = ""
    reopen: bool = False


@strawberry.type
class ResolveRecapFlagPayload:
    success: bool
    message: str
    flag: RecapStaffFlag | None


# ─── Builders (sync, DB) ────────────────────────────────────────────


def _totals_type(t: dict) -> ReceiptSpendTotals:
    return ReceiptSpendTotals(
        product=_money(t["product"]), other=_money(t["other"]),
        unitemized=_money(t["unitemized"]), fees=_money(t["fees"]),
        tax=_money(t["tax"]), total=_money(t["total"]), receipts=int(t["receipts"]),
    )


def flag_type(row) -> RecapStaffFlag:
    from recaps.models import RecapFlag
    from recaps.receipt_flags import KIND_LABELS

    recap = row.custom_recap
    event = getattr(recap, "event", None)
    day = getattr(event, "date", None)
    other = (row.details or {}).get("otherRecap") or {}
    resolver = row.resolved_by
    resolver_name = None
    if resolver is not None:
        resolver_name = (
            f"{getattr(resolver, 'first_name', '') or ''} {getattr(resolver, 'last_name', '') or ''}".strip()
            or getattr(resolver, "email", "")
            or None
        )
    return RecapStaffFlag(
        id=strawberry.ID(str(row.uuid)),
        recap_id=strawberry.ID(str(recap.id)),
        recap_uuid=str(recap.uuid),
        tenant_id=strawberry.ID(str(row.tenant_id)),
        tenant_name=getattr(row.tenant, "name", "") or "",
        event_name=(getattr(event, "name", "") or "")[:120],
        event_date=day.date().isoformat() if day else "",
        kind=row.kind,
        kind_label=KIND_LABELS.get(row.kind, row.kind),
        severity=row.severity,
        explanation=row.explanation,
        other_recap_uuid=other.get("recapUuid") or None,
        created_at=_iso(row.created_at),
        resolved_at=_iso(row.resolved_at),
        resolved_by_name=resolver_name,
        resolution_note=row.resolution_note or "",
        auto_cleared=bool(
            row.resolved_at and row.resolved_by_id is None
            and row.resolution_note == RecapFlag.AUTO_CLEARED_NOTE
        ),
    )


def _flag_qs():
    from recaps.models import RecapFlag

    return RecapFlag.objects.select_related(
        "custom_recap", "custom_recap__event", "tenant", "resolved_by"
    )


def build_recap_audit(recap_id: int) -> RecapReceiptAudit | None:
    from recaps.heic_conversion import display_blob_name
    from recaps.models import CustomRecap, CustomRecapFile, ReceiptReading
    from recaps.receipt_flags import flags_enabled
    from recaps.receipt_reader import _receipt_file_q
    from recaps.receipt_spend import recap_spend
    from utils.gcs import extract_blob_name_from_url, public_url

    if not CustomRecap.objects.filter(id=recap_id).exists():
        return None
    spend = recap_spend(recap_id)
    by_reading: dict[int, dict] = {}
    for rec in spend["receipts"]:
        for rid in rec["readingIds"]:
            by_reading[rid] = rec
    uuids = dict(CustomRecap.objects.filter(
        id__in={i for rec in spend["receipts"] for i in [rec["ownerRecapId"], *rec["alsoOnRecapIds"]]}
    ).values_list("id", "uuid"))
    readings = []
    rows = (
        ReceiptReading.objects.filter(custom_recap_file__custom_recap_id=recap_id)
        .select_related("custom_recap_file")
        .order_by("custom_recap_file__created_at", "id")
    )
    for r in rows:
        try:
            blob = extract_blob_name_from_url(str(getattr(r.custom_recap_file.url, "name", "") or ""))
            url = public_url(display_blob_name(blob)) if blob else None
        except Exception:  # noqa: BLE001
            url = None
        rec = by_reading.get(r.id)
        others = []
        if rec:
            others = [str(uuids[i]) for i in [rec["ownerRecapId"], *rec["alsoOnRecapIds"]]
                      if i != recap_id and i in uuids]
        readings.append(
            RecapReceiptReading(
                # Prefixed: Relay keys records by ``id`` across all types.
                id=strawberry.ID(f"ReceiptReading:{r.id}"),
                file_id=strawberry.ID(str(r.custom_recap_file_id)),
                file_url=url,
                status=r.status,
                merchant=r.merchant or "",
                store_number=r.store_number or "",
                address=r.address or "",
                city=r.city or "",
                state_code=r.state_code or "",
                ticket_number=r.ticket_number or "",
                register=r.register or "",
                receipt_date=r.receipt_date or "",
                purchase_time=r.purchase_time or "",
                subtotal=_money(r.subtotal),
                fees=_money(r.fees),
                tax=_money(r.tax),
                total=_money(r.total),
                card_last4=r.card_last4 or "",
                confidence=r.confidence,
                arithmetic_ok=r.arithmetic_ok,
                arithmetic_note=r.arithmetic_note or "",
                line_items=[
                    ReceiptLineItem(
                        name=str(i.get("name") or ""),
                        qty=i.get("qty"),
                        unit_price=i.get("unitPrice"),
                        amount=str(i.get("amount") or ""),
                        is_brand=bool(i.get("isBrand")),
                    )
                    for i in (r.line_items or [])
                    if isinstance(i, dict)
                ],
                model=r.model or "",
                read_at=_iso(r.model_called_at or (r.read_at if r.status != "pending" else None)),
                error=r.error or "",
                counted_here=bool(rec and rec["ownerRecapId"] == recap_id and rec["counted"]),
                also_on_recap_uuids=others,
            )
        )
    waiting = (
        CustomRecapFile.objects.filter(_receipt_file_q(), custom_recap_id=recap_id)
        .filter(receipt_reading__isnull=True)
        .count()
        + sum(1 for r in readings if r.status == ReceiptReading.STATUS_PENDING)
    )
    flags = [flag_type(row) for row in _flag_qs().filter(custom_recap_id=recap_id).order_by("resolved_at", "-created_at")]
    return RecapReceiptAudit(
        readings=readings,
        flags=flags,
        totals=_totals_type(spend["totals"]),
        waiting_count=waiting,
        flags_enabled=flags_enabled(),
    )


def list_flags(
    *, tenant_id: int | None, date_from: date | None, date_to: date | None,
    kind: str | None, unresolved_only: bool, first: int,
) -> list[RecapStaffFlag]:
    from recaps.receipt_reader import _event_window_q

    qs = _flag_qs()
    if tenant_id:
        qs = qs.filter(tenant_id=tenant_id)
    if kind:
        qs = qs.filter(kind=kind)
    if unresolved_only:
        qs = qs.filter(resolved_at__isnull=True)
    if date_from or date_to:
        qs = qs.filter(_event_window_q("custom_recap__", date_from, date_to))
    qs = qs.order_by("-created_at")[: max(1, min(int(first or 200), MAX_FLAGS))]
    order = {"high": 0, "medium": 1, "low": 2}
    rows = sorted(qs, key=lambda r: (r.resolved_at is not None, order.get(r.severity, 3)))
    return [flag_type(r) for r in rows]


def build_spend_summary(
    *, tenant_id: int | None, date_from: date | None, date_to: date | None, include_csv: bool
) -> ReceiptSpendSummary:
    from recaps.receipt_spend import program_spend, spend_csv

    data = program_spend(tenant_id=tenant_id, date_from=date_from, date_to=date_to)
    recaps = [
        ReceiptSpendRecapRow(
            recap_id=strawberry.ID(str(r["recapId"])),
            recap_uuid=r["recapUuid"],
            event_name=r["eventName"],
            event_date=r["eventDate"],
            open_flags=list(r["openFlags"]),
            totals=_totals_type(r),
        )
        for r in data["recaps"]
    ]
    return ReceiptSpendSummary(
        totals=_totals_type(data["totals"]),
        recaps=recaps,
        receipt_rows=len(data["receipts"]),
        csv=spend_csv(data["receipts"]) if include_csv else None,
    )


def resolve_flag(flag_uuid: str, user_id: int, note: str, reopen: bool) -> tuple[bool, str, RecapStaffFlag | None]:
    row = _flag_qs().filter(uuid=flag_uuid).first()
    if row is None:
        return False, "Flag not found.", None
    note = (note or "").strip()[:500]
    if reopen:
        row.resolved_at = None
        row.resolved_by_id = None
        row.resolution_note = ""
        row.save(update_fields=["resolved_at", "resolved_by", "resolution_note", "updated_at"])
        return True, "Flag reopened.", flag_type(row)
    if not note:
        return False, "Add a short note saying what you found.", flag_type(row)
    row.resolved_at = timezone.now()
    row.resolved_by_id = user_id
    row.resolution_note = note
    row.save(update_fields=["resolved_at", "resolved_by", "resolution_note", "updated_at"])
    row.refresh_from_db()
    row = _flag_qs().get(id=row.id)
    return True, "Flag resolved.", flag_type(row)


# ─── Queries / mutations ────────────────────────────────────────────


@strawberry.type
class ReceiptAuditQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_receipt_audit(
        self, info: strawberry.Info, recap_id: strawberry.ID
    ) -> RecapReceiptAudit | None:
        """Receipt readings, spend and staff flags for one custom recap.
        Ignite staff only; null for anyone else."""
        if not await _is_staff(info):
            return None
        try:
            rid = resolve_id_to_int(str(recap_id).strip())
        except Exception:  # noqa: BLE001
            return None
        try:
            return await sync_to_async(build_recap_audit, thread_sensitive=True)(rid)
        except Exception as exc:  # noqa: BLE001
            logger.exception("recapReceiptAudit failed recap=%s: %s", rid, exc)
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_staff_flags(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        kind: str | None = None,
        unresolved_only: bool = True,
        first: int = 200,
    ) -> list[RecapStaffFlag]:
        """Fraud / quality flags across recaps, highest severity first.
        Ignite staff only; anyone else gets an empty list."""
        if not await _is_staff(info):
            return []
        try:
            tid = _optional_id(tenant_id)
        except Exception:  # noqa: BLE001
            return []
        try:
            return await sync_to_async(list_flags, thread_sensitive=True)(
                tenant_id=tid, date_from=_parse_date(date_from), date_to=_parse_date(date_to),
                kind=(kind or "").strip() or None, unresolved_only=bool(unresolved_only),
                first=first,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("recapStaffFlags failed: %s", exc)
            return []

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def receipt_spend_summary(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_csv: bool = False,
    ) -> ReceiptSpendSummary | None:
        """Receipt spend for a brand (or all brands) over an event-date
        range, each receipt counted once; ``includeCsv`` adds the CSV (one
        row per receipt). Ignite staff only; null for anyone else."""
        if not await _is_staff(info):
            return None
        try:
            tid = _optional_id(tenant_id)
        except Exception:  # noqa: BLE001
            return None
        start, end = _parse_date(date_from), _parse_date(date_to)
        if tid is None and not (start and end):
            return None  # every brand at once needs a date range
        try:
            return await sync_to_async(build_spend_summary, thread_sensitive=True)(
                tenant_id=tid, date_from=start, date_to=end, include_csv=bool(include_csv)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("receiptSpendSummary failed tenant=%s: %s", tid, exc)
            return None


@strawberry.type
class ReceiptAuditMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def resolve_recap_flag(
        self, info: strawberry.Info, input: ResolveRecapFlagInput
    ) -> ResolveRecapFlagPayload:
        """Resolve (with a note) or reopen one staff flag. Ignite staff only.
        Never changes the recap itself."""
        if not await _is_staff(info):
            return ResolveRecapFlagPayload(success=False, message="Not allowed.", flag=None)
        user = info.context.request.user
        user_id = getattr(user, "pk", None) or getattr(user, "id", None)
        try:
            ok, message, flag = await sync_to_async(resolve_flag, thread_sensitive=True)(
                str(input.flag_id).strip(), user_id, input.note, bool(input.reopen)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("resolveRecapFlag failed: %s", exc)
            return ResolveRecapFlagPayload(success=False, message="Could not update the flag.", flag=None)
        return ResolveRecapFlagPayload(success=ok, message=message, flag=flag)
