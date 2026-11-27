"""GraphQL for the on-shelf check (see :mod:`recaps.shelf_check`).

* ``recapShelfReadings(recapId)`` — Ignite staff only: each shelf photo on a
  recap with what was read and any staff correction.
* ``readRecapShelfPhotos(recapId)`` — Ignite staff only: read that recap's
  unread shelf photos now.
* ``correctShelfReading(input)`` — Ignite staff only: store a correction,
  which always wins over the reading.
* ``shelfSummary(tenantId, startDate, endDate)`` — Ignite staff for any
  brand, a brand's own users for their brand: counts from approved recaps
  only. No per-photo rows, no staff notes, no ambassador.

Staff is the DB-backed ``events.staffing_board._accessible_tenants`` decision.
"""

from __future__ import annotations

import logging
from datetime import date

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from events.staffing_board import _accessible_tenants
from recaps import shelf_check
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)


@strawberry.type
class ShelfAnswers:
    # "yes" | "no" | "unclear"
    product_on_shelf: str
    facings: int | None
    price_tag_visible: str
    # e.g. "3.49"; null when no price was legible.
    price: str | None
    out_of_stock_gap: str


@strawberry.type
class ShelfCorrection:
    answers: ShelfAnswers
    note: str
    corrected_by_name: str
    corrected_at: str


@strawberry.type
class ShelfPhotoReading:
    file_id: strawberry.ID
    file_url: str
    bucket_name: str
    # "pending" (not read yet) | "read" | "unreadable" (not a shelf photo)
    status: str
    # What was read; null unless status is "read".
    reading: ShelfAnswers | None
    # The staff correction, which is the answer when present.
    correction: ShelfCorrection | None


@strawberry.type
class RecapShelfReadings:
    # False when the caller is not Ignite staff or the recap was not found.
    allowed: bool
    photos: list[ShelfPhotoReading]
    # Set after readRecapShelfPhotos: one plain line about what happened.
    message: str = ""


@strawberry.input
class CorrectShelfReadingInput:
    file_id: strawberry.ID
    product_on_shelf: str
    price_tag_visible: str
    out_of_stock_gap: str
    facings: int | None = None
    price: str | None = None
    note: str = ""


@strawberry.type
class CorrectShelfReadingPayload:
    success: bool
    message: str
    photo: ShelfPhotoReading | None = None


@strawberry.type
class ShelfNotFoundStore:
    store_name: str
    event_date: str


@strawberry.type
class StorePrepKitResource:
    label: str
    kind: str
    url: str
    note: str = ""


@strawberry.type
class StorePrepKitLastShelf:
    event_date: str
    product_on_shelf: str
    price_tag_visible: str
    store_name: str = ""


@strawberry.type
class StorePrepKitStockRisk:
    level: str  # "none" | "watch" | "high"
    reason: str


@strawberry.type
class StorePrepKit:
    """Pre-shift briefing for a BA shift card (logistics + last shelf + SKUs)."""

    logistics_notes: str
    skus: list[str]
    resources: list[StorePrepKitResource]
    last_shelf_visit: StorePrepKitLastShelf | None = None
    stock_risk: StorePrepKitStockRisk | None = None


@strawberry.type
class StorePrepKitPayload:
    allowed: bool
    kit: StorePrepKit | None = None
    message: str = ""


@strawberry.type
class ShelfSummary:
    # "ready" | "none" (no shelf photos with an answer) | "unavailable"
    status: str
    # Approved retail visits with a shelf photo that has an answer: the base
    # of every figure below.
    visits: int
    on_shelf: int
    not_found: int
    unclear: int
    price_tag_visible: int
    # Approved retail visits whose shelf photos have no answer yet.
    unread_visits: int
    not_found_stores: list[ShelfNotFoundStore]
    # Stores not found beyond the ones listed.
    not_found_more: int


@strawberry.type
class ShelfVoiceStoreRow:
    store_key: str
    store_name: str
    market: str
    state_code: str
    visit_count: int
    latest_date: str
    latest_recap_uuid: str
    latest_status: str
    on_shelf_visits: int
    not_found_visits: int
    osa_rate_pct: float | None
    missing_tag_visits: int
    oos_gap_visits: int
    latest_facings: int | None
    avg_facings: float | None
    latest_price: str | None
    price_drift_pct: float | None
    stockout_signal: str
    competitor_promo: str
    has_before_after: bool


