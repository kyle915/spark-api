"""Staff-only GraphQL (``schema_clients``) for linking tenants to Close leads
and reading the sales-assist audit log.

* ``salesAssistLinks`` — every tenant <-> Close lead link.
* ``salesAssistDrafts(kind, first)`` — recent drafts (dry runs, created,
  skipped, failed) with their text, for review.
* ``linkTenantToClose`` / ``unlinkTenantFromClose`` — the ONLY way a tenant
  gets linked besides the cron ``link`` action; the fuzzy-name match never
  links anything by itself.

Staff is decided from the database (``recaps.weekly_summary._staff_user``);
everyone else gets an empty list or ``ok: false``. Nothing here calls Close
or sends anything.
"""

from __future__ import annotations

from datetime import date

import strawberry
from asgiref.sync import sync_to_async

from recaps.weekly_summary import _person, _staff_user
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated


@strawberry.type
class SalesAssistLink:
    id: strawberry.ID
    tenant_id: strawberry.ID
    tenant_name: str
    close_lead_id: str
    close_opportunity_id: str
    renewal_date: str | None
    contact_email: str
    active: bool
    linked_by_name: str | None


@strawberry.type
class SalesAssistDraftItem:
    id: strawberry.ID
    kind: str
    status: str
    reason: str
    tenant_name: str | None
    close_lead_id: str
    lead_name: str
    period_start: str | None
    period_end: str | None
    note_text: str
    email_to: str
    email_subject: str
    email_body: str
    close_note_id: str
    close_email_id: str
    est_cost_usd: float
    created_at: str


@strawberry.type
class SalesAssistResult:
    ok: bool
    reason: str | None = None
    link: SalesAssistLink | None = None


def _link_type(link) -> SalesAssistLink:
    return SalesAssistLink(
        id=strawberry.ID(f"SalesAssistLink:{link.id}"),
        tenant_id=strawberry.ID(str(link.tenant_id)),
        tenant_name=link.tenant.name,
        close_lead_id=link.close_lead_id,
        close_opportunity_id=link.close_opportunity_id,
        renewal_date=link.renewal_date.isoformat() if link.renewal_date else None,
        contact_email=link.contact_email,
        active=link.active,
        linked_by_name=_person(link.linked_by) if link.linked_by_id else None,
    )


def _draft_type(row) -> SalesAssistDraftItem:
    return SalesAssistDraftItem(
        id=strawberry.ID(f"SalesAssistDraft:{row.id}"),
        kind=row.kind,
        status=row.status,
        reason=row.reason,
        tenant_name=row.tenant.name if row.tenant_id else None,
        close_lead_id=row.close_lead_id,
        lead_name=row.lead_name,
        period_start=row.period_start.isoformat() if row.period_start else None,
        period_end=row.period_end.isoformat() if row.period_end else None,
        note_text=row.note_text,
        email_to=row.email_to,
        email_subject=row.email_subject,
        email_body=row.email_body,
        close_note_id=row.close_note_id,
        close_email_id=row.close_email_id,
        est_cost_usd=float(row.est_cost_usd),
        created_at=row.created_at.isoformat(),
    )


@strawberry.type
class SalesAssistQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def sales_assist_links(self, info: strawberry.Info) -> list[SalesAssistLink]:
        if await _staff_user(info) is None:
            return []

        def _load():
            from sales_assist.models import CloseLink

            return [
                _link_type(link)
                for link in CloseLink.objects.select_related("tenant", "linked_by").order_by("tenant__name")
            ]

        return await sync_to_async(_load)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def sales_assist_drafts(
        self, info: strawberry.Info, kind: str | None = None, first: int = 50
    ) -> list[SalesAssistDraftItem]:
        if await _staff_user(info) is None:
            return []

        def _load():
            from sales_assist.models import SalesAssistDraft

            qs = SalesAssistDraft.objects.select_related("tenant").order_by("-created_at")
            if kind:
                qs = qs.filter(kind=kind)
            return [_draft_type(r) for r in qs[: max(1, min(int(first or 50), 200))]]

        return await sync_to_async(_load)()


@strawberry.type
class SalesAssistMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def link_tenant_to_close(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        close_lead_id: str,
        close_opportunity_id: str | None = None,
        renewal_date: str | None = None,
        contact_email: str | None = None,
    ) -> SalesAssistResult:
        user = await _staff_user(info)
        if user is None:
            return SalesAssistResult(ok=False, reason="Only Ignite staff can link Close leads.")
        lead_id = (close_lead_id or "").strip()
        if not lead_id.startswith("lead_"):
            return SalesAssistResult(ok=False, reason="A Close lead id starts with lead_.")
        opp = (close_opportunity_id or "").strip()
        if opp and not opp.startswith("oppo_"):
            return SalesAssistResult(ok=False, reason="A Close opportunity id starts with oppo_.")
        renews = None
        if renewal_date:
            try:
                renews = date.fromisoformat(renewal_date)
            except ValueError:
                return SalesAssistResult(ok=False, reason="Renewal date must be YYYY-MM-DD.")
        tid = resolve_id_to_int(tenant_id)

        def _save():
            from sales_assist.models import CloseLink
            from tenants.models import Tenant

            tenant = Tenant.objects.filter(id=tid).first()
            if tenant is None:
                return SalesAssistResult(ok=False, reason="Tenant not found.")
            link, _ = CloseLink.objects.update_or_create(
                tenant=tenant,
                defaults={
                    "close_lead_id": lead_id,
                    "close_opportunity_id": opp,
                    "renewal_date": renews,
                    "contact_email": (contact_email or "").strip(),
                    "active": True,
                    "linked_by": user,
                },
            )
            link = CloseLink.objects.select_related("tenant", "linked_by").get(id=link.id)
            return SalesAssistResult(ok=True, link=_link_type(link))

        return await sync_to_async(_save)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def unlink_tenant_from_close(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> SalesAssistResult:
        if await _staff_user(info) is None:
            return SalesAssistResult(ok=False, reason="Only Ignite staff can unlink Close leads.")
        tid = resolve_id_to_int(tenant_id)

        def _save():
            from sales_assist.models import CloseLink

            updated = CloseLink.objects.filter(tenant_id=tid).update(active=False)
            return SalesAssistResult(ok=bool(updated), reason=None if updated else "No link for that tenant.")

        return await sync_to_async(_save)()
