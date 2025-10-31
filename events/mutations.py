import strawberry
from strawberry_django.permissions import IsAuthenticated
from graphql import GraphQLError
from asgiref.sync import sync_to_async

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

from .types import EventDetailResponse
from tenants.models import Tenant
from .models import Event

User = get_user_model()


class EventMutationService:
    async def get_user(self, info: strawberry.Info) -> User:
        """Get the user for the request.

        Args:
            info (strawberry.Info): The info object.

        Returns:
            User: The user for the request.
        """
        # request = info.context["request"]
        # user = getattr(request, "user", None)
        user = info.context.request.user
        if not user or not user.is_authenticated or isinstance(user, AnonymousUser):
            raise GraphQLError(
                "Authentication required. Please provide a valid Auth token.")
        return user

    async def get_tenant(
        self,
        user: User,
        tenant_id: int | None = None
    ) -> Tenant:
        """Get the tenant for the user.

        Args:
            info (strawberry.Info): The info object.
            tenant_id (int | None, optional): The tenant id. Defaults to None.

        Returns:
            Tenant: The tenant for the user.
        """
        tenant = await sync_to_async(user.get_tenant)(tenant_id)
        if not tenant:
            raise GraphQLError(
                f"No active tenant found for user {user.username} with id {tenant_id}.")
        return tenant

    async def create_event(
        self,
        name: str,
        created_by: User,
        tenant: Tenant
    ) -> Event:
        """Create a new event.

        Args:
            name (str): The name of the event.
            created_by (User): The user who created the event.
            tenant (Tenant): The tenant of the event.

        Returns:
            Event: The created event.
        """
        return await sync_to_async(Event.objects.create)(
            name=name,
            tenant=tenant,
            created_by=created_by,
        )

    async def update_event(
        self,
        event_id: strawberry.ID,
        name: str,
        updated_by: User,
    ) -> Event:
        """Update an existing event.

        Args:
            event_id (strawberry.ID): The id of the event.
            name (str): The name of the event.
            updated_by (User): The user who updated the event.

        Returns:
            Event: The updated event.
        """
        event = await sync_to_async(Event.objects.get)(id=event_id)
        event.name = name
        event.updated_by = updated_by
        await sync_to_async(event.save)()
        return event


@strawberry.type
class EventsAmbassadorsMutation:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_event(
        self,
        info: strawberry.Info,
        name: str,
        tenant_id: int | None = None
    ) -> EventDetailResponse:
        """
        Create a new event.
        If tenant_id is not provided, uses the first active tenant for the user.
        """
        try:
            service: EventMutationService = EventMutationService()
            user: User = await service.get_user(info)
            tenant: Tenant = await service.get_tenant(user, tenant_id)
            event: Event = await service.create_event(
                name=name,
                created_by=user,
                tenant=tenant,
            )
            return EventDetailResponse(
                success=True,
                message="Event created successfully.",
                event=event,
            )
        except GraphQLError as e:
            return EventDetailResponse(
                success=False,
                message=str(e),
                event=None,
            )

    @strawberry.mutation
    async def update_event(
        self,
        info: strawberry.Info,
        event_id: strawberry.ID,
        name: str,
    ) -> EventDetailResponse:
        """
        Update an existing event.        
        """
        try:
            service: EventMutationService = EventMutationService()
            user: User = await service.get_user(info)
            event: Event = await service.update_event(
                event_id=event_id,
                name=name,
                updated_by=user,
            )
            return EventDetailResponse(
                success=True,
                message="Event updated successfully.",
                event=event,
            )
        except GraphQLError as e:
            return EventDetailResponse(
                success=False,
                message=str(e),
                event=None,
            )
        except Event.DoesNotExist:
            return EventDetailResponse(
                success=False,
                message="Event not found.",
                event=None,
            )


class EventsSparkMutation(EventsAmbassadorsMutation):
    pass