@strawberry.type
class ShelfVoicePhoto:
    file_id: str
    photo_url: str
    bucket_name: str
    store_key: str
    store_name: str
    market: str
    state_code: str
    event_date: str
    recap_uuid: str
    product_on_shelf: str
    facings: int | None
    price_tag_visible: str
    price: str | None
    out_of_stock_gap: str


@strawberry.type
class ShelfVoiceBeforeAfterPair:
    store_key: str
    store_name: str
    market: str
    state_code: str
    photo_count: int
    before_photo: ShelfVoicePhoto
    after_photo: ShelfVoicePhoto


@strawberry.type
class ShelfVoiceCompetitorMarket:
    market: str
    state_code: str
    competitor_brand: str
    mention_count: int
    prior_mention_count: int
    delta: int
    trend: str
    sample_quote: str
    sample_store_key: str
    sample_store_name: str
    sample_recap_uuid: str


@strawberry.type
class ShelfVoiceCompetitorPromo:
    store_key: str
    store_name: str
    market: str
    state_code: str
    event_date: str
    recap_uuid: str
    competitor_brand: str
    detail: str


@strawberry.type
class ShelfVoiceHub:
    status: str
    start_date: str
    end_date: str
    audited_visits: int
    unread_visits: int
    on_shelf_count: int
    not_found_count: int
    unclear_count: int
    osa_rate_pct: float | None
    price_tag_visible_count: int
    missing_tag_count: int
    price_tag_rate_pct: float | None
    oos_gaps_count: int
    avg_facings: float | None
    median_price: str | None
    min_price: str | None
    max_price: str | None
    priced_visits_count: int
    stores: list[ShelfVoiceStoreRow]
    shelf_photos: list[ShelfVoicePhoto]
    before_after_pairs: list[ShelfVoiceBeforeAfterPair]
    competitor_markets: list[ShelfVoiceCompetitorMarket]
    competitor_promos: list[ShelfVoiceCompetitorPromo]


def _answers_type(answers: dict) -> ShelfAnswers:
    price = answers.get("price")
    return ShelfAnswers(
        product_on_shelf=answers["product_on_shelf"],
        facings=answers.get("facings"),
        price_tag_visible=answers["price_tag_visible"],
        price=f"{price:.2f}" if price is not None else None,
        out_of_stock_gap=answers["out_of_stock_gap"],
    )


def _photo_type(row: dict) -> ShelfPhotoReading:
    reading, correction = row["reading"], row["correction"]
    status = "pending"
    read_answers = None
    if reading is not None and reading.status == "read":
        status = "read"
        read_answers = _answers_type(shelf_check._answers_of(reading))
    elif reading is not None and reading.status == "unreadable":
        status = "unreadable"
    corrected = None
    if correction is not None:
        user = correction.corrected_by
        name = ""
        if user is not None:
            name = (
                f"{user.first_name or ''} {user.last_name or ''}".strip()
                or (user.email or "")
            )
        corrected = ShelfCorrection(
            answers=_answers_type(shelf_check._answers_of(correction)),
            note=correction.note or "",
            corrected_by_name=name,
            corrected_at=correction.corrected_at.isoformat() if correction.corrected_at else "",
        )
    return ShelfPhotoReading(
        file_id=strawberry.ID(str(row["file_id"])),
        file_url=row["file_url"],
        bucket_name=row["bucket_name"],
        status=status,
        reading=read_answers,
        correction=corrected,
    )


def _not_allowed() -> RecapShelfReadings:
    return RecapShelfReadings(allowed=False, photos=[])


async def _is_staff(info: strawberry.Info) -> bool:
    is_admin, _allowed = await _accessible_tenants(info.context.request.user)
    return bool(is_admin)


def _recap_int(recap_id) -> int | None:
    try:
        return resolve_id_to_int(str(recap_id).strip())
    except Exception:  # noqa: BLE001
        return None


def _load_recap_photos(rid: int) -> RecapShelfReadings:
    from recaps.models import CustomRecap

    if not CustomRecap.objects.filter(id=rid).exists():
        return _not_allowed()
    return RecapShelfReadings(
        allowed=True,
        photos=[_photo_type(row) for row in shelf_check.recap_shelf_photos(rid)],
    )


