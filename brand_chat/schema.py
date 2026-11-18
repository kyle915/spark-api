"""GraphQL for brand chat (``schema_clients``).

* ``brandChat(tenantId, conversationId)``: the person's own chat for the
  brand (the asked conversation, or their latest), suggested questions and
  how many questions they have left this hour.
* ``askBrandChat(tenantId, question, conversationId)``: ask one question.
  Read-only: the model can only call the read tools in
  :mod:`brand_chat.tools`. Omit ``conversationId`` to start a new chat.
* ``clearBrandChat(tenantId, conversationId)``: wipe the person's own
  history for the brand (one chat, or all of it).

The brand is worked out on the server from the caller's membership
(:func:`brand_chat.service.resolve_tenant`); a tenant id the caller does
not belong to is never read. Nothing here raises: a failure is ``ok:
false`` and a plain ``reason``.
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.permissions import StrictIsAuthenticated

from . import conf

logger = logging.getLogger(__name__)

NO_BRAND = "Brand chat is not available for this account."


@strawberry.type
class BrandChatSource:
    tool: str
    label: str
    detail: str
    link: str
    link_label: str


@strawberry.type
class BrandChatLink:
    label: str
    url: str


@strawberry.type
class BrandChatTurnType:
    id: strawberry.ID
    conversation_id: strawberry.ID
    question: str
    answer: str
    # answered | not_grounded | failed | limited
    status: str
    sources: list[BrandChatSource]
    links: list[BrandChatLink]
    created_at: str


@strawberry.type
class BrandChatView:
    ok: bool
    reason: str | None
    brand_name: str
    conversation_id: strawberry.ID | None
    turns: list[BrandChatTurnType]
    suggestions: list[str]
    questions_per_hour: int
    questions_left_this_hour: int


@strawberry.type
class BrandChatReply:
    ok: bool
    reason: str | None
    conversation_id: strawberry.ID | None
    turn: BrandChatTurnType | None
    questions_left_this_hour: int


@strawberry.type
class BrandChatClearResult:
    ok: bool
    reason: str | None
    cleared: int


def _turn_type(row) -> BrandChatTurnType:
    return BrandChatTurnType(
        id=strawberry.ID(str(row.uuid)),
        conversation_id=strawberry.ID(str(row.conversation)),
        question=row.question,
        answer=row.answer,
        status=row.status,
        sources=[
            BrandChatSource(
                tool=str(s.get("tool") or ""),
                label=str(s.get("label") or ""),
                detail=str(s.get("detail") or ""),
                link=str(s.get("link") or ""),
                link_label=str(s.get("link_label") or ""),
            )
            for s in (row.sources or [])
            if isinstance(s, dict)
        ],
        links=[
            BrandChatLink(label=str(link.get("label") or ""), url=str(link.get("url") or ""))
            for link in (row.links or [])
            if isinstance(link, dict)
        ],
        created_at=row.created_at.isoformat(),
    )


def _user(info):
    user = info.context.request.user
    return user if getattr(user, "is_authenticated", False) else None


def _left(user_id: int, tenant_id: int) -> int:
    from .service import usage

    return max(0, conf.questions_per_hour_per_user() - usage(user_id, tenant_id)["user_last_hour"])


@strawberry.type
class BrandChatQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def brand_chat(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        conversation_id: strawberry.ID | None = None,
    ) -> BrandChatView:
        user = _user(info)

        def _build() -> BrandChatView:
            from .service import resolve_tenant, suggestions, visible_turns

            tenant = resolve_tenant(user, tenant_id)
            if tenant is None:
                return BrandChatView(
                    ok=False, reason=NO_BRAND, brand_name="", conversation_id=None, turns=[],
                    suggestions=[], questions_per_hour=0, questions_left_this_hour=0,
                )
            convo, rows = visible_turns(user.pk, tenant.id, conversation_id)
            try:
                ideas = suggestions(tenant.id)
            except Exception:  # noqa: BLE001
                logger.exception("brand chat suggestions failed tenant=%s", tenant.id)
                ideas = []
            return BrandChatView(
                ok=True,
                reason=None if conf.enabled() else "Brand chat is turned off right now.",
                brand_name=tenant.name,
                conversation_id=strawberry.ID(str(convo)) if convo else None,
                turns=[_turn_type(r) for r in rows],
                suggestions=ideas,
                questions_per_hour=conf.questions_per_hour_per_user(),
                questions_left_this_hour=_left(user.pk, tenant.id),
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("brandChat failed")
            return BrandChatView(
                ok=False, reason="Brand chat could not be loaded.", brand_name="",
                conversation_id=None, turns=[], suggestions=[], questions_per_hour=0,
                questions_left_this_hour=0,
            )


@strawberry.type
class BrandChatMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def ask_brand_chat(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        question: str,
        conversation_id: strawberry.ID | None = None,
    ) -> BrandChatReply:
        """Ask one question about the brand's own data. Changes nothing in
        Spark except the person's own chat history."""
        user = _user(info)
        if not " ".join((question or "").split()):
            return BrandChatReply(
                ok=False, reason="A question is required.", conversation_id=None, turn=None,
                questions_left_this_hour=0,
            )

        def _run() -> BrandChatReply:
            from .service import ask, resolve_tenant

            tenant = resolve_tenant(user, tenant_id)
            if tenant is None:
                return BrandChatReply(
                    ok=False, reason=NO_BRAND, conversation_id=None, turn=None,
                    questions_left_this_hour=0,
                )
            row = ask(user, tenant, question, conversation_id)
            return BrandChatReply(
                ok=row.status != row.STATUS_LIMITED,
                reason=row.answer if row.status == row.STATUS_LIMITED else None,
                conversation_id=strawberry.ID(str(row.conversation)),
                turn=_turn_type(row),
                questions_left_this_hour=_left(user.pk, tenant.id),
            )

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("askBrandChat failed")
            return BrandChatReply(
                ok=False, reason="Brand chat couldn't answer just now.", conversation_id=None,
                turn=None, questions_left_this_hour=0,
            )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def clear_brand_chat(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        conversation_id: strawberry.ID | None = None,
    ) -> BrandChatClearResult:
        """Clear the person's own chat history for the brand: one chat when
        ``conversationId`` is given, else all of it."""
        user = _user(info)

        def _run() -> BrandChatClearResult:
            from .service import clear, resolve_tenant

            tenant = resolve_tenant(user, tenant_id)
            if tenant is None:
                return BrandChatClearResult(ok=False, reason=NO_BRAND, cleared=0)
            return BrandChatClearResult(
                ok=True, reason=None, cleared=clear(user.pk, tenant.id, conversation_id)
            )

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("clearBrandChat failed")
            return BrandChatClearResult(ok=False, reason="History could not be cleared.", cleared=0)
