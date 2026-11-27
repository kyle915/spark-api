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


@strawberry.type
class SparkHelpSourceType:
    title: str
    route_path: str
    route_label: str
    audience: str
    score: float


@strawberry.type
class SparkHelpAnswerResult:
    ok: bool
    reason: str | None
    answer: str
    confidence: str
    matched_via: str
    sources: list[SparkHelpSourceType]
    gap_logged: bool
    is_admin_scope: bool


@strawberry.type
class SparkHelpArticleType:
    id: strawberry.ID
    uuid: str
    title: str
    category: str
    audience: str
    route_path: str
    route_label: str
    tags: list[str]
    body: str
    source_kind: str
    has_embedding: bool
    embedding_model: str
    is_active: bool
    usage_count: int
    updated_at: str


@strawberry.type
class SparkHelpGapType:
    id: strawberry.ID
    uuid: str
    question: str
    route_path: str
    caller_role: str
    source_surface: str
    reason: str
    asked_count: int
    status: str
    last_asked_at: str
    resolved_article_title: str | None


@strawberry.type
class SparkHelpKnowledgePayload:
    ok: bool
    reason: str | None
    is_admin: bool
    builtin_count: int
    embedded_count: int
    open_gap_count: int
    articles: list[SparkHelpArticleType]
    gaps: list[SparkHelpGapType]


@strawberry.type
class SparkHelpArticleMutationResult:
    ok: bool
    reason: str | None
    article: SparkHelpArticleType | None = None


@strawberry.type
class SparkHelpActionResult:
    ok: bool
    reason: str | None = None


@strawberry.type
class SparkHelpEmbeddingSyncResult:
    ok: bool
    reason: str | None
    articles_checked: int
    articles_embedded: int
    builtin_embedded: int


@strawberry.input
class UpsertSparkHelpArticleInput:
    uuid: str | None = None
    title: str
    body: str
    category: str = "general"
    audience: str = "all"
    route_path: str = ""
    route_label: str = ""
    tags: list[str] | None = None
    source_kind: str = "admin_faq"
    is_active: bool = True


@strawberry.input
class ResolveSparkHelpGapInput:
    gap_uuid: str
    title: str
    body: str
    category: str = "general"
    audience: str = "all"
    route_path: str = ""
    route_label: str = ""
    tags: list[str] | None = None


def _article_type(art) -> SparkHelpArticleType:
    return SparkHelpArticleType(
        id=strawberry.ID(str(art.uuid)),
        uuid=str(art.uuid),
        title=art.title,
        category=art.category,
        audience=art.audience,
        route_path=art.route_path or "",
        route_label=art.route_label or "",
        tags=[str(t) for t in (art.tags or [])],
        body=art.body,
        source_kind=art.source_kind,
        has_embedding=bool(isinstance(art.embedding, list) and len(art.embedding) > 0),
        embedding_model=art.embedding_model or "",
        is_active=bool(art.is_active),
        usage_count=int(art.usage_count or 0),
        updated_at=art.updated_at.isoformat() if art.updated_at else "",
    )


