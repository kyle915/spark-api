import strawberry
from strawberry_django.permissions import IsAuthenticated
from asgiref.sync import sync_to_async
from typing import List
from graphql import GraphQLError

from django.db.models import QuerySet
from django.db.models import Model

from .types import Event, EventType, EventStatus
from .models import Event as EventModel
from .models import EventType as EventTypeModel
from .models import EventStatus as EventStatusModel
from utils.graphql.mixins import SparkGraphQLMixin

import logging

logger = logging.getLogger(__name__)


class BaseEventQueriesService(SparkGraphQLMixin):
    """Service for event queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    async def get_records(
        self,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
        tenant_id: strawberry.ID | None = None
    ) -> List[Model]:
        """Get all records."""
        queryset = self.get_model().objects.all()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if q:
            queryset = queryset.filter(name__icontains=q)
        queryset = queryset.order_by('-created_at')[offset:offset+limit]
        print('executing queryset')
        return await sync_to_async(list)(queryset)

    async def get_record(
        self,
        id: strawberry.ID,
        tenant_id: strawberry.ID | None = None
    ) -> Model | None:
        """Get a single record."""
        try:
            if tenant_id:
                return await sync_to_async(self.get_model().objects.get)(id=id, tenant_id=tenant_id)
            return await sync_to_async(self.get_model().objects.get)(id=id)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")


class EventQueriesService(BaseEventQueriesService):
    """Service for event queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return EventModel


class EventTypeQueriesService(BaseEventQueriesService):
    """Service for event type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return EventTypeModel


class EventStatusQueriesService(BaseEventQueriesService):
    """Service for event status queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return EventStatusModel


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
        logger.info('executing events query')
        service = EventQueriesService()
        tenant = await service.get_user_tenant(info)

        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event(self, info: strawberry.Info, id: strawberry.ID) -> Event | None:
        """Get a single event.
        It limits the events to the tenant of the user. Otherwise, it returns 404 (None)
        """
        try:
            service = EventQueriesService()
            tenant = await service.get_user_tenant(info)
            event = await service.get_record(id, tenant.id)
            return event
        except GraphQLError:
            return None

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_types(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[EventType]:
        """Get all event types."""
        service = EventTypeQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_type(self, info: strawberry.Info, id: strawberry.ID) -> EventType | None:
        """Get a single event type."""
        try:
            service = EventTypeQueriesService()
            tenant = await service.get_user_tenant(info)
            event_type = await service.get_record(id, tenant.id)
            return event_type
        except GraphQLError:
            return None

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_statuses(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[EventStatus]:
        """Get all event statuses."""
        service = EventStatusQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_status(self, info: strawberry.Info, id: strawberry.ID) -> EventStatus | None:
        """Get a single event status."""
        try:
            service = EventStatusQueriesService()
            tenant = await service.get_user_tenant(info)
            event_status = await service.get_record(id, tenant.id)
            return event_status
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
        service = EventQueriesService()
        return await service.get_records(limit, offset, q, tenant_id)

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

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_types(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[EventType]:
        """Get all event types."""
        service = EventTypeQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_type(self, info: strawberry.Info, id: strawberry.ID) -> EventType | None:
        """Get a single event type."""
        try:
            service = EventTypeQueriesService()
            tenant = await service.get_user_tenant(info)
            event_type = await service.get_record(id, tenant.id)
            return event_type
        except GraphQLError:
            return None

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_statuses(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[EventStatus]:
        """Get all event statuses."""
        service = EventStatusQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_status(self, info: strawberry.Info, id: strawberry.ID) -> EventStatus | None:
        """Get a single event status."""
        try:
            service = EventStatusQueriesService()
            tenant = await service.get_user_tenant(info)
            event_status = await service.get_record(id, tenant.id)
            return event_status
        except GraphQLError:
            return None
