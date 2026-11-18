"""Staff side of the check-in product-questions box.

``baProductQa(tenantId)`` — the brand's BA product notes, whether the box has
anything to answer from, and the questions BAs asked recently.
``setBaProductNotes(tenantId, notes)`` — save the notes.
``setBaBrandWebsite(tenantId, url)`` — the brand site the box also reads.
``refreshBaProductSources(tenantId, scope)`` — re-read documents / website now.
``setBaProductSourceExcluded(tenantId, sourceId, excluded)`` — keep one page or
document out of answers.

Ignite staff only. The notes are shown to every ambassador on the brand's
check-in link, so who may write them is kept narrow.
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async
from django.core.cache import cache
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
REFRESH_COOLDOWN_SECONDS = 60


@strawberry.type
class BaProductQuestionRow:
    question: str
    answer: str
    outcome: str
    asked_at: str
    event_name: str
    source: str


@strawberry.type
class BaProductSource:
    id: strawberry.ID
    kind: str
    kind_label: str
    title: str
    url: str
    label: str
    status: str
    error: str
    excluded: bool
    chars: int
    extracted_at: str
    checked_at: str


@strawberry.type
class BaProductQa:
    notes: str
    has_material: bool
    ai_available: bool
    questions: list[BaProductQuestionRow]
    website_url: str
    website_pages: int
    website_products: int
    document_count: int
    website_refreshed_at: str
    website_refresh_message: str
    documents_refreshed_at: str
    sources: list[BaProductSource]


def _tenant_pk(tenant_id) -> int:
    try:
        return resolve_id_to_int(tenant_id)
    except (GraphQLError, ValueError, TypeError):
        raise GraphQLError("Invalid tenant ID.")


async def _is_staff(info) -> bool:
    user = info.context.request.user
    role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    return _is_admin_access(role_slug, is_staff, is_super, email)


def _iso(dt) -> str:
    return dt.isoformat() if dt else ""


def _sources(tenant_pk: int) -> tuple[list[BaProductSource], dict]:
    from django.db.models.functions import Length

    from product_knowledge.models import KnowledgeRefresh, KnowledgeSource
    from product_knowledge.retrieval import source_label

    rows = (
        KnowledgeSource.objects.filter(tenant_id=tenant_pk)
        .annotate(n_chars=Length("text"))
        .defer("text")
        .order_by("kind", "title", "id")
    )
    out = []
    counts = {"pages": 0, "products": 0, "documents": 0}
    for r in rows:
        if not r.excluded and r.status == KnowledgeSource.STATUS_OK:
            if r.kind == KnowledgeSource.KIND_WEBSITE_PAGE:
                counts["pages"] += 1
            elif r.kind == KnowledgeSource.KIND_SHOPIFY_PRODUCT:
                counts["products"] += 1
            else:
                counts["documents"] += 1
        out.append(
            BaProductSource(
                id=strawberry.ID(str(r.id)),
                kind=r.kind,
                kind_label=r.get_kind_display(),
                title=r.title,
                url=r.url,
                label=source_label(r),
                status=r.status,
                error=r.error,
                excluded=r.excluded,
                chars=int(r.n_chars or 0),
                extracted_at=_iso(r.extracted_at),
                checked_at=_iso(r.checked_at),
            )
        )

    def last(scope):
        return (
            KnowledgeRefresh.objects.filter(tenant_id=tenant_pk, scope=scope)
            .exclude(finished_at=None)
            .order_by("-started_at")
            .first()
        )

    web, docs = last(KnowledgeRefresh.SCOPE_WEBSITE), last(KnowledgeRefresh.SCOPE_DOCUMENTS)
    counts["websiteAt"] = _iso(web.finished_at) if web else ""
    counts["websiteMessage"] = (web.message if web else "") or ""
    counts["documentsAt"] = _iso(docs.finished_at) if docs else ""
    return out, counts


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
    sources, counts = _sources(tenant_pk)
    return BaProductQa(
        notes=tenant.ba_product_notes or "",
        has_material=state["hasMaterial"],
        ai_available=state["aiAvailable"],
        website_url=tenant.brand_website_url or "",
        website_pages=counts["pages"],
        website_products=counts["products"],
        document_count=counts["documents"],
        website_refreshed_at=counts["websiteAt"],
        website_refresh_message=counts["websiteMessage"],
        documents_refreshed_at=counts["documentsAt"],
        sources=sources,
        questions=[
            BaProductQuestionRow(
                question=r.question,
                answer=r.answer,
                outcome=r.outcome,
                asked_at=r.created_at.isoformat() if r.created_at else "",
                event_name=(getattr(r.event, "name", "") or ""),
                source=r.source or "",
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

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_ba_brand_website(
        self, info: strawberry.Info, tenant_id: strawberry.ID, url: str
    ) -> BaProductQa:
        """The brand's own website, crawled weekly for product facts. Pass ""
        to stop using it (its pages are dropped). Staff only."""
        from product_knowledge.crawler import normalize_site_url, site_host

        if not await _is_staff(info):
            raise GraphQLError("Only Ignite staff can change the brand website.")
        tenant_pk = _tenant_pk(tenant_id)
        raw = (url or "").strip()
        site = normalize_site_url(raw)
        if raw and not site:
            raise GraphQLError("Enter the brand's website address, like https://brand.com.")

        def _save() -> BaProductQa | None:
            from product_knowledge.models import KnowledgeSource
            from tenants.models import Tenant

            tenant = Tenant.objects.filter(pk=tenant_pk).first()
            if tenant is None:
                return None
            old = normalize_site_url(tenant.brand_website_url)
            if old and (not site or site_host(old) != site_host(site)):
                # A different site: the old site's pages no longer apply.
                KnowledgeSource.objects.filter(
                    tenant_id=tenant_pk, kind__in=KnowledgeSource.WEBSITE_KINDS
                ).delete()
            tenant.brand_website_url = site
            tenant.save(update_fields=["brand_website_url", "updated_at"])
            return _read(tenant_pk)

        result = await sync_to_async(_save, thread_sensitive=True)()
        if result is None:
            raise GraphQLError("Tenant not found.")
        return result

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def refresh_ba_product_sources(
        self, info: strawberry.Info, tenant_id: strawberry.ID, scope: str = "all"
    ) -> BaProductQa:
        """Re-read the brand's documents and website now (scope: all /
        website / documents). Takes up to a minute or so. Staff only."""
        from product_knowledge.refresh import INTERACTIVE_BUDGET_SECONDS, SCOPES, refresh_tenant

        if not await _is_staff(info):
            raise GraphQLError("Only Ignite staff can refresh product sources.")
        tenant_pk = _tenant_pk(tenant_id)
        if scope not in SCOPES:
            raise GraphQLError("Unknown refresh scope.")
        if not cache.add(f"ba-product-refresh:{tenant_pk}", 1, REFRESH_COOLDOWN_SECONDS):
            raise GraphQLError("A refresh just ran for this brand. Try again in a minute.")

        def _run() -> BaProductQa | None:
            from django.db import connection

            from tenants.models import Tenant

            try:
                tenant = Tenant.objects.filter(pk=tenant_pk).first()
                if tenant is None:
                    return None
                refresh_tenant(tenant, scope=scope, budget_seconds=INTERACTIVE_BUDGET_SECONDS)
                return _read(tenant_pk)
            finally:
                connection.close()

        # Off the shared sync thread: a crawl must not stall other requests.
        result = await sync_to_async(_run, thread_sensitive=False)()
        if result is None:
            raise GraphQLError("Tenant not found.")
        return result

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_ba_product_source_excluded(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        source_id: strawberry.ID,
        excluded: bool,
    ) -> BaProductQa:
        """Keep one website page or document out of BA answers (or let it
        back in). Excluded sources are not re-fetched. Staff only."""
        if not await _is_staff(info):
            raise GraphQLError("Only Ignite staff can change product sources.")
        tenant_pk = _tenant_pk(tenant_id)
        try:
            source_pk = int(str(source_id))
        except (TypeError, ValueError):
            raise GraphQLError("Invalid source ID.")

        def _save() -> BaProductQa | None:
            from product_knowledge.models import KnowledgeSource

            updated = KnowledgeSource.objects.filter(pk=source_pk, tenant_id=tenant_pk).update(
                excluded=bool(excluded)
            )
            return _read(tenant_pk) if updated else None

        result = await sync_to_async(_save, thread_sensitive=True)()
        if result is None:
            raise GraphQLError("Source not found.")
        return result
