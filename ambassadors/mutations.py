import strawberry
from strawberry.types import Info
from events.models import Event
from .models import Ambassador, AmbassadorEvent
from .types import AmbassadorEventType


from utils.graphql.permissions import StrictIsAuthenticated


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