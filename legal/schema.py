"""GraphQL for legal acceptance (clients + spark schemas).

- ``myLegalStatus``: what the caller must accept and what they have.
- ``acceptLegalTerms(version)``: record a clickwrap acceptance (IP via the
  right-most trusted X-Forwarded-For hop, user agent, timestamp).
- ``legalAcceptanceReport``: staff-only report of who accepted what, when.
"""

from __future__ import annotations

import logging
from datetime import datetime

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

from . import service

logger = logging.getLogger("spark.legal")


@strawberry.type
class LegalDocumentStatus:
    document: str
    title: str
    path: str
    version: str
    accepted_version: str | None
    accepted_at: datetime | None
    needs_acceptance: bool


@strawberry.type
class LegalStatus:
    # True for admin + client users (the people this gate applies to).
    required: bool
    needs_acceptance: bool
    # Echo this back to acceptLegalTerms.
    current_version: str
    # True when they accepted an older version: show "What changed".
    is_update: bool
    change_summary: str | None
    documents: list[LegalDocumentStatus]


@strawberry.type
class AcceptLegalTermsResponse:
    success: bool
    message: str
    # "VERSION_MISMATCH" when the page showed an older version than current.
    code: str | None
    status: LegalStatus | None


@strawberry.type
class LegalAcceptanceReportRow:
    user_uuid: str
    email: str
    name: str
    role: str
    # "accepted" (current version) | "outdated" (an older version) | "never"
    status: str
    accepted_version: str | None
    accepted_at: datetime | None
    ip_address: str
    user_agent: str


@strawberry.type
class LegalAcceptanceReport:
    current_version: str
    total_users: int
    accepted_count: int
    pending_count: int
    rows: list[LegalAcceptanceReportRow]


def _to_status(data: service.LegalStatusData) -> LegalStatus:
    return LegalStatus(
        required=data.required,
        needs_acceptance=data.needs_acceptance,
        current_version=data.current_version,
        is_update=data.is_update,
        change_summary=(data.change_summary or None) if data.is_update else None,
        documents=[
            LegalDocumentStatus(
                document=d.key,
                title=d.title,
                path=d.path,
                version=d.version,
                accepted_version=d.accepted_version,
                accepted_at=d.accepted_at,
                needs_acceptance=d.needs_acceptance,
            )
            for d in data.documents
        ],
    )


def _request(info: strawberry.Info):
    return getattr(info.context, "request", None)


@strawberry.type
class LegalQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_legal_status(self, info: strawberry.Info) -> LegalStatus:
        user = _request(info).user
        data = await sync_to_async(service.legal_status, thread_sensitive=True)(user.pk)
        return _to_status(data)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def legal_acceptance_report(
        self,
        info: strawberry.Info,
        pending_only: bool = False,
        search: str | None = None,
    ) -> LegalAcceptanceReport:
        """Staff only: every active admin/client user and their acceptance."""
        user = _request(info).user
        role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
        if not _is_admin_access(role_slug, is_staff, is_super, email):
            raise GraphQLError(
                "Only Ignite staff can view legal acceptances.",
                extensions={"code": "FORBIDDEN"},
            )

        def _build():
            all_rows = service.acceptance_report(search=search or "")
            accepted = sum(1 for r in all_rows if r.status == "accepted")
            rows = [r for r in all_rows if r.status != "accepted"] if pending_only else all_rows
            return all_rows, accepted, rows

        all_rows, accepted, rows = await sync_to_async(_build, thread_sensitive=True)()
        from . import versions

        return LegalAcceptanceReport(
            current_version=versions.current_version(),
            total_users=len(all_rows),
            accepted_count=accepted,
            pending_count=len(all_rows) - accepted,
            rows=[
                LegalAcceptanceReportRow(
                    user_uuid=r.user_uuid,
                    email=r.email,
                    name=r.name,
                    role=r.role,
                    status=r.status,
                    accepted_version=r.accepted_version,
                    accepted_at=r.accepted_at,
                    ip_address=r.ip_address,
                    user_agent=r.user_agent,
                )
                for r in rows
            ],
        )


@strawberry.type
class LegalMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def accept_legal_terms(
        self, info: strawberry.Info, version: str
    ) -> AcceptLegalTermsResponse:
        """Record that the caller agreed to the Terms of Service and Acceptable
        Use Policy and has read the Privacy Policy, at ``version``."""
        request = _request(info)
        user = request.user
        ip = service.client_ip(request)
        ua = service.user_agent(request)
        try:
            data = await sync_to_async(service.accept_current, thread_sensitive=True)(
                user.pk, version=version, ip=ip, ua=ua
            )
        except service.VersionMismatch as exc:
            data = await sync_to_async(service.legal_status, thread_sensitive=True)(user.pk)
            return AcceptLegalTermsResponse(
                success=False,
                message=(
                    "The Terms were updated while this page was open. "
                    f"Please review version {exc.args[0]} and accept again."
                ),
                code="VERSION_MISMATCH",
                status=_to_status(data),
            )
        except Exception:  # noqa: BLE001
            logger.exception("acceptLegalTerms failed for user %s", user.pk)
            return AcceptLegalTermsResponse(
                success=False,
                message="Could not record your acceptance. Please try again.",
                code="ERROR",
                status=None,
            )
        return AcceptLegalTermsResponse(
            success=True,
            message="Thanks — your acceptance is recorded.",
            code=None,
            status=_to_status(data),
        )
