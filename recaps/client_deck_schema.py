"""GraphQL for best photos on a recap and the client recap draft.

Everything here is for Ignite staff, decided by the DB-backed check the rest
of the app uses (:func:`events.staffing_board._accessible_tenants`). Anyone
else gets ``ok=false`` and no data. Brand users see best photos only through
``tenantPhotoGallery`` (approved recaps, their own brand).

Nothing in this module sends, shares or deletes anything.
"""

from __future__ import annotations

import base64
import logging
from datetime import date

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

NOT_AVAILABLE = "Not available."


class _Staff(SparkGraphQLMixin):
    async def staff_user(self, info: strawberry.Info):
        """The signed-in user when they are Ignite staff, else None."""
        from events.staffing_board import _accessible_tenants

        try:
            user = await self.get_user(info)
            is_admin, _allowed = await _accessible_tenants(user)
        except Exception:  # noqa: BLE001
            return None
        return user if is_admin else None


# ─── Best photos on one recap ───────────────────────────────────────


@strawberry.type
class RecapPhotoPick:
    """One photo on a recap: what the scorer found and what staff decided.

    ``status`` is ``scored``, ``skipped`` (set aside without a score; ``note``
    says why), ``unreadable`` or ``not_scored``. ``staffChoice`` is
    ``pinned``, ``unpinned`` or blank. ``isPick`` is the result: staff's
    choice when there is one, otherwise the scorer's."""

    file_id: str
    url: str
    category: str
    status: str
    note: str
    score: int | None
    caption: str
    model_pick: bool
    staff_choice: str
    is_pick: bool
    can_pick: bool


@strawberry.type
class RecapPhotoPicksResult:
    ok: bool
    reason: str
    items: list[RecapPhotoPick]


def _picks_result(recap_id: int) -> RecapPhotoPicksResult:
    from recaps.photo_picks import recap_photo_picks

    return RecapPhotoPicksResult(
        ok=True,
        reason="",
        items=[RecapPhotoPick(**item) for item in recap_photo_picks(recap_id)],
    )


def _denied_picks(reason: str = NOT_AVAILABLE) -> RecapPhotoPicksResult:
    return RecapPhotoPicksResult(ok=False, reason=reason, items=[])


def _custom_recap_id(raw_id: str) -> int | None:
    from recaps.models import CustomRecap
    from recaps.scoped_ai import _one, _split_id

    _type_name, token = _split_id(str(raw_id))
    if not token:
        return None
    recap = _one(CustomRecap.objects.all(), token)
    return recap.id if recap is not None else None


@strawberry.type
class BestPhotoQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_photo_picks(
        self, info: strawberry.Info, recap_id: strawberry.ID
    ) -> RecapPhotoPicksResult:
        """The photos on one custom recap with their score, caption and
        whether each is a best photo. Staff only. Reads stored rows; never
        calls a model."""
        if await _Staff().staff_user(info) is None:
            return _denied_picks()

        def _build():
            rid = _custom_recap_id(str(recap_id))
            if rid is None:
                return _denied_picks("Recap not found.")
            return _picks_result(rid)

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("recapPhotoPicks failed: %s", exc)
            return _denied_picks("Best photos could not be loaded.")


