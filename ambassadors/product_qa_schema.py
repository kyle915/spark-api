"""Staff side of the check-in product-questions box.

``baProductQa(tenantId)`` — the brand's BA product notes, whether the box has
anything to answer from, and the questions BAs asked recently.
``setBaProductNotes(tenantId, notes)`` — save the notes.

Ignite staff only. The notes are shown to every ambassador on the brand's
check-in link, so who may write them is kept narrow.
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

logger = logging.getLogger(__name__)

NOTES_MAX_CHARS = 8000
RECENT_QUESTIONS = 40


@strawberry.type
class BaProductQuestionRow:
    question: str
    answer: str
    outcome: str
    asked_at: str
    event_name: str


@strawberry.type
class BaProductQa:
    notes: str
    has_material: bool
    ai_available: bool
    questions: list[BaProductQuestionRow]


async def _is_staff(info) -> bool:
    user = info.context.request.user
    role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    return _is_admin_access(role_slug, is_staff, is_super, email)


def _read(tenant_pk: int) -> BaProductQa | None:
    from ambassadors.product_qa import public_state
    from tenants.models import BaProductQuestion, Tenant

    tenant = Tenant.objects.filter(pk=tenant_pk).first()
    if tenant is None:
        return None
    state = public_state(tenant)
    rows = (
        BaProductQuestion.objects.filter(tenant_id=tenant_pk)
        .select_related("event")
        .order_by("-created_at")[:RECENT_QUESTIONS]
    )
    return BaProductQa(
        notes=tenant.ba_product_notes or "",
        has_material=state["hasMaterial"],
        ai_available=state["aiAvailable"],
        questions=[
            BaProductQuestionRow(
                question=r.question,
                answer=r.answer,
                outcome=r.outcome,
                asked_at=r.created_at.isoformat() if r.created_at else "",
                event_name=(getattr(r.event, "name", "") or ""),
            )
            for r in rows
        ],
    )


@strawberry.type
class BaProductQaQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ba_product_qa(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> BaProductQa | None:
        """This brand's BA product notes + recent BA questions. Staff only;
        anyone else gets null."""
        try:
            if not await _is_staff(info):
                return None
            tenant_pk = resolve_id_to_int(tenant_id)
        except Exception:  # noqa: BLE001
            return None
        try:
            return await sync_to_async(_read, thread_sensitive=True)(tenant_pk)
        except Exception as exc:  # noqa: BLE001
            logger.warning("baProductQa failed tenant=%s: %s", tenant_pk, exc)
            return None


@strawberry.type
class BaProductQaMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_ba_product_notes(
        self, info: strawberry.Info, tenant_id: strawberry.ID, notes: str
    ) -> BaProductQa:
        """Save the facts ambassadors' product questions are answered from.
        Pass "" to clear. Staff only."""
        if not await _is_staff(info):
            raise GraphQLError("Only Ignite staff can change product notes.")
        try:
            tenant_pk = resolve_id_to_int(tenant_id)
        except (GraphQLError, ValueError, TypeError):
            raise GraphQLError("Invalid tenant ID.")
        text = (notes or "").replace("\r\n", "\n").strip()
        if len(text) > NOTES_MAX_CHARS:
            raise GraphQLError(f"Keep product notes under {NOTES_MAX_CHARS} characters.")
        user = info.context.request.user

        def _save() -> BaProductQa | None:
            from tenants.models import Tenant

            tenant = Tenant.objects.filter(pk=tenant_pk).first()
            if tenant is None:
                return None
            tenant.ba_product_notes = text
            tenant.updated_by_id = getattr(user, "id", None) or tenant.updated_by_id
            tenant.save(update_fields=["ba_product_notes", "updated_by", "updated_at"])
            return _read(tenant_pk)

        result = await sync_to_async(_save, thread_sensitive=True)()
        if result is None:
            raise GraphQLError("Tenant not found.")
        return result