def _read_message(report: dict) -> str:
    if report.get("ai_unavailable"):
        return "Photo reading is not available right now."
    done = report.get("read", 0) + report.get("unreadable", 0)
    if report.get("failed") or report.get("errors"):
        return "Some photos could not be read. Try again later."
    if done == 0:
        return "No photos left to read."
    return f"Read {done} photo{'s' if done != 1 else ''}."


def _parse_date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip()[:10]) if raw else None
    except ValueError:
        return None


def _empty_summary(status: str) -> ShelfSummary:
    return ShelfSummary(
        status=status,
        visits=0,
        on_shelf=0,
        not_found=0,
        unclear=0,
        price_tag_visible=0,
        unread_visits=0,
        not_found_stores=[],
        not_found_more=0,
    )


def _empty_hub(status: str) -> ShelfVoiceHub:
    return ShelfVoiceHub(
        status=status,
        start_date="",
        end_date="",
        audited_visits=0,
        unread_visits=0,
        on_shelf_count=0,
        not_found_count=0,
        unclear_count=0,
        osa_rate_pct=None,
        price_tag_visible_count=0,
        missing_tag_count=0,
        price_tag_rate_pct=None,
        oos_gaps_count=0,
        avg_facings=None,
        median_price=None,
        min_price=None,
        max_price=None,
        priced_visits_count=0,
        stores=[],
        shelf_photos=[],
        before_after_pairs=[],
        competitor_markets=[],
        competitor_promos=[],
    )


def _hub_photo_type(row: dict) -> ShelfVoicePhoto:
    return ShelfVoicePhoto(
        file_id=str(row["file_id"]),
        photo_url=str(row["photo_url"]),
        bucket_name=str(row.get("bucket_name") or "Shelf"),
        store_key=str(row.get("store_key") or ""),
        store_name=str(row.get("store_name") or "Store"),
        market=str(row.get("market") or ""),
        state_code=str(row.get("state_code") or ""),
        event_date=str(row.get("event_date") or ""),
        recap_uuid=str(row.get("recap_uuid") or ""),
        product_on_shelf=str(row.get("product_on_shelf") or "unclear"),
        facings=row.get("facings"),
        price_tag_visible=str(row.get("price_tag_visible") or "unclear"),
        price=row.get("price"),
        out_of_stock_gap=str(row.get("out_of_stock_gap") or "unclear"),
    )


