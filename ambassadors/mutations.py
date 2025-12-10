import strawberry
from strawberry import relay
from strawberry.types import Info

from events.models import Event
from utils.graphql.permissions import StrictIsAuthenticated

from .models import Ambassador, AmbassadorEvent
from .types import (
    AmbassadorEventType,
    PublicAmbassadorCreationResponse,
    AmbassadorInvitationResponse,
    AcceptInvitationResponse,
    ApproveAmbassadorResponse,
)
from . import inputs
from .services import (
    PublicAmbassadorCreationService,
    AmbassadorInvitationService,
    AcceptInvitationService,
    ApproveAmbassadorService,
)


@strawberry.type
class ApplyAmbassadorEventResponse:
    success: bool
    message: str
    application: AmbassadorEventType | None = None


@strawberry.type
class AmbassadorMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def apply_ambassador_event(
        self, info: Info, event_id: strawberry.ID
    ) -> ApplyAmbassadorEventResponse:
        user = info.context.request.user
        # Manual check removed as StrictIsAuthenticated handles it

        try:
            ambassador = await Ambassador.objects.aget(user=user)
        except Ambassador.DoesNotExist:
            return ApplyAmbassadorEventResponse(
                success=False, message="Ambassador profile not found"
            )

        try:
            event = await Event.objects.select_related("tenant").aget(id=event_id)
        except Event.DoesNotExist:
            return ApplyAmbassadorEventResponse(
                success=False, message="Event not found"
            )

        if await AmbassadorEvent.objects.filter(
            ambassador=ambassador, event=event
        ).aexists():
            return ApplyAmbassadorEventResponse(
                success=False, message="Already applied to this event"
            )

        application = await AmbassadorEvent.objects.acreate(
            ambassador=ambassador,
            event=event,
            tenant=event.tenant,
            is_approved=False,
            created_by=user,
            updated_by=user,
        )

        return ApplyAmbassadorEventResponse(
            success=True, message="Application successful", application=application
        )

    @relay.mutation  # Public - no permission_classes
    async def create_public_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.CreatePublicAmbassadorInput,
    ) -> PublicAmbassadorCreationResponse:
        return await PublicAmbassadorCreationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_ambassador_invitation(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorInvitationInput,
    ) -> AmbassadorInvitationResponse:
        return await AmbassadorInvitationService.create(input, info)

    @relay.mutation  # Public with token validation
    async def accept_ambassador_invitation(
        self,
        info: strawberry.Info,
        input: inputs.AcceptAmbassadorInvitationInput,
    ) -> AcceptInvitationResponse:
        return await AcceptInvitationService.accept(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.ApproveAmbassadorInput,
    ) -> ApproveAmbassadorResponse:
        return await ApproveAmbassadorService.approve(input, info)
