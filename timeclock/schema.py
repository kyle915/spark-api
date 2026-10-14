"""Admin GraphQL for clock links. Ignite staff only.

Sessions carry worker names, emails, and GPS fixes, so client users get
``available: false`` / errors rather than data.
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import Count, Max, Q
from graphql import GraphQLError
from strawberry import relay

from events.event_confirmations import public_page_base
from tenants.models import Tenant
from timeclock import sheets
from timeclock.models import ClockLink, ClockSession
from timeclock.services import safe_timezone
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)
from utils.utils import build_mutation_response

logger = logging.getLogger(__name__)

DEFAULT_SHARE_WITH = "kyle@igniteproductions.co"
SESSIONS_CAP = 500


async def _is_staff(info: strawberry.Info) -> bool:
    user = info.context.request.user
    if not user or not user.is_authenticated:
        return False
    return _is_admin_access(*(await resolve_request_user_access(user)))


async def _require_staff(info: strawberry.Info):
    if not await _is_staff(info):
        raise GraphQLError("Clock links are Ignite staff only.")
    return info.context.request.user


def clock_url(link: ClockLink) -> str:
    return f"{public_page_base()}/clock/{link.token}"


@strawberry.type(name="ClockSession")
class ClockSessionType:
    id: strawberry.ID
    first_name: str
    last_name: str
    email: str
    market: str
    address: str
    timezone: str
    clock_in_at: str
    clock_in_gps: str
    clock_in_accuracy_m: float | None
    clock_in_map_url: str | None
    clock_out_at: str | None
    clock_out_gps: str | None
    clock_out_accuracy_m: float | None
    clock_out_map_url: str | None
    duration_minutes: int | None
    sheet_synced: bool
    sheet_error: str | None


@strawberry.type(name="ClockLink")
class ClockLinkType:
    id: strawberry.ID
    label: str
    url: str
    tenant_id: strawberry.ID | None
    tenant_name: str | None
    sheet_url: str | None
    is_active: bool
    created_at: str
    session_count: int
    open_count: int
    last_clock_at: str | None
    sync_pending: int
    sync_error: str | None


@strawberry.type(name="ClockLinks")
class ClockLinksType:
    available: bool
    service_account_email: str | None = None
    links: list[ClockLinkType] = strawberry.field(default_factory=list)


def _session_type(s: ClockSession) -> ClockSessionType:
    return ClockSessionType(
        id=strawberry.ID(str(s.uuid)),
        first_name=s.first_name,
        last_name=s.last_name,
        email=s.email,
        market=s.market,
        address=s.address,
        timezone=s.timezone,
        clock_in_at=s.clock_in_at.isoformat(),
        clock_in_gps=s.clock_in_gps,
        clock_in_accuracy_m=s.clock_in_accuracy_m,
        clock_in_map_url=sheets.maps_url(s.clock_in_lat, s.clock_in_lng) or None,
        clock_out_at=s.clock_out_at.isoformat() if s.clock_out_at else None,
        clock_out_gps=s.clock_out_gps or None,
        clock_out_accuracy_m=s.clock_out_accuracy_m,
        clock_out_map_url=sheets.maps_url(s.clock_out_lat, s.clock_out_lng) or None,
        duration_minutes=s.duration_minutes,
        sheet_synced=bool(s.link.sheet_id) and not s.sheet_dirty,
        sheet_error=s.sheet_error or None,
    )


def _link_rows(link_ids: list[int] | None = None) -> list[ClockLinkType]:
    qs = ClockLink.objects.select_related("tenant").annotate(
        n_sessions=Count("sessions"),
        n_open=Count("sessions", filter=Q(sessions__clock_out_at__isnull=True)),
        n_dirty=Count("sessions", filter=Q(sessions__sheet_dirty=True)),
        last_in=Max("sessions__clock_in_at"),
        last_out=Max("sessions__clock_out_at"),
    )
    if link_ids is not None:
        qs = qs.filter(id__in=link_ids)
    rows = []
    for link in qs:
        error = (
            ClockSession.objects.filter(link=link, sheet_dirty=True)
            .exclude(sheet_error="")
            .values_list("sheet_error", flat=True)
            .first()
        )
        last = max([d for d in (link.last_in, link.last_out) if d], default=None)
        rows.append(
            ClockLinkType(
                id=strawberry.ID(str(link.uuid)),
                label=link.label,
                url=clock_url(link),
                tenant_id=strawberry.ID(str(link.tenant_id)) if link.tenant_id else None,
                tenant_name=link.tenant.name if link.tenant_id else None,
                sheet_url=link.sheet_url or None,
                is_active=link.is_active,
                created_at=link.created_at.isoformat(),
                session_count=link.n_sessions,
                open_count=link.n_open,
                last_clock_at=last.isoformat() if last else None,
                sync_pending=link.n_dirty if link.sheet_id else 0,
                sync_error=error if link.sheet_id else None,
            )
        )
    return rows


def _link_by_id(link_id) -> ClockLink:
    link = ClockLink.objects.select_related("tenant").filter(uuid=str(link_id)).first()
    if link is None:
        raise GraphQLError("Clock link not found.")
    return link


def _tenant(tenant_id) -> Tenant | None:
    if tenant_id in (None, ""):
        return None
    try:
        pk = resolve_id_to_int(tenant_id)
    except Exception as exc:  # noqa: BLE001
        raise GraphQLError("Unknown client.") from exc
    tenant = Tenant.objects.filter(pk=pk).first()
    if tenant is None:
        raise GraphQLError("Unknown client.")
    return tenant


@strawberry.type
class ClockLinkQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def clock_links(self, info: strawberry.Info) -> ClockLinksType:
        if not await _is_staff(info):
            return ClockLinksType(available=False)
        rows = await sync_to_async(_link_rows)()
        email = await sync_to_async(sheets.service_account_email)()
        return ClockLinksType(available=True, service_account_email=email, links=rows)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def clock_sessions(
        self, info: strawberry.Info, link_id: strawberry.ID, limit: int = 100
    ) -> list[ClockSessionType]:
        await _require_staff(info)

        def _go():
            link = _link_by_id(link_id)
            qs = ClockSession.objects.select_related("link").filter(link=link)
            return [_session_type(s) for s in qs[: max(1, min(limit, SESSIONS_CAP))]]

        return await sync_to_async(_go)()


@strawberry.input
class CreateClockLinkInput(SparkGraphQLInput):
    label: str
    tenant_id: strawberry.ID | None = None
    # "create" (new Sheet shared to share_with) | "paste" (sheet_url) | "none"
    sheet_mode: str = "create"
    sheet_url: str | None = None
    share_with: list[str] | None = None
    timezone: str | None = None


@strawberry.input
class UpdateClockLinkInput(SparkGraphQLInput):
    link_id: strawberry.ID
    label: str | None = None
    is_active: bool | None = None
    tenant_id: strawberry.ID | None = None
    clear_tenant: bool = False
    sheet_url: str | None = None


@strawberry.input
class ClockLinkIdInput(SparkGraphQLInput):
    link_id: strawberry.ID


@strawberry.input
class DeleteClockSessionInput(SparkGraphQLInput):
    session_id: strawberry.ID


@strawberry.type
class ClockLinkResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    link: ClockLinkType | None = None


@strawberry.type
class ClockActionResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


def _one_row(link: ClockLink) -> ClockLinkType:
    return _link_rows([link.id])[0]


def _create(user, input: CreateClockLinkInput) -> tuple[ClockLink, str]:
    label = " ".join((input.label or "").split())
    if not label:
        raise GraphQLError("Give the link a name.")
    if len(label) > 120:
        raise GraphQLError("Keep the name under 120 characters.")
    mode = (input.sheet_mode or "create").strip().lower()
    if mode not in ("create", "paste", "none"):
        raise GraphQLError("Pick a Sheet option.")
    if mode == "paste" and not (input.sheet_url or "").strip():
        raise GraphQLError("Paste the Google Sheet link.")
    link = ClockLink.objects.create(
        label=label,
        tenant=_tenant(input.tenant_id),
        timezone=safe_timezone(input.timezone),
        created_by=user if getattr(user, "pk", None) else None,
    )
    note = "Clock link ready."
    try:
        if mode == "paste":
            sheets.attach_existing_sheet(link, input.sheet_url or "")
            note = "Clock link ready and connected to your Sheet."
        elif mode == "create":
            share = [e.strip().lower() for e in (input.share_with or [DEFAULT_SHARE_WITH]) if e and e.strip()]
            sheets.create_sheet(link, share)
            note = f"Clock link ready. New Sheet shared with {', '.join(share)}."
    except sheets.SheetSetupError as exc:
        note = f"Clock link ready, but no Sheet yet: {exc}"
    except Exception:  # noqa: BLE001 — the link is still usable without a sheet
        logger.exception("clock link sheet setup failed link=%s", link.pk)
        note = "Clock link ready, but setting up the Sheet failed. Paste a Sheet link to retry."
    return link, note


def _update(input: UpdateClockLinkInput) -> tuple[ClockLink, str]:
    link = _link_by_id(input.link_id)
    fields = []
    if input.label is not None:
        label = " ".join(input.label.split())
        if not label:
            raise GraphQLError("Give the link a name.")
        link.label = label[:120]
        fields.append("label")
    if input.is_active is not None:
        link.is_active = input.is_active
        fields.append("is_active")
    if input.clear_tenant:
        link.tenant = None
        fields.append("tenant")
    elif input.tenant_id is not None:
        link.tenant = _tenant(input.tenant_id)
        fields.append("tenant")
    if fields:
        link.save(update_fields=[*fields, "updated_at"])
    note = "Saved."
    if input.sheet_url is not None and input.sheet_url.strip():
        try:
            sheets.attach_existing_sheet(link, input.sheet_url)
        except sheets.SheetSetupError as exc:
            raise GraphQLError(str(exc)) from exc
        ClockSession.objects.filter(link=link).update(sheet_dirty=True, sheet_row=None)
        result = sheets.sync_dirty(link=link)
        note = f"Sheet connected. Wrote {result['synced']} of {result['attempted']} sessions."
    return link, note


def _delete_session(session_id) -> str:
    session = ClockSession.objects.select_related("link").filter(uuid=str(session_id)).first()
    if session is None:
        raise GraphQLError("Session not found.")
    try:
        removed = sheets.delete_row(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("clock session row delete failed session=%s", session.pk, exc_info=True)
        raise GraphQLError(
            "Couldn't remove the Sheet row, so the session was kept. Try again."
        ) from exc
    session.delete()
    return "Session deleted and its Sheet row removed." if removed else "Session deleted."


@strawberry.type
class ClockLinkMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_clock_link(
        self, info: strawberry.Info, input: CreateClockLinkInput
    ) -> ClockLinkResponse:
        user = await _require_staff(info)

        def _go():
            link, note = _create(user, input)
            return _one_row(link), note

        row, note = await sync_to_async(_go)()
        return build_mutation_response(
            ClockLinkResponse, success=True, message=note, input_obj=input, link=row
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_clock_link(
        self, info: strawberry.Info, input: UpdateClockLinkInput
    ) -> ClockLinkResponse:
        await _require_staff(info)

        def _go():
            link, note = _update(input)
            return _one_row(link), note

        row, note = await sync_to_async(_go)()
        return build_mutation_response(
            ClockLinkResponse, success=True, message=note, input_obj=input, link=row
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def sync_clock_sheet(
        self, info: strawberry.Info, input: ClockLinkIdInput
    ) -> ClockLinkResponse:
        await _require_staff(info)

        def _go():
            link = _link_by_id(input.link_id)
            if not link.sheet_id:
                raise GraphQLError("This link has no Sheet yet.")
            result = sheets.sync_dirty(link=link)
            return _one_row(link), result

        row, result = await sync_to_async(_go)()
        ok = result["failed"] == 0
        message = (
            "Sheet is up to date."
            if result["attempted"] == 0
            else f"Wrote {result['synced']} of {result['attempted']} sessions to the Sheet."
        )
        return build_mutation_response(
            ClockLinkResponse, success=ok, message=message, input_obj=input, link=row
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_clock_session(
        self, info: strawberry.Info, input: DeleteClockSessionInput
    ) -> ClockActionResponse:
        await _require_staff(info)
        message = await sync_to_async(_delete_session)(input.session_id)
        return build_mutation_response(
            ClockActionResponse, success=True, message=message, input_obj=input
        )
