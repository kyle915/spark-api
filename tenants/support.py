"""Support-ticket capture + Ignite-team notification for the web Help page.

The web Help page (``spark-front-client`` ``SparkHelp.tsx``) was fully static —
FAQs plus ``mailto:`` links. ``createSupportTicket`` lets a signed-in user
submit a request that is CAPTURED as a :class:`tenants.models.SupportTicket`
row AND notifies the Ignite team.

The Ignite-team recipient list is NOT hardcoded here. We REUSE the exact same
resolution the request-approval email uses (see ``events/mutations.py``):

* ``IGNITE_REVIEW_CC`` — the static historical Ignite ops list
  (events@, kyle@, myriant@, nevena@, madison@), defined in ``events/routing.py``.
* ``_get_spark_admin_emails`` — every active ``role=spark-admin`` user, so a
  newly-added admin is copied automatically without a settings edit.
* ``REQUEST_REVIEW_COPY_EMAILS`` (via ``_get_request_review_copy_emails``) —
  the env-configured review copy list.
* ``suppress_cc`` — strips the known stray/typo address from the composed list.

If none of those resolve to a real recipient we still SAVE the ticket and
return success (the notify is best-effort), and log a warning so the gap is
visible — we never invent an address.
"""
from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

from .models import SupportTicket
from .types import SupportTicketType

logger = logging.getLogger(__name__)

# Valid category slugs. Anything else (or empty) collapses to "other" so a
# stale/garbage value from the form can't land an un-renderable category.
_VALID_CATEGORIES = {
    SupportTicket.CATEGORY_QUESTION,
    SupportTicket.CATEGORY_BUG,
    SupportTicket.CATEGORY_BILLING,
    SupportTicket.CATEGORY_OTHER,
}


@strawberry.input
class CreateSupportTicketInput(SparkGraphQLInput):
    """A support request from the Help page. Scoped server-side to the
    authenticated user + their tenant — neither is accepted from the client."""

    subject: str
    body: str
    # One of question/bug/billing/other. Omitted / unknown → "other".
    category: str | None = None


@strawberry.type
class CreateSupportTicketResponse:
    success: bool
    message: str
    ticket: SupportTicketType | None = None
    client_mutation_id: strawberry.ID | None = None


