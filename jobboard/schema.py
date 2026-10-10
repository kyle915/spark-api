"""Admin GraphQL for the job board: the link, per-gig shifts, and bookings.

Ignite staff only. Brand users get ``available: false`` and no data, since
bookings carry BA contact details.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError
from strawberry import relay

from events.demo_cancel import request_display_code
from events.envelopes import _admin_request_url
from events.event_confirmations import public_page_base
from events.field_marketing import MARKETS, _active_tenant_for_user, _user_can_pick_any_tenant
from jobboard.gigs import (
    board_for_tenant,
    date_label,
    default_open_shifts,
    list_gigs,
    pending_plans,
    time_label,
)
from jobboard.models import JobBoardBooking
from jobboard.services import CancelError, cancel_booking, set_gig
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.permissions import StrictIsAuthenticated
from utils.utils import build_mutation_response

_MARKET_LABELS = {key: label for key, label, *_ in MARKETS}


@strawberry.type(name="JobBoardBooking")
class JobBoardBookingType:
    id: strawberry.ID
    name: str
    phone: str
    email: str
    note: str
    created_at: str


@strawberry.type(name="JobBoardGig")
class JobBoardGigType:
    id: strawberry.ID
    venue: str
    date_label: str
    time_label: str
    market: str
    type_label: str
    request_code: str
    request_url: str
    open_shifts: int
    default_open_shifts: int
    listed: bool
    booked: int
    spots_left: int
    bookings: list[JobBoardBookingType]


@strawberry.type(name="JobBoardPendingPlan")
class JobBoardPendingPlanType:
    id: strawberry.ID
    name: str
    starts_on: str | None
    market: str
    request_code: str


@strawberry.type(name="JobBoard")
class JobBoardType:
    available: bool
    token: str | None = None
    url: str | None = None
    gigs: list[JobBoardGigType] = strawberry.field(default_factory=list)
    pending: list[JobBoardPendingPlanType] = strawberry.field(default_factory=list)


def _booking_type(b: JobBoardBooking) -> JobBoardBookingType:
    p = b.phone
    return JobBoardBookingType(
        id=strawberry.ID(str(b.uuid)),
        name=b.name,
        phone=f"({p[:3]}) {p[3:6]}-{p[6:]}" if len(p) == 10 else p,
        email=b.email,
        note=b.note,
        created_at=b.created_at.isoformat(),
    )


def _gig_type(gig, bookings: list[JobBoardBooking]) -> JobBoardGigType:
    request = gig.plan.request
    return JobBoardGigType(
        id=strawberry.ID(str(gig.event.uuid)),
        venue=gig.venue,
        date_label=date_label(gig),
        time_label=time_label(gig),
        market=gig.area or _MARKET_LABELS.get(gig.plan.market, gig.plan.market or ""),
        type_label=gig.type_label,
        request_code=request_display_code(request.id),
        request_url=_admin_request_url(request),
        open_shifts=gig.open_shifts,
        default_open_shifts=default_open_shifts(gig.plan),
        listed=gig.listed,
        booked=gig.booked,
        spots_left=gig.spots_left,
        bookings=[_booking_type(b) for b in bookings],
    )


def build_admin_board(tenant) -> JobBoardType:
    board = board_for_tenant(tenant)
    if board is None:
        return JobBoardType(available=False)
    gigs = list_gigs(tenant)
    live = JobBoardBooking.objects.filter(
        event_id__in=[g.event.id for g in gigs], cancelled_at__isnull=True
    ).order_by("created_at")
    by_event: dict[int, list[JobBoardBooking]] = {}
    for b in live:
        by_event.setdefault(b.event_id, []).append(b)
    return JobBoardType(
        available=True,
        token=board.token,
        url=f"{public_page_base()}/jobs/{board.token}",
        gigs=[_gig_type(g, by_event.get(g.event.id, [])) for g in gigs],
        pending=[
            JobBoardPendingPlanType(
                id=strawberry.ID(str(p.uuid)),
                name=p.name,
                starts_on=p.starts_on.isoformat() if p.starts_on else None,
                market=_MARKET_LABELS.get(p.market, p.market or ""),
                request_code=request_display_code(p.request_id),
            )
            for p in pending_plans(tenant)
        ],
    )


def _admin_tenant(user, tenant_id):
    if not _user_can_pick_any_tenant(user):
        return None
    return _active_tenant_for_user(user, tenant_id)


@strawberry.type
class JobBoardQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_board(
        self, info: strawberry.Info, tenant_id: strawberry.ID | None = None
    ) -> JobBoardType:
        user = await SparkGraphQLMixin().get_user(info)
        tenant = await sync_to_async(_admin_tenant)(user, tenant_id)
        if tenant is None:
            return JobBoardType(available=False)
        return await sync_to_async(build_admin_board)(tenant)


@strawberry.input
class SetJobBoardGigInput(SparkGraphQLInput):
    tenant_id: strawberry.ID
    gig_id: strawberry.ID
    open_shifts: int | None = None
    listed: bool | None = None


@strawberry.input
class CancelJobBoardBookingInput(SparkGraphQLInput):
    tenant_id: strawberry.ID
    booking_id: strawberry.ID


@strawberry.type
class JobBoardGigResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    gig: JobBoardGigType | None = None


@strawberry.type
class JobBoardCancelResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


def _set_gig(user, input: SetJobBoardGigInput) -> JobBoardGigType:
    tenant = _admin_tenant(user, input.tenant_id)
    if tenant is None or board_for_tenant(tenant) is None:
        raise GraphQLError("Job board isn't available for this brand.")
    gig = next((g for g in list_gigs(tenant) if str(g.event.uuid) == str(input.gig_id)), None)
    if gig is None:
        raise GraphQLError("That gig is no longer upcoming.")
    if input.open_shifts is not None and input.open_shifts < 0:
        raise GraphQLError("Open shifts can't be negative.")
    set_gig(gig, open_shifts=input.open_shifts, listed=input.listed, actor=user)
    bookings = list(
        JobBoardBooking.objects.filter(event=gig.event, cancelled_at__isnull=True).order_by("created_at")
    )
    return _gig_type(gig, bookings)


def _cancel(user, input: CancelJobBoardBookingInput) -> None:
    tenant = _admin_tenant(user, input.tenant_id)
    if tenant is None:
        raise GraphQLError("Job board isn't available for this brand.")
    booking = JobBoardBooking.objects.filter(tenant=tenant, uuid=str(input.booking_id)).first()
    if booking is None:
        raise GraphQLError("Booking not found.")
    try:
        cancel_booking(booking, user)
    except CancelError as exc:
        raise GraphQLError(str(exc)) from exc


@strawberry.type
class JobBoardMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_job_board_gig(
        self, info: strawberry.Info, input: SetJobBoardGigInput
    ) -> JobBoardGigResponse:
        user = await SparkGraphQLMixin().get_user(info)
        gig = await sync_to_async(_set_gig)(user, input)
        return build_mutation_response(
            JobBoardGigResponse, success=True, message="Saved.", input_obj=input, gig=gig
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def cancel_job_board_booking(
        self, info: strawberry.Info, input: CancelJobBoardBookingInput
    ) -> JobBoardCancelResponse:
        user = await SparkGraphQLMixin().get_user(info)
        await sync_to_async(_cancel)(user, input)
        return build_mutation_response(
            JobBoardCancelResponse,
            success=True,
            message="Booking cancelled. The BA was emailed and the spot is open again.",
            input_obj=input,
        )
