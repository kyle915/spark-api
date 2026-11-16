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
