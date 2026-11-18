"""GraphQL for sharing a client recap deck with the brand.

* ``setClientRecapDeckStatus`` — staff mark a deck ``ready`` (the brand's
  users can then list and download it) or move it back to ``draft``.
  Nothing is emailed or sent either way.
* ``clientRecapDecks`` — the ready decks for a brand. A brand user only
  ever gets their own brand's (the same scoping the program report uses);
  staff can ask for any brand.
* ``clientRecapDeckPdf`` — download one deck. A brand user may only
  download a ready deck of their own brand; staff may download any deck.

Drafting and editing stay in ``recaps/client_deck_schema.py`` (staff only).
"""

from __future__ import annotations

import base64
import logging

import strawberry
from asgiref.sync import sync_to_async

from recaps.client_deck_schema import (
    NOT_AVAILABLE,
    ClientRecapDraftPdf,
    ClientRecapDraftResult,
    _draft_type,
    _load_draft,
    _no_draft,
    _Staff,
)
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)


@strawberry.type
class ClientRecapDeckSummary:
    uuid: str
    tenant_name: str
    title: str
    period: str
    ready_at: str


async def _brand_tenant_id(info: strawberry.Info, requested) -> int | None:
    """The brand a non-staff user may read, or None. Ambassadors get
    nothing."""
    from recaps.report_types import _CampaignReportService
    from utils.graphql.permissions import resolve_request_user_access

    service = _CampaignReportService()
    try:
        user = await service.get_user(info)
        role_slug, _is_staff, _is_super, _email = await resolve_request_user_access(user)
        if (role_slug or "").lower() == "ambassador":
            return None
        return await service.resolve_target_tenant_id(info, requested)
    except Exception:  # noqa: BLE001
        return None


def _pdf_denied(reason: str = NOT_AVAILABLE) -> ClientRecapDraftPdf:
    return ClientRecapDraftPdf(ok=False, reason=reason, filename="", pdf_base64="")


@strawberry.type
class ClientRecapDeckQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def client_recap_decks(
        self, info: strawberry.Info, tenant_id: strawberry.ID, first: int = 20
    ) -> list[ClientRecapDeckSummary]:
        """Recap decks Ignite has shared with a brand, newest first. Only
        decks staff marked ready; never a draft."""
        if await _Staff().staff_user(info) is not None:
            from recaps.client_deck_schema import _tenant

            tenant = await sync_to_async(_tenant, thread_sensitive=True)(str(tenant_id))
            target = tenant.id if tenant is not None else None
        else:
            target = await _brand_tenant_id(info, tenant_id)
        if target is None:
            return []

        def _build():
            from recap_decks.builder import ready_decks
            from recaps.client_recap_draft import period_label

            return [
                ClientRecapDeckSummary(
                    uuid=str(deck.draft.uuid),
                    tenant_name=deck.draft.tenant.name or "",
                    title=deck.draft.title,
                    period=period_label(deck.draft.start_date, deck.draft.end_date),
                    ready_at=deck.ready_at.isoformat() if deck.ready_at else "",
                )
                for deck in ready_decks(target, first)
            ]

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("clientRecapDecks failed: %s", exc)
            return []

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def client_recap_deck_pdf(self, info: strawberry.Info, uuid: str) -> ClientRecapDraftPdf:
        """One deck as a PDF, base64-encoded. Brand users: ready decks of
        their own brand only. Rendered on request; nothing is stored or
        sent."""
        is_staff = await _Staff().staff_user(info) is not None

        def _load():
            draft = _load_draft(uuid)
            if draft is None:
                return None, None
            from recap_decks.builder import deck_for

            return draft, deck_for(draft).status

        draft, status = await sync_to_async(_load, thread_sensitive=True)()
        if draft is None:
            return _pdf_denied()
        if not is_staff:
            from recap_decks.models import ClientRecapDeck

            if status != ClientRecapDeck.STATUS_READY:
                return _pdf_denied()
            if await _brand_tenant_id(info, str(draft.tenant_id)) != draft.tenant_id:
                return _pdf_denied()

        def _render():
            from recaps.client_recap_draft import (
                ClientRecapDraftError,
                pdf_filename,
                render_draft_pdf,
            )

            try:
                data = render_draft_pdf(draft)
            except ClientRecapDraftError as exc:
                return _pdf_denied(str(exc))
            return ClientRecapDraftPdf(
                ok=True,
                reason="",
                filename=pdf_filename(draft),
                pdf_base64=base64.b64encode(data).decode("ascii"),
            )

        try:
            return await sync_to_async(_render, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("clientRecapDeckPdf failed: %s", exc)
            return _pdf_denied("The PDF could not be made.")


@strawberry.type
class ClientRecapDeckMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_client_recap_deck_status(
        self, info: strawberry.Info, uuid: str, status: str
    ) -> ClientRecapDraftResult:
        """``ready`` shares the deck with the brand's users (it shows under
        their reports; nothing is emailed). ``draft`` takes it back. Staff
        only."""
        user = await _Staff().staff_user(info)
        if user is None:
            return _no_draft()

        def _apply():
            from recap_decks.builder import DeckError, set_status

            draft = _load_draft(uuid)
            if draft is None:
                return _no_draft("Draft not found.")
            try:
                set_status(draft, (status or "").strip().lower(), user)
            except DeckError as exc:
                return _no_draft(str(exc))
            return ClientRecapDraftResult(ok=True, reason="", draft=_draft_type(draft))

        try:
            return await sync_to_async(_apply, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("setClientRecapDeckStatus failed: %s", exc)
            return _no_draft("That change could not be saved.")