def _gap_type(gap) -> SparkHelpGapType:
    return SparkHelpGapType(
        id=strawberry.ID(str(gap.uuid)),
        uuid=str(gap.uuid),
        question=gap.question,
        route_path=gap.route_path or "",
        caller_role=gap.caller_role,
        source_surface=gap.source_surface,
        reason=gap.reason,
        asked_count=int(gap.asked_count or 1),
        status=gap.status,
        last_asked_at=gap.last_asked_at.isoformat() if gap.last_asked_at else "",
        resolved_article_title=(
            gap.resolved_article.title if gap.resolved_article_id and gap.resolved_article else None
        ),
    )


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

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def spark_help_knowledge(
        self,
        info: strawberry.Info,
        audience: str | None = None,
        search: str | None = None,
        gap_status: str | None = "open",
    ) -> SparkHelpKnowledgePayload:
        """Admin-only: list editable Spark Help articles & unanswered Knowledge Gaps."""
        user = _user(info)

        def _build() -> SparkHelpKnowledgePayload:
            from django.db.models import Q

            from .help_knowledge import BUILTIN_HELP_DOCS, is_ignite_admin_user
            from .models import SparkHelpArticle, SparkHelpGap

            if not is_ignite_admin_user(user):
                return SparkHelpKnowledgePayload(
                    ok=False,
                    reason="Ignite admin access is required to manage Spark Help knowledge.",
                    is_admin=False,
                    builtin_count=0,
                    embedded_count=0,
                    open_gap_count=0,
                    articles=[],
                    gaps=[],
                )

            art_qs = SparkHelpArticle.objects.all()
            if audience and audience in {"all", "client", "admin"}:
                art_qs = art_qs.filter(audience=audience)
            if search and search.strip():
                q = search.strip()
                art_qs = art_qs.filter(
                    Q(title__icontains=q)
                    | Q(body__icontains=q)
                    | Q(category__icontains=q)
                    | Q(route_path__icontains=q)
                )
            articles = list(art_qs.order_by("-updated_at", "-id")[:200])

            gap_qs = SparkHelpGap.objects.select_related("resolved_article")
            if gap_status and gap_status in {"open", "resolved", "dismissed"}:
                gap_qs = gap_qs.filter(status=gap_status)
            gaps = list(gap_qs.order_by("-asked_count", "-last_asked_at", "-id")[:150])

            all_active = SparkHelpArticle.objects.filter(is_active=True)
            embedded_count = sum(
                1
                for emb in all_active.values_list("embedding", flat=True)
                if isinstance(emb, list) and len(emb) > 0
            )
            open_gap_count = SparkHelpGap.objects.filter(
                status=SparkHelpGap.STATUS_OPEN
            ).count()

            return SparkHelpKnowledgePayload(
                ok=True,
                reason=None,
                is_admin=True,
                builtin_count=len(BUILTIN_HELP_DOCS),
                embedded_count=embedded_count,
                open_gap_count=open_gap_count,
                articles=[_article_type(a) for a in articles],
                gaps=[_gap_type(g) for g in gaps],
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("sparkHelpKnowledge failed")
            return SparkHelpKnowledgePayload(
                ok=False,
                reason="Could not load Spark Help knowledge.",
                is_admin=False,
                builtin_count=0,
                embedded_count=0,
                open_gap_count=0,
                articles=[],
                gaps=[],
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
        current_path: str | None = None,
    ) -> BrandChatReply:
        """Ask one question about the brand's own data or how to use Spark.
        Read-only: changes nothing except the person's own chat history."""
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
            row = ask(
                user,
                tenant,
                question,
                conversation_id,
                current_path=current_path,
            )
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

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def ask_spark_help(
        self,
        info: strawberry.Info,
        question: str,
        current_path: str | None = None,
        tenant_id: strawberry.ID | None = None,
        preview_as_client: bool = False,
    ) -> SparkHelpAnswerResult:
        """Route-aware Spark Help Q&A backed by hybrid BM25 + OpenAI embeddings.

        Brand clients ALWAYS receive client-safe scope (never admin docs).
        Ignite admins receive full admin + client scope, and may pass
        ``previewAsClient: true`` to test client-only visibility safely.
        """
        user = _user(info)

        def _run() -> SparkHelpAnswerResult:
            from .help_knowledge import (
                answer_spark_help_question,
                is_ignite_admin_user,
            )
            from .service import resolve_tenant

            actual_admin = is_ignite_admin_user(user)
            effective_admin = actual_admin and not bool(preview_as_client)
            tenant = resolve_tenant(user, tenant_id) if tenant_id else None
            res = answer_spark_help_question(
                question,
                is_admin=effective_admin,
                tenant_id=tenant.id if tenant else None,
                user=user,
                current_path=current_path,
                log_gaps=not bool(preview_as_client and actual_admin),
            )
            return SparkHelpAnswerResult(
                ok=bool(res.get("ok")),
                reason=res.get("reason"),
                answer=str(res.get("answer") or ""),
                confidence=str(res.get("confidence") or "medium"),
                matched_via=str(res.get("matched_via") or "bm25"),
                sources=[
                    SparkHelpSourceType(
                        title=str(s.get("title") or ""),
                        route_path=str(s.get("route_path") or ""),
                        route_label=str(s.get("route_label") or "Open"),
                        audience=str(s.get("audience") or "all"),
                        score=float(s.get("score") or 0.0),
                    )
                    for s in (res.get("sources") or [])
                ],
                gap_logged=bool(res.get("gap_logged")),
                is_admin_scope=bool(res.get("is_admin_scope")),
            )

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("askSparkHelp failed")
            return SparkHelpAnswerResult(
                ok=False,
                reason="Spark Help couldn't answer right now.",
                answer="",
                confidence="low",
                matched_via="bm25",
                sources=[],
                gap_logged=False,
                is_admin_scope=False,
            )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def report_brand_chat_unhelpful(
        self,
        info: strawberry.Info,
        turn_id: strawberry.ID,
        current_path: str | None = None,
    ) -> SparkHelpActionResult:
        """Allow any user to flag their own Ask Spark turn as unhelpful so
        Ignite admins see it in Knowledge Gaps. Never writes knowledge."""
        user = _user(info)

        def _run() -> SparkHelpActionResult:
            from .help_knowledge import is_ignite_admin_user, record_help_gap
            from .models import BrandChatTurn

            turn = BrandChatTurn.objects.filter(
                uuid=str(turn_id), user_id=user.pk
            ).first()
            if turn is None or not (turn.question or "").strip():
                return SparkHelpActionResult(ok=False, reason="Turn not found.")
            record_help_gap(
                turn.question,
                is_admin=is_ignite_admin_user(user),
                route_path=current_path,
                source_surface="feedback",
                reason="not_helpful",
                tenant_id=turn.tenant_id,
                user=user,
            )
            return SparkHelpActionResult(ok=True, reason=None)

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("reportBrandChatUnhelpful failed")
            return SparkHelpActionResult(ok=False, reason="Could not record feedback.")

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def upsert_spark_help_article(
        self,
        info: strawberry.Info,
        input: UpsertSparkHelpArticleInput,
    ) -> SparkHelpArticleMutationResult:
        """Admin-only: create or update an editable Spark Help / SOP article and embed it."""
        user = _user(info)

        def _run() -> SparkHelpArticleMutationResult:
            from .help_knowledge import is_ignite_admin_user, upsert_help_article

            if not is_ignite_admin_user(user):
                return SparkHelpArticleMutationResult(
                    ok=False,
                    reason="Ignite admin access is required to add or edit Spark Knowledge.",
                    article=None,
                )
            try:
                art = upsert_help_article(
                    user=user,
                    uuid_str=input.uuid,
                    title=input.title,
                    body=input.body,
                    category=input.category,
                    audience=input.audience,
                    route_path=input.route_path,
                    route_label=input.route_label,
                    tags=input.tags,
                    source_kind=input.source_kind,
                    is_active=input.is_active,
                )
            except ValueError as exc:
                return SparkHelpArticleMutationResult(
                    ok=False, reason=str(exc), article=None
                )
            return SparkHelpArticleMutationResult(
                ok=True, reason=None, article=_article_type(art)
            )

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("upsertSparkHelpArticle failed")
            return SparkHelpArticleMutationResult(
                ok=False, reason="Could not save help article.", article=None
            )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_spark_help_article(
        self, info: strawberry.Info, uuid: strawberry.ID
    ) -> SparkHelpActionResult:
        """Admin-only: delete a custom Spark Help article."""
        user = _user(info)

        def _run() -> SparkHelpActionResult:
            from .help_knowledge import is_ignite_admin_user
            from .models import SparkHelpArticle

            if not is_ignite_admin_user(user):
                return SparkHelpActionResult(
                    ok=False,
                    reason="Ignite admin access is required to delete Spark Knowledge.",
                )
            deleted, _ = SparkHelpArticle.objects.filter(uuid=str(uuid)).delete()
            if not deleted:
                return SparkHelpActionResult(ok=False, reason="Article not found.")
            return SparkHelpActionResult(ok=True, reason=None)

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("deleteSparkHelpArticle failed")
            return SparkHelpActionResult(ok=False, reason="Could not delete article.")

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def resolve_spark_help_gap(
        self,
        info: strawberry.Info,
        input: ResolveSparkHelpGapInput,
    ) -> SparkHelpArticleMutationResult:
        """Admin-only: answer a Knowledge Gap by saving + embedding a new article."""
        user = _user(info)

        def _run() -> SparkHelpArticleMutationResult:
            from django.utils import timezone

            from .help_knowledge import is_ignite_admin_user, upsert_help_article
            from .models import SparkHelpArticle, SparkHelpGap

            if not is_ignite_admin_user(user):
                return SparkHelpArticleMutationResult(
                    ok=False,
                    reason="Ignite admin access is required to resolve Knowledge Gaps.",
                    article=None,
                )
            gap = SparkHelpGap.objects.filter(uuid=str(input.gap_uuid)).first()
            if gap is None:
                return SparkHelpArticleMutationResult(
                    ok=False, reason="Knowledge gap not found.", article=None
                )
            try:
                art = upsert_help_article(
                    user=user,
                    title=input.title,
                    body=input.body,
                    category=input.category,
                    audience=input.audience,
                    route_path=input.route_path or gap.route_path,
                    route_label=input.route_label,
                    tags=input.tags,
                    source_kind=SparkHelpArticle.SOURCE_GAP_RESOLVED,
                )
            except ValueError as exc:
                return SparkHelpArticleMutationResult(
                    ok=False, reason=str(exc), article=None
                )
            gap.status = SparkHelpGap.STATUS_RESOLVED
            gap.resolved_article = art
            gap.resolved_by = user if getattr(user, "pk", None) else None
            gap.resolved_at = timezone.now()
            gap.save(
                update_fields=["status", "resolved_article", "resolved_by", "resolved_at"]
            )
            return SparkHelpArticleMutationResult(
                ok=True, reason=None, article=_article_type(art)
            )

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("resolveSparkHelpGap failed")
            return SparkHelpArticleMutationResult(
                ok=False, reason="Could not resolve knowledge gap.", article=None
            )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def dismiss_spark_help_gap(
        self, info: strawberry.Info, uuid: strawberry.ID
    ) -> SparkHelpActionResult:
        """Admin-only: dismiss a Knowledge Gap without creating an article."""
        user = _user(info)

        def _run() -> SparkHelpActionResult:
            from django.utils import timezone

            from .help_knowledge import is_ignite_admin_user
            from .models import SparkHelpGap

            if not is_ignite_admin_user(user):
                return SparkHelpActionResult(
                    ok=False,
                    reason="Ignite admin access is required to dismiss Knowledge Gaps.",
                )
            updated = SparkHelpGap.objects.filter(uuid=str(uuid)).update(
                status=SparkHelpGap.STATUS_DISMISSED,
                resolved_by=user if getattr(user, "pk", None) else None,
                resolved_at=timezone.now(),
            )
            if not updated:
                return SparkHelpActionResult(ok=False, reason="Knowledge gap not found.")
            return SparkHelpActionResult(ok=True, reason=None)

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("dismissSparkHelpGap failed")
            return SparkHelpActionResult(ok=False, reason="Could not dismiss gap.")

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def sync_spark_help_embeddings(
        self, info: strawberry.Info
    ) -> SparkHelpEmbeddingSyncResult:
        """Admin-only: generate/refresh OpenAI text-embedding-3-small vectors."""
        user = _user(info)

        def _run() -> SparkHelpEmbeddingSyncResult:
            from .help_knowledge import is_ignite_admin_user, sync_missing_embeddings

            if not is_ignite_admin_user(user):
                return SparkHelpEmbeddingSyncResult(
                    ok=False,
                    reason="Ignite admin access is required to sync embeddings.",
                    articles_checked=0,
                    articles_embedded=0,
                    builtin_embedded=0,
                )
            stats = sync_missing_embeddings()
            return SparkHelpEmbeddingSyncResult(
                ok=True,
                reason=None,
                articles_checked=stats["articles_checked"],
                articles_embedded=stats["articles_embedded"],
                builtin_embedded=stats["builtin_embedded"],
            )

        try:
            return await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("syncSparkHelpEmbeddings failed")
            return SparkHelpEmbeddingSyncResult(
                ok=False,
                reason="Could not sync embeddings.",
                articles_checked=0,
                articles_embedded=0,
                builtin_embedded=0,
            )
