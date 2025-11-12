import datetime
import strawberry
from strawberry_django.permissions import IsAuthenticated
from asgiref.sync import sync_to_async
from typing import List
from graphql import GraphQLError

from django.db.models import QuerySet
from django.db.models import Model

from events import types
from events import models

from utils.graphql.mixins import SparkGraphQLMixin

import logging

logger = logging.getLogger(__name__)


class BaseEventQueriesService(SparkGraphQLMixin):
    """Service for event queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.all()

    def get_filtered_queryset(
        self,
        tenant_id: int | None = None,
        q: str | None = None
    ) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    async def get_records(
        self,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
        tenant_id: strawberry.ID | None = None
    ) -> List[Model]:
        """Get all records."""
        queryset = self.get_filtered_queryset(tenant_id, q)
        queryset = queryset.order_by('-created_at')[offset:offset+limit]
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
        return models.Event


@strawberry.type
class EventQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def events(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[types.Event]:
        """Get all events."""
        service = EventQueriesService()
        tenant = await service.get_user_tenant(info)

        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event(self, info: strawberry.Info, id: strawberry.ID) -> types.Event | None:
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
    async def today_events(
        self,
        info: strawberry.Info,
        q: str | None = None,
    ) -> List[types.Event]:
        """Get all events for today."""
        service = EventQueriesService()
        tenant = await service.get_user_tenant(info)
        queryset = service.get_filtered_queryset(tenant.id, q)
        queryset = queryset.filter(start_time__day=datetime.date.today().day)
        queryset = queryset.order_by('start_time')

        return await sync_to_async(list)(queryset)


class EventTypeQueriesService(BaseEventQueriesService):
    """Service for event type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.EventType


@strawberry.type
class EventTypeQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_types(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[types.EventType]:
        """Get all event types."""
        service = EventTypeQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_type(self, info: strawberry.Info, id: strawberry.ID) -> types.EventType | None:
        """Get a single event type."""
        try:
            service = EventTypeQueriesService()
            tenant = await service.get_user_tenant(info)
            event_type = await service.get_record(id, tenant.id)
            return event_type
        except GraphQLError:
            return None


class EventStatusQueriesService(BaseEventQueriesService):
    """Service for event status queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.EventStatus


@strawberry.type
class EventStatusQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_statuses(
        self,
        info: strawberry.Info,
    ) -> List[types.EventStatus]:
        """Get all event statuses."""
        service = EventStatusQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit=50, offset=0, q=None, tenant_id=tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event_status(self, info: strawberry.Info, id: strawberry.ID) -> types.EventStatus | None:
        """Get a single event status."""
        try:
            service = EventStatusQueriesService()
            tenant = await service.get_user_tenant(info)
            event_status = await service.get_record(id, tenant.id)
            return event_status
        except GraphQLError:
            return None


class RequestQueriesService(BaseEventQueriesService):
    """Service for request queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Request


@strawberry.type
class RequestQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def requests(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[types.Request]:
        """Get all requests."""
        service = RequestQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def request(self, info: strawberry.Info, id: strawberry.ID) -> types.Request | None:
        """Get a single request."""
        try:
            service = RequestQueriesService()
            tenant = await service.get_user_tenant(info)
            request = await service.get_record(id, tenant.id)
            return request
        except GraphQLError:
            return None


class ClientQueriesService(BaseEventQueriesService):
    """Service for client queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Client


@strawberry.type
class ClientQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def clients(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[types.Client]:
        """Get all clients."""
        service = ClientQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def client(self, info: strawberry.Info, id: strawberry.ID) -> types.Client | None:
        """Get a single client."""
        try:
            service = ClientQueriesService()
            tenant = await service.get_user_tenant(info)
            client = await service.get_record(id, tenant.id)
            return client
        except GraphQLError:
            return None


class LocationQueriesService(BaseEventQueriesService):
    """Service for location queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Location


@strawberry.type
class LocationQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def locations(self, info: strawberry.Info) -> List[types.Location]:
        """Get all locations."""
        service = LocationQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit=50, offset=0, q=None, tenant_id=tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def location(self, info: strawberry.Info, id: strawberry.ID) -> types.Location | None:
        """Get a single location."""
        try:
            service = LocationQueriesService()
            tenant = await service.get_user_tenant(info)
            location = await service.get_record(id, tenant.id)
            return location
        except GraphQLError:
            return None


class DistributorQueriesService(BaseEventQueriesService):
    """Service for distributor queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Distributor


@strawberry.type
class DistributorQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def distributors(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[types.Distributor]:
        """Get all distributors."""
        service = DistributorQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def distributor(self, info: strawberry.Info, id: strawberry.ID) -> types.Distributor | None:
        """Get a single distributor."""
        try:
            service = DistributorQueriesService()
            tenant = await service.get_user_tenant(info)
            distributor = await service.get_record(id, tenant.id)
            return distributor
        except GraphQLError:
            return None


class RetailerQueriesService(BaseEventQueriesService):
    """Service for retailer queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Retailer


@strawberry.type
class RetailerQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def retailers(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[types.Retailer]:
        """Get all retailers."""
        service = RetailerQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def retailer(self, info: strawberry.Info, id: strawberry.ID) -> types.Retailer | None:
        """Get a single retailer."""
        try:
            service = RetailerQueriesService()
            tenant = await service.get_user_tenant(info)
            retailer = await service.get_record(id, tenant.id)
            return retailer
        except GraphQLError:
            return None


class RequestTypeQueriesService(BaseEventQueriesService):
    """Service for request type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.RequestType


@strawberry.type
class RequestTypeQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def request_types(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[types.RequestType]:
        """Get all request types."""
        service = RequestTypeQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def request_type(self, info: strawberry.Info, id: strawberry.ID) -> types.RequestType | None:
        """Get a single request type."""
        try:
            service = RequestTypeQueriesService()
            tenant = await service.get_user_tenant(info)
            request_type = await service.get_record(id, tenant.id)
            return request_type
        except GraphQLError:
            return None


class RequestStatusQueriesService(BaseEventQueriesService):
    """Service for request status queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.RequestStatus


@strawberry.type
class RequestStatusQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def request_statuses(self, info: strawberry.Info) -> List[types.RequestStatus]:
        """Get all request statuses."""
        service = RequestStatusQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit=50, offset=0, q=None, tenant_id=tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def request_status(self, info: strawberry.Info, id: strawberry.ID) -> types.RequestStatus | None:
        """Get a single request status."""
        try:
            service = RequestStatusQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


class ProductTypeQueriesService(BaseEventQueriesService):
    """Service for product type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.ProductType


@strawberry.type
class ProductTypeQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def product_types(self, info: strawberry.Info) -> List[types.ProductType]:
        """Get all product types."""
        service = ProductTypeQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit=50, offset=0, q=None, tenant_id=tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def product_type(self, info: strawberry.Info, id: strawberry.ID) -> types.ProductType | None:
        """Get a single product type."""
        try:
            service = ProductTypeQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


class ProductQueriesService(BaseEventQueriesService):
    """Service for product queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Product


@strawberry.type
class ProductQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def products(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        q: str | None = None,
    ) -> List[types.Product]:
        """Get all products."""
        service = ProductQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_records(limit, offset, q, tenant.id)

    @strawberry.field(extensions=[IsAuthenticated()])
    async def product(self, info: strawberry.Info, id: strawberry.ID) -> types.Product | None:
        """Get a single product."""
        try:
            service = ProductQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None