@strawberry.type
class BestPhotoMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_recap_photo_pick(
        self, info: strawberry.Info, file_id: strawberry.ID, choice: str
    ) -> RecapPhotoPicksResult:
        """Pin (``pinned``) or unpin (``unpinned``) one photo as a best
        photo, or hand the decision back to the scorer (``""``). Staff only.
        Stored apart from the scorer's own choice and never overwritten by
        it. A receipt cannot be pinned. Returns the recap's photos."""
        user = await _Staff().staff_user(info)
        if user is None:
            return _denied_picks()

        def _apply():
            from recaps.models import CustomRecapFile
            from recaps.photo_picks import set_staff_pick

            try:
                fid = int(str(file_id).strip())
            except ValueError:
                return _denied_picks("Photo not found.")
            outcome = set_staff_pick(fid, (choice or "").strip().lower(), user)
            recap_id = (
                CustomRecapFile.objects.filter(id=fid)
                .values_list("custom_recap_id", flat=True)
                .first()
            )
            if recap_id is None:
                return _denied_picks(outcome["reason"] or "Photo not found.")
            result = _picks_result(recap_id)
            result.ok = bool(outcome["ok"])
            result.reason = outcome["reason"]
            return result

        try:
            return await sync_to_async(_apply, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("setRecapPhotoPick failed: %s", exc)
            return _denied_picks("That change could not be saved.")


# ─── Client recap draft ─────────────────────────────────────────────


@strawberry.type
class ClientRecapNumber:
    label: str
    value: int


@strawberry.type
class ClientRecapBar:
    label: str
    value: int


@strawberry.type
class ClientRecapDraftPhoto:
    id: str
    url: str
    category: str
    caption: str
    store: str
    state: str
    event_date: str
    is_pick: bool


@strawberry.type
class ClientRecapDraftType:
    """A staff draft of a client recap. ``textSource`` is ``model``,
    ``fallback`` (built from the numbers, because the model was unavailable
    or its reply did not hold up), ``summary`` (the weekly or monthly client
    summary already written for these dates) or ``edited``."""

    uuid: str
    tenant_id: str
    tenant_name: str
    start_date: str
    end_date: str
    period: str
    title: str
    summary: str
    takeaways: list[str]
    text_source: str
    numbers: list[ClientRecapNumber]
    weeks: list[ClientRecapBar]
    stores: list[ClientRecapBar]
    states: list[ClientRecapBar]
    quotes: list[str]
    photos: list[ClientRecapDraftPhoto]
    created_at: str
    updated_at: str


@strawberry.type
class ClientRecapDraftResult:
    ok: bool
    reason: str
    draft: ClientRecapDraftType | None


@strawberry.type
class ClientRecapDraftSummary:
    uuid: str
    tenant_name: str
    title: str
    period: str
    updated_at: str


@strawberry.type
class ClientRecapDraftPdf:
    ok: bool
    reason: str
    filename: str
    pdf_base64: str


def _draft_type(draft) -> ClientRecapDraftType:
    from recaps.client_recap_draft import draft_photos, period_label

    facts = draft.facts or {}
    themes = facts.get("themes") or []
    quotes = (
        [t["quote"] for t in themes if t.get("quote")]
        if themes
        else list(facts.get("comments") or [])
    )[:6]
    return ClientRecapDraftType(
        uuid=str(draft.uuid),
        tenant_id=str(draft.tenant_id),
        tenant_name=draft.tenant.name or "",
        start_date=draft.start_date.isoformat(),
        end_date=draft.end_date.isoformat(),
        period=facts.get("period") or period_label(draft.start_date, draft.end_date),
        title=draft.title,
        summary=draft.summary,
        takeaways=[str(t) for t in (draft.takeaways or [])],
        text_source=draft.text_source,
        numbers=[
            ClientRecapNumber(label=n["label"], value=int(n["value"]))
            for n in facts.get("numbers") or []
        ],
        weeks=[
            ClientRecapBar(label=w["week_of"], value=int(w["consumers_sampled"]))
            for w in facts.get("consumers_sampled_by_week") or []
        ],
        stores=[
            ClientRecapBar(label=s["label"], value=int(s["consumers_sampled"]))
            for s in facts.get("top_stores") or []
        ],
        states=[
            ClientRecapBar(label=s["label"], value=int(s["consumers_sampled"]))
            for s in facts.get("states") or []
        ],
        quotes=quotes,
        photos=[
            ClientRecapDraftPhoto(**{k: v for k, v in p.items() if not k.startswith("_")})
            for p in draft_photos(draft)
        ],
        created_at=draft.created_at.isoformat(),
        updated_at=draft.updated_at.isoformat(),
    )


def _no_draft(reason: str = NOT_AVAILABLE) -> ClientRecapDraftResult:
    return ClientRecapDraftResult(ok=False, reason=reason, draft=None)


def _load_draft(raw_uuid: str):
    import uuid as _uuid

    from recaps.models import ClientRecapDraft

    try:
        _uuid.UUID(str(raw_uuid))
    except (ValueError, AttributeError, TypeError):
        return None
    return ClientRecapDraft.objects.select_related("tenant").filter(uuid=str(raw_uuid)).first()


def _parse_day(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def _tenant(raw_id: str):
    from tenants.models import Tenant
    from utils.graphql.mixins import resolve_id_to_int

    try:
        return Tenant.objects.filter(id=resolve_id_to_int(str(raw_id))).first()
    except Exception:  # noqa: BLE001
        return None


@strawberry.type
class ClientRecapDraftQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def client_recap_draft(
        self, info: strawberry.Info, uuid: str
    ) -> ClientRecapDraftResult:
        """One client recap draft. Staff only."""
        if await _Staff().staff_user(info) is None:
            return _no_draft()

        def _build():
            draft = _load_draft(uuid)
            if draft is None:
                return _no_draft("Draft not found.")
            return ClientRecapDraftResult(ok=True, reason="", draft=_draft_type(draft))

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("clientRecapDraft failed: %s", exc)
            return _no_draft("The draft could not be loaded.")

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def client_recap_drafts(
        self, info: strawberry.Info, tenant_id: strawberry.ID, first: int = 20
    ) -> list[ClientRecapDraftSummary]:
        """A brand's recent client recap drafts, newest first. Staff only;
        an empty list for anyone else."""
        if await _Staff().staff_user(info) is None:
            return []

        def _build():
            from recaps.client_recap_draft import period_label
            from recaps.models import ClientRecapDraft

            tenant = _tenant(str(tenant_id))
            if tenant is None:
                return []
            rows = ClientRecapDraft.objects.filter(tenant=tenant).select_related("tenant")[
                : max(1, min(int(first or 20), 50))
            ]
            return [
                ClientRecapDraftSummary(
                    uuid=str(d.uuid),
                    tenant_name=d.tenant.name or "",
                    title=d.title,
                    period=period_label(d.start_date, d.end_date),
                    updated_at=d.updated_at.isoformat(),
                )
                for d in rows
            ]

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("clientRecapDrafts failed: %s", exc)
            return []

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def client_recap_draft_pdf(
        self, info: strawberry.Info, uuid: str
    ) -> ClientRecapDraftPdf:
        """The draft as a PDF, base64-encoded, for staff to download. Staff
        only. Rendered on request and not stored, sent or shared."""
        denied = ClientRecapDraftPdf(ok=False, reason=NOT_AVAILABLE, filename="", pdf_base64="")
        if await _Staff().staff_user(info) is None:
            return denied

        def _build():
            from recaps.client_recap_draft import (
                ClientRecapDraftError,
                pdf_filename,
                render_draft_pdf,
            )

            draft = _load_draft(uuid)
            if draft is None:
                return ClientRecapDraftPdf(
                    ok=False, reason="Draft not found.", filename="", pdf_base64=""
                )
            try:
                data = render_draft_pdf(draft)
            except ClientRecapDraftError as exc:
                return ClientRecapDraftPdf(ok=False, reason=str(exc), filename="", pdf_base64="")
            return ClientRecapDraftPdf(
                ok=True,
                reason="",
                filename=pdf_filename(draft),
                pdf_base64=base64.b64encode(data).decode("ascii"),
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("clientRecapDraftPdf failed: %s", exc)
            return ClientRecapDraftPdf(
                ok=False, reason="The PDF could not be made.", filename="", pdf_base64=""
            )


@strawberry.type
class ClientRecapDraftMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_client_recap_draft(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str,
        end_date: str,
    ) -> ClientRecapDraftResult:
        """Draft a client recap for a brand and date range: numbers from the
        dashboard roll-ups, the best photos, and a first-draft summary with
        takeaways written from approved recaps. Staff only. The draft is for
        staff to edit; it is not sent or shared with anyone."""
        user = await _Staff().staff_user(info)
        if user is None:
            return _no_draft()
        start, end = _parse_day(start_date), _parse_day(end_date)
        if start is None or end is None:
            return _no_draft("Pick a start and an end date.")

        def _create():
            from recaps.client_recap_draft import ClientRecapDraftError, create_draft

            tenant = _tenant(str(tenant_id))
            if tenant is None:
                return _no_draft("Choose a client first.")
            try:
                draft = create_draft(tenant, start, end, user)
            except ClientRecapDraftError as exc:
                return _no_draft(str(exc))
            return ClientRecapDraftResult(ok=True, reason="", draft=_draft_type(draft))

        try:
            return await sync_to_async(_create, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("createClientRecapDraft failed: %s", exc)
            return _no_draft("The draft could not be made.")

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_client_recap_draft(
        self,
        info: strawberry.Info,
        uuid: str,
        title: str | None = None,
        summary: str | None = None,
        takeaways: list[str] | None = None,
        photo_ids: list[strawberry.ID] | None = None,
    ) -> ClientRecapDraftResult:
        """Save staff edits to a draft: the title, the summary, the
        takeaways, and which photos it uses (in order). Only what is passed
        changes. Staff only."""
        user = await _Staff().staff_user(info)
        if user is None:
            return _no_draft()

        def _update():
            from recaps.client_recap_draft import ClientRecapDraftError, update_draft

            draft = _load_draft(uuid)
            if draft is None:
                return _no_draft("Draft not found.")
            try:
                draft = update_draft(
                    draft,
                    user,
                    title=title,
                    summary=summary,
                    takeaways=takeaways,
                    photo_ids=[str(p) for p in photo_ids] if photo_ids is not None else None,
                )
            except ClientRecapDraftError as exc:
                return _no_draft(str(exc))
            return ClientRecapDraftResult(ok=True, reason="", draft=_draft_type(draft))

        try:
            return await sync_to_async(_update, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("updateClientRecapDraft failed: %s", exc)
            return _no_draft("Your changes could not be saved.")