@strawberry.type
class ShelfCheckQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_shelf_readings(
        self, info: strawberry.Info, recap_id: strawberry.ID
    ) -> RecapShelfReadings:
        """Shelf photos on one custom recap with what was read. Staff only;
        anyone else gets ``allowed: false`` and no photos. Reads nothing new."""
        if not await _is_staff(info):
            return _not_allowed()
        rid = _recap_int(recap_id)
        if rid is None:
            return _not_allowed()
        try:
            return await sync_to_async(_load_recap_photos, thread_sensitive=True)(rid)
        except Exception:  # noqa: BLE001
            logger.exception("recapShelfReadings failed recap=%s", rid)
            return _not_allowed()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def shelf_summary(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> ShelfSummary:
        """On-shelf counts for one brand from approved retail recaps in the
        window (inclusive dates, by event date). Ignite staff may pass any
        brand; a brand user only their own. Never raises."""
        try:
            target = resolve_id_to_int(str(tenant_id).strip())
        except Exception:  # noqa: BLE001
            return _empty_summary("unavailable")
        is_admin, allowed = await _accessible_tenants(info.context.request.user)
        if not is_admin and target not in (allowed or set()):
            return _empty_summary("unavailable")

        def _build() -> ShelfSummary:
            data = shelf_check.shelf_summary(
                target, _parse_date(start_date), _parse_date(end_date)
            )
            return ShelfSummary(
                status="ready" if data["visits"] else "none",
                visits=data["visits"],
                on_shelf=data["on_shelf"],
                not_found=data["not_found"],
                unclear=data["unclear"],
                price_tag_visible=data["price_tag"],
                unread_visits=data["unread_visits"],
                not_found_stores=[
                    ShelfNotFoundStore(
                        store_name=row["store_name"], event_date=row["event_date"]
                    )
                    for row in data["not_found_stores"]
                ],
                not_found_more=data["not_found_more"],
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("shelfSummary failed tenant=%s", target)
            return _empty_summary("unavailable")

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def shelf_voice_hub(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> ShelfVoiceHub:
        """Shelf Compliance, Pricing & Competitor Voice Hub for one brand and window.

        Strictly tenant-scoped (staff for any brand; brand users only their own).
        Reads stored shelf readings, shelf photos, store signals, and competitor
        mentions without invoking any LLM or exposing staff notes / BA PII.
        """
        try:
            target = resolve_id_to_int(str(tenant_id).strip())
        except Exception:  # noqa: BLE001
            return _empty_hub("unavailable")
        is_admin, allowed = await _accessible_tenants(info.context.request.user)
        if not is_admin and target not in (allowed or set()):
            return _empty_hub("unavailable")

        def _build() -> ShelfVoiceHub:
            from recaps.shelf_voice_hub import build_shelf_voice_hub

            data = build_shelf_voice_hub(
                target, _parse_date(start_date), _parse_date(end_date)
            )
            return ShelfVoiceHub(
                status=data["status"],
                start_date=data["start_date"],
                end_date=data["end_date"],
                audited_visits=data["audited_visits"],
                unread_visits=data["unread_visits"],
                on_shelf_count=data["on_shelf_count"],
                not_found_count=data["not_found_count"],
                unclear_count=data["unclear_count"],
                osa_rate_pct=data["osa_rate_pct"],
                price_tag_visible_count=data["price_tag_visible_count"],
                missing_tag_count=data["missing_tag_count"],
                price_tag_rate_pct=data["price_tag_rate_pct"],
                oos_gaps_count=data["oos_gaps_count"],
                avg_facings=data["avg_facings"],
                median_price=data["median_price"],
                min_price=data["min_price"],
                max_price=data["max_price"],
                priced_visits_count=data["priced_visits_count"],
                stores=[
                    ShelfVoiceStoreRow(**row)
                    for row in data["stores"]
                ],
                shelf_photos=[_hub_photo_type(row) for row in data["shelf_photos"]],
                before_after_pairs=[
                    ShelfVoiceBeforeAfterPair(
                        store_key=p["store_key"],
                        store_name=p["store_name"],
                        market=p["market"],
                        state_code=p["state_code"],
                        photo_count=p["photo_count"],
                        before_photo=_hub_photo_type(p["before_photo"]),
                        after_photo=_hub_photo_type(p["after_photo"]),
                    )
                    for p in data["before_after_pairs"]
                ],
                competitor_markets=[
                    ShelfVoiceCompetitorMarket(**row)
                    for row in data["competitor_markets"]
                ],
                competitor_promos=[
                    ShelfVoiceCompetitorPromo(**row)
                    for row in data["competitor_promos"]
                ],
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("shelfVoiceHub failed tenant=%s", target)
            return _empty_hub("unavailable")

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def store_prep_kit(
        self, info: strawberry.Info, event_id: strawberry.ID
    ) -> StorePrepKitPayload:
        """Store Prep Kit for one event: logistics, last shelf, SKUs, resources.

        Ignite staff for any tenant; brand users only for their own events.
        Walk-up check-in also embeds this on the public context payload.
        """
        try:
            eid = resolve_id_to_int(str(event_id).strip())
        except Exception:  # noqa: BLE001
            return StorePrepKitPayload(allowed=False, message="Event not found.")

        def _build() -> StorePrepKitPayload:
            from events.models import Event
            from recaps.store_prep_kit import build_store_prep_kit

            event = (
                Event.objects.select_related("tenant", "request", "retailer")
                .filter(id=eid)
                .first()
            )
            if event is None:
                return StorePrepKitPayload(allowed=False, message="Event not found.")
            return StorePrepKitPayload(
                allowed=True,
                kit=_kit_type(build_store_prep_kit(event)),
            )

        is_admin, allowed = await _accessible_tenants(info.context.request.user)

        def _tenant_id() -> int | None:
            from events.models import Event

            return Event.objects.filter(id=eid).values_list("tenant_id", flat=True).first()

        tenant_id = await sync_to_async(_tenant_id, thread_sensitive=True)()
        if tenant_id is None:
            return StorePrepKitPayload(allowed=False, message="Event not found.")
        if not is_admin and tenant_id not in (allowed or set()):
            return StorePrepKitPayload(allowed=False, message="Not allowed.")

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("storePrepKit failed event=%s", eid)
            return StorePrepKitPayload(allowed=False, message="Unavailable.")


def _kit_type(raw: dict) -> StorePrepKit:
    last = raw.get("lastShelfVisit")
    risk = raw.get("stockRisk")
    resources = [
        StorePrepKitResource(
            label=str(row.get("label") or ""),
            kind=str(row.get("kind") or "link"),
            url=str(row.get("url") or ""),
            note=str(row.get("note") or ""),
        )
        for row in (raw.get("resources") or [])
        if isinstance(row, dict) and (row.get("url") or "").strip()
    ]
    return StorePrepKit(
        logistics_notes=str(raw.get("logisticsNotes") or ""),
        skus=[str(s) for s in (raw.get("skus") or []) if str(s).strip()],
        resources=resources,
        last_shelf_visit=(
            StorePrepKitLastShelf(
                event_date=str(last.get("eventDate") or ""),
                product_on_shelf=str(last.get("productOnShelf") or "unclear"),
                price_tag_visible=str(last.get("priceTagVisible") or "unclear"),
                store_name=str(last.get("storeName") or ""),
            )
            if isinstance(last, dict)
            else None
        ),
        stock_risk=(
            StorePrepKitStockRisk(
                level=str(risk.get("level") or "none"),
                reason=str(risk.get("reason") or ""),
            )
            if isinstance(risk, dict)
            else None
        ),
    )


@strawberry.type
class ShelfCheckMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def read_recap_shelf_photos(
        self, info: strawberry.Info, recap_id: strawberry.ID
    ) -> RecapShelfReadings:
        """Read this recap's unread shelf photos now (a few at a time, with
        a time limit). Staff only. A photo that already has an answer is not
        read again."""
        if not await _is_staff(info):
            return _not_allowed()
        rid = _recap_int(recap_id)
        if rid is None:
            return _not_allowed()

        def _run() -> RecapShelfReadings:
            from recaps.models import CustomRecap

            if not CustomRecap.objects.filter(id=rid).exists():
                return _not_allowed()
            try:
                message = _read_message(shelf_check.read_recap_shelf_photos(rid))
            except Exception:  # noqa: BLE001
                logger.exception("readRecapShelfPhotos failed recap=%s", rid)
                message = "Photos could not be read. Try again later."
            result = _load_recap_photos(rid)
            result.message = message
            return result

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def correct_shelf_reading(
        self, info: strawberry.Info, input: CorrectShelfReadingInput
    ) -> CorrectShelfReadingPayload:
        """Store a staff correction for one shelf photo. It is kept apart
        from the reading and is the answer from then on."""
        if not await _is_staff(info):
            raise GraphQLError("Only Ignite staff can correct a shelf reading.")
        try:
            file_id = int(str(input.file_id).strip())
        except (TypeError, ValueError):
            raise GraphQLError("Photo not found.")
        user = info.context.request.user

        def _save() -> CorrectShelfReadingPayload:
            recap_file = shelf_check.shelf_files().filter(id=file_id).first()
            if recap_file is None:
                return CorrectShelfReadingPayload(success=False, message="Photo not found.")
            try:
                shelf_check.save_correction(
                    recap_file,
                    product_on_shelf=(input.product_on_shelf or "").strip().lower(),
                    facings=input.facings,
                    price_tag_visible=(input.price_tag_visible or "").strip().lower(),
                    price=input.price,
                    out_of_stock_gap=(input.out_of_stock_gap or "").strip().lower(),
                    note=input.note or "",
                    user=user,
                )
            except ValueError as exc:
                return CorrectShelfReadingPayload(success=False, message=str(exc))
            rows = [
                row
                for row in shelf_check.recap_shelf_photos(recap_file.custom_recap_id)
                if row["file_id"] == file_id
            ]
            return CorrectShelfReadingPayload(
                success=True,
                message="Saved.",
                photo=_photo_type(rows[0]) if rows else None,
            )

        return await sync_to_async(_save, thread_sensitive=True)()
