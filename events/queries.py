import strawberry
from strawberry_django.permissions import IsAuthenticated
from asgiref.sync import sync_to_async
from typing import List
from graphql import GraphQLError

from django.db.models import QuerySet

from .types import Event
from .models import Event as EventModel
from utils.graphql import SparkGraphQLMixin


class EventQueriesService(SparkGraphQLMixin):
    """Service for event queries."""

    def get_events_queryset(
        self,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
        tenant_id: strawberry.ID | None = None,
    ) -> QuerySet[EventModel]:
        """Get the events queryset."""
        queryset = EventModel.objects.all()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if q:
            queryset = queryset.filter(name__icontains=q)

        # pagination stuff
        queryset = queryset.order_by('-created_at')[offset:offset+limit]
        return queryset

    async def get_event(
        self,
        id: strawberry.ID,
        tenant_id: strawberry.ID | None = None
    ) -> Event | None:
        """Get a single event."""
        try:
            if tenant_id:
                return await sync_to_async(EventModel.objects.get)(id=id, tenant_id=tenant_id)
            return await sync_to_async(EventModel.objects.get)(id=id)
        except EventModel.DoesNotExist:
            raise GraphQLError("Event not found.")


@strawberry.type
class EventAmbassadorsQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def events(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[Event]:
        """Get all events."""
        service = EventQueriesService()
        tenant = await service.get_user_tenant(info)

        @sync_to_async
        def get_events() -> List[Event]:
            queryset = service.get_events_queryset(limit, offset, q, tenant.id)
            return list(queryset)

        return await get_events()

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event(self, info: strawberry.Info, id: strawberry.ID) -> Event | None:
        """Get a single event.
        It limits the events to the tenant of the user. Otherwise, it returns 404 (None)
        """
        try:
            service = EventQueriesService()
            tenant = await service.get_user_tenant(info)
            event = await service.get_event(id, tenant.id)
            return event
        except GraphQLError:
            return None


@strawberry.type
class EventClientQueries(EventAmbassadorsQueries):
    pass


@strawberry.type
class EventSparkQueries:

    @strawberry.field(extensions=[IsAuthenticated()])
    async def events(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        tenant_id: strawberry.ID | None = None,
        q: str | None = None,
    ) -> List[Event]:
        """Get all events."""
        @sync_to_async
        def get_events() -> List[Event]:
            service = EventQueriesService()
            queryset = service.get_events_queryset(limit, offset, q, tenant_id)
            return list(queryset)
        return await get_events()

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event(self, info: strawberry.Info, id: strawberry.ID) -> Event | None:
        """Get a single event.

        It doesn't limit the events to the tenant of the user.
        """
        try:
            service = EventQueriesService()
            event = await service.get_event(id)
            return event
        except GraphQLError:
            return None