def _resolve_ignite_recipients() -> list[str]:
    """Resolve the Ignite-team recipient list by REUSING the request-approval
    email's resolution (``events/mutations.py``). Returns a deduped, suppressed
    list of addresses — empty if nothing is configured.

    Imported lazily to avoid an import cycle (events imports tenants models).
    """
    from events.mutations import (
        _get_request_review_copy_emails,
        _get_spark_admin_emails,
    )
    from events.routing import IGNITE_REVIEW_CC, suppress_cc

    composed = list(IGNITE_REVIEW_CC)
    composed += _get_spark_admin_emails()
    composed += _get_request_review_copy_emails()
    # Dedupe case-insensitively, preserve first-seen order, then drop the
    # known suppressed address(es) — same hygiene as the approval CC.
    seen: set[str] = set()
    deduped: list[str] = []
    for email in composed:
        normalized = (email or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return suppress_cc(deduped)


def _normalize_category(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    return value if value in _VALID_CATEGORIES else SupportTicket.CATEGORY_OTHER


class _SupportTicketScope(SparkGraphQLMixin):
    """Tenant-scoping shell for the admin support-ticket query. Mirrors
    ``tenants/forms.py`` ``_FormScope`` / ``recaps/report_types.py``
    ``_CampaignReportService``: clients are pinned to their own tenant, admins
    may target any tenant via an explicit id."""

    async def resolve_target_tenant_id(
        self, info: strawberry.Info, requested_tenant_id
    ) -> int | None:
        """The CONCRETE tenant id the caller may operate on, or None.

        * **client** — always their own tenant (``requested_tenant_id`` ignored).
        * **admin** — the requested tenant id, or None when none/garbage was
          passed (caller turns that into ``[]`` rather than raising).
        """
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )

        if not _is_admin_access(role_slug, is_staff, is_super, email):
            tenant = await self.get_user_tenant(info)
            return tenant.id if tenant else None

        if requested_tenant_id is None:
            return None
        raw = str(requested_tenant_id).strip()
        if not raw:
            return None
        try:
            return resolve_id_to_int(raw)
        except Exception:
            return None


@strawberry.type
class SupportTicketQueries:
    """Admin-facing read of captured support tickets. Exposed on the clients
    schema (same endpoint the web admin uses)."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_support_tickets(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
    ) -> list[SupportTicketType]:
        """List a tenant's captured support tickets (newest first).

        Tenant-scoped: clients see only their own tenant's tickets (the
        ``tenantId`` argument is overridden to their tenant); admins see the
        requested tenant's tickets. Never raises — returns ``[]`` for an
        out-of-scope request or on error.
        """
        scope = _SupportTicketScope()
        try:
            resolved = await scope.resolve_target_tenant_id(info, tenant_id)
        except Exception:  # noqa: BLE001
            return []
        if resolved is None:
            return []

        @sync_to_async
        def _load() -> list[SupportTicket]:
            return list(
                SupportTicket.objects.filter(tenant_id=resolved).select_related(
                    "tenant", "created_by"
                )
            )

        try:
            return await _load()
        except Exception:  # noqa: BLE001
            return []


@strawberry.type
class SupportTicketMutations:
    """Help-page support-ticket mutation. Exposed on the clients schema (the
    web app talks to the clients GraphQL endpoint)."""

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_support_ticket(
        self,
        info: strawberry.Info,
        input: CreateSupportTicketInput,
    ) -> CreateSupportTicketResponse:
        """Capture a support request as a row scoped to the authenticated user
        + their tenant, then notify the Ignite team (best-effort).

        ``StrictIsAuthenticated`` gates the call; past the gate we never raise —
        any failure returns ``success=False``. A mail hiccup never fails the
        save (mirrors the recaps approval-notify error handling): the ticket is
        committed first, then the notify is wrapped in try/except + logger.
        """
        user = info.context.request.user

        subject = (input.subject or "").strip()
        body = (input.body or "").strip()
        if not subject or not body:
            return CreateSupportTicketResponse(
                success=False,
                message="Subject and message are required.",
                client_mutation_id=input.client_mutation_id,
            )

        category = _normalize_category(input.category)

        # Resolve the submitter's bound tenant (may be None — a user without a
        # tenant can still file a ticket; we just notify without a brand name).
        try:
            tenant = await sync_to_async(user.get_tenant)()
        except Exception:  # noqa: BLE001
            tenant = None

        @sync_to_async
        def _create() -> SupportTicket:
            return SupportTicket.objects.create(
                tenant=tenant,
                created_by=user,
                subject=subject,
                body=body,
                category=category,
            )

        try:
            ticket = await _create()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to save support ticket: %s", exc)
            return CreateSupportTicketResponse(
                success=False,
                message="Could not submit your support request. Please try again.",
                client_mutation_id=input.client_mutation_id,
            )

        # ── Notify the Ignite team (best-effort; never fails the save) ──
        try:
            from .envelopes import SupportTicketIgniteNotificationMailer

            recipients = await sync_to_async(_resolve_ignite_recipients)()
            if recipients:
                submitter_name = (
                    (user.get_full_name() or "").strip()
                    if hasattr(user, "get_full_name")
                    else ""
                ) or (getattr(user, "email", "") or "")
                submitter_email = (getattr(user, "email", "") or "").strip()
                tenant_name = (getattr(tenant, "name", "") or "").strip() or None

                mailer = SupportTicketIgniteNotificationMailer(
                    to_emails=recipients,
                    subject=subject,
                    body=body,
                    category=category,
                    submitter_name=submitter_name,
                    submitter_email=submitter_email,
                    tenant_name=tenant_name,
                    reply_to_email=submitter_email or None,
                )
                await sync_to_async(mailer.send)()
            else:
                # No recipient resolved — save still succeeded. Surface the gap
                # without inventing an address.
                logger.warning(
                    "Support ticket %s saved but no Ignite-team recipients "
                    "resolved (IGNITE_REVIEW_CC / spark-admins / "
                    "REQUEST_REVIEW_COPY_EMAILS all empty) — no notify sent.",
                    ticket.id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "support-ticket Ignite notify failed for ticket=%s: %s",
                ticket.id,
                exc,
            )

        return CreateSupportTicketResponse(
            success=True,
            message="Your support request has been submitted.",
            ticket=ticket,
            client_mutation_id=input.client_mutation_id,
        )
