import datetime
import logging
from typing import List

import strawberry
from enum import Enum
from utils.graphql.permissions import StrictIsAuthenticated
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.db.models import (
    Case,
    DateField,
    DateTimeField,
    DurationField,
    ExpressionWrapper,
    F,
    Func,
    IntegerField,
    Model,
    QuerySet,
    Value,
    When,
)
from django.utils import timezone
from django.db.models.functions import Cast, Coalesce

from events import types
from events import models
from tenants.models import Tenant
from events.inputs import (
    EventFiltersInput,
    EventTypeFiltersInput,
    EventStatusFiltersInput,
    RequestFiltersInput,
    ClientFiltersInput,
    LocationFiltersInput,
    DistributorFiltersInput,
    RetailerFiltersInput,
    RequestTypeFiltersInput,
    RequestStatusFiltersInput,
    ProductTypeFiltersInput,
    ProductFiltersInput,
    RequestStoreManagerFiltersInput,
    DistanceUnit,
)

from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)

logger = logging.getLogger(__name__)


class BaseEventQueriesService(SparkGraphQLMixin):
    """Service for event queries."""

    ordering: tuple[str, ...] = ("-created_at",)

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.all()

    def get_filtered_queryset(
        self, tenant_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        tenant_id: strawberry.ID | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return the filtered queryset with ordering applied."""
        queryset = self.get_filtered_queryset(tenant_id, q)
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        tenant_id: strawberry.ID | None = None,
        q: str | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        default_limit: int = 10,
        max_limit: int = 50,
        ordering: tuple[str, ...] | None = None,
        queryset: QuerySet | None = None,
    ) -> CountableConnection[Model]:
        """Return a Relay compliant connection for the queryset."""
        if queryset is None:
            queryset = self.get_ordered_queryset(tenant_id, q, ordering)
        try:
            return await connection_from_queryset_async(
                queryset,
                first=first,
                after=after,
                last=last,
                before=before,
                default_limit=default_limit,
                max_limit=max_limit,
            )
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc

    async def get_record(
        self,
        id: strawberry.ID | None = None,
        tenant_id: strawberry.ID | None = None,
        uuid: str | None = None,
    ) -> Model | None:
        """Get a single record using id or uuid."""
        filters: dict[str, object] = {}
        if id not in (None, ""):
            filters["id"] = id
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if tenant_id:
            filters["tenant_id"] = tenant_id
        if "id" not in filters and "uuid" not in filters:
            raise GraphQLError("Record not found.")

        try:
            return await sync_to_async(self.get_model().objects.get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")

    async def get_record_by_uuid(
        self, uuid: str, tenant_id: strawberry.ID | None = None
    ) -> Model | None:
        """Get a single record by UUID."""
        return await self.get_record(uuid=uuid, tenant_id=tenant_id)

    def has_unrestricted_tenant_access(self, user) -> bool:
        """Return True when role can query any tenant without membership."""
        return self.get_role_slug(user) in {"spark-admin", "ambassador"}

    async def resolve_tenant_id(
        self,
        info: strawberry.Info,
        *,
        tenant_id: strawberry.ID | None = None,
        tenant_uuid: strawberry.ID | None = None,
    ) -> int | None:
        """Resolve tenant id honoring unrestricted roles and error messaging."""
        user = await self.get_user(info)
        unrestricted = self.has_unrestricted_tenant_access(user)
        has_explicit_tenant = tenant_id is not None or tenant_uuid is not None

        should_filter = not unrestricted or has_explicit_tenant
        if not should_filter:
            return None

        if unrestricted and has_explicit_tenant:
            tenant = await self._get_tenant_without_membership(
                tenant_id=tenant_id,
                tenant_uuid=tenant_uuid,
            )
            return tenant.id

        try:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=tenant_id,
                tenant_uuid=tenant_uuid,
                user=user,
            )
            return tenant.id
        except GraphQLError as exc:
            membership_error = "not a member of this tenant" in str(exc).lower()
            if membership_error and not self.get_role_slug(user) == "client":
                raise GraphQLError("Tenant access denied.") from exc
            raise


class EventQueriesService(BaseEventQueriesService):
    """Service for event queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Event

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.select_related("tenant")


@strawberry.type
class EventQueries:
    @staticmethod
    def _filter_events_for_local_today(queryset: QuerySet) -> QuerySet:
        """Filter events whose local date (based on event/request timezone) matches 'today'."""
        offset_value = Coalesce(
            F("timezone__offset"), F("request__timezone__offset"), Value(0)
        )
        offset_minutes = Case(
            When(timezone__offset__lt=-24, then=F("timezone__offset")),
            When(timezone__offset__gt=24, then=F("timezone__offset")),
            When(request__timezone__offset__lt=-24, then=F("request__timezone__offset")),
            When(request__timezone__offset__gt=24, then=F("request__timezone__offset")),
            default=ExpressionWrapper(
                offset_value * Value(60), output_field=IntegerField()
            ),
            output_field=IntegerField(),
        )
        offset_interval = Func(
            offset_minutes,
            function="MAKE_INTERVAL",
            template="%(function)s(mins => %(expressions)s)",
            output_field=DurationField(),
        )

        event_dt = Coalesce(
            F("date"),
            F("request__date"),
            output_field=DateTimeField(),
        )
        now = timezone.now()
        return queryset.annotate(
            event_local_date=Cast(
                ExpressionWrapper(
                    event_dt + offset_interval,
                    output_field=DateTimeField(),
                ),
                output_field=DateField(),
            ),
            current_local_date=Cast(
                ExpressionWrapper(
                    Value(now) + offset_interval,
                    output_field=DateTimeField(),
                ),
                output_field=DateField(),
            ),
        ).filter(event_local_date=F("current_local_date"))

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def events(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: EventFiltersInput | None = None,
    ) -> CountableConnection[types.Event]:
        """Get all events using Relay pagination."""
        service = EventQueriesService()
        filters_tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        filters_tenant_uuid: strawberry.ID | None = (
            filters.tenant_uuid if filters else None
        )
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=filters_tenant_id,
            tenant_uuid=filters_tenant_uuid,
        )

        queryset = service.get_ordered_queryset(tenant_id=resolved_tenant_id, q=q)

        if filters:
            if filters.event_type_id:
                queryset = queryset.filter(event_type_id=filters.event_type_id)
            if filters.event_status:
                queryset = queryset.filter(status__slug=filters.event_status.value)
            if filters.request_id:
                queryset = queryset.filter(request_id=filters.request_id)
            if filters.date:
                queryset = queryset.filter(request__date=filters.date)

            if filters.coordinates:
                from django.db.models import F
                from django.db.models.functions import ACos, Cos, Radians, Sin

                lat = filters.coordinates.coordinates[0]
                lon = filters.coordinates.coordinates[1]
                range_val = filters.coordinates.range
                unit = filters.coordinates.unit

                # Earth radius: 6371 km or 3959 miles
                earth_radius = 6371 if unit == DistanceUnit.KILOMETERS else 3959

                distance_expr = earth_radius * ACos(
                    Cos(Radians(lat))
                    * Cos(Radians(F("request__coordinates__0")))
                    * Cos(Radians(F("request__coordinates__1")) - Radians(lon))
                    + Sin(Radians(lat)) * Sin(Radians(F("request__coordinates__0")))
                )

                queryset = queryset.annotate(distance=distance_expr).filter(
                    distance__lte=range_val
                )
                queryset = queryset.order_by("distance", "start_time")

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Event | None:
        """Get a single event by id or UUID.
        Spark admins and ambassadors can view any tenant; other roles are limited to their tenant.
        """
        try:
            service = EventQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            event = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
            return event
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def today_events(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: EventFiltersInput | None = None,
    ) -> CountableConnection[types.Event]:
        """Get today's events for the current tenant."""
        service = EventQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        queryset = service.get_filtered_queryset(resolved_tenant_id, q)

        if filters:
            if filters.event_type_id:
                queryset = queryset.filter(event_type_id=filters.event_type_id)
            if filters.event_status:
                queryset = queryset.filter(status__slug=filters.event_status.value)
            if filters.request_id:
                queryset = queryset.filter(request_id=filters.request_id)

        queryset = EventQueries._filter_events_for_local_today(queryset).order_by(
            "start_time"
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def today_events_coordinates(
        self,
        info: strawberry.Info,
        coordinates: List[float],
        range: float,
        unit: DistanceUnit = DistanceUnit.KILOMETERS,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: EventFiltersInput | None = None,
    ) -> CountableConnection[types.Event]:
        """Get today's events within a radius of the coordinates.

        Args:
            coordinates: [latitude, longitude]
            range: Search radius
            unit: Distance unit (km or mi), defaults to kilometers
        """
        from django.db.models import F
        from django.db.models.functions import ACos, Cos, Radians, Sin

        service = EventQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        queryset = service.get_filtered_queryset(resolved_tenant_id, q)

        if filters:
            if filters.event_type_id:
                queryset = queryset.filter(event_type_id=filters.event_type_id)
            if filters.event_status:
                queryset = queryset.filter(status__slug=filters.event_status.value)
            if filters.request_id:
                queryset = queryset.filter(request_id=filters.request_id)

        queryset = EventQueries._filter_events_for_local_today(queryset)

        # Calculate distance
        lat = coordinates[0]
        lon = coordinates[1]

        # Earth radius: 6371 km or 3959 miles
        earth_radius = 6371 if unit == DistanceUnit.KILOMETERS else 3959

        distance_expr = earth_radius * ACos(
            Cos(Radians(lat))
            * Cos(Radians(F("request__coordinates__0")))
            * Cos(Radians(F("request__coordinates__1")) - Radians(lon))
            + Sin(Radians(lat)) * Sin(Radians(F("request__coordinates__0")))
        )

        queryset = queryset.annotate(distance=distance_expr).filter(distance__lte=range)
        queryset = queryset.order_by("distance", "start_time")

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )


class EventTypeQueriesService(BaseEventQueriesService):
    """Service for event type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.EventType


@strawberry.type
class EventTypeQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: EventTypeFiltersInput | None = None,
    ) -> CountableConnection[types.EventType]:
        """Get all event types."""
        service = EventTypeQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_type(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.EventType | None:
        """Get a single event type."""
        try:
            service = EventTypeQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            event_type = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
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
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_statuses(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: EventStatusFiltersInput | None = None,
    ) -> CountableConnection[types.EventStatus]:
        """Get all event statuses."""
        service = EventStatusQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=50,
            max_limit=100,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_status(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.EventStatus | None:
        """Get a single event status."""
        try:
            service = EventStatusQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            event_status = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
            return event_status
        except GraphQLError:
            return None


class RequestQueriesService(BaseEventQueriesService):
    """Service for request queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Request


class RequestStoreManagerQueriesService(BaseEventQueriesService):
    """Service for request store manager queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.RequestStoreManager


@strawberry.type
class RequestQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def requests(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: RequestFiltersInput | None = None,
    ) -> CountableConnection[types.Request]:
        """Get all requests."""
        service = RequestQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        queryset = service.get_ordered_queryset(tenant_id=resolved_tenant_id, q=q)

        if filters:
            if filters.status_id:
                queryset = queryset.filter(status_id=filters.status_id)
            if filters.client_id:
                queryset = queryset.filter(client_id=filters.client_id)
            if filters.retailer_id:
                queryset = queryset.filter(retailer_id=filters.retailer_id)
            if filters.distributor_id:
                queryset = queryset.filter(distributor_id=filters.distributor_id)
            if filters.date:
                queryset = queryset.filter(date=filters.date)

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Request | None:
        """Get a single request."""
        try:
            service = RequestQueriesService()
            tenant_id = await service.resolve_tenant_id(info)

            request = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
            return request
        except GraphQLError:
            raise GraphQLError


@strawberry.type
class RequestStoreManagerQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request_store_managers(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: RequestStoreManagerFiltersInput | None = None,
    ) -> CountableConnection[types.RequestStoreManager]:
        """Get all request store managers."""
        service = RequestStoreManagerQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        queryset = service.get_ordered_queryset(tenant_id=resolved_tenant_id, q=q)

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request_store_manager(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.RequestStoreManager | None:
        """Get a single request store manager."""
        try:
            service = RequestStoreManagerQueriesService()
            tenant_id = await service.resolve_tenant_id(info)

            manager = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
            return manager
        except GraphQLError:
            return None


class ClientQueriesService(BaseEventQueriesService):
    """Service for client queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Client


@strawberry.type
class ClientQueries:
    @strawberry.field
    async def public_clients(
        self,
        info: strawberry.Info,
        request_url_name: str,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Client]:
        """Get public clients filtered by tenant request_url_name."""
        service = ClientQueriesService()
        try:
            tenant = await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            return await service.get_connection(
                queryset=service.get_model().objects.none(),
                first=first,
                after=after,
                last=last,
                before=before,
            )

        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def clients(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: ClientFiltersInput | None = None,
    ) -> CountableConnection[types.Client]:
        """Get all clients."""
        service = ClientQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def client(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Client | None:
        """Get a single client."""
        try:
            service = ClientQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            client = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
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
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def locations(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: LocationFiltersInput | None = None,
    ) -> CountableConnection[types.Location]:
        """Get all locations."""
        service = LocationQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=50,
            max_limit=100,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def location(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Location | None:
        """Get a single location."""
        try:
            service = LocationQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            location = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
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
    @strawberry.field
    async def public_distributors(
        self,
        info: strawberry.Info,
        request_url_name: str,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Distributor]:
        """Get public distributors filtered by tenant request_url_name."""
        service = DistributorQueriesService()
        try:
            tenant = await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            return await service.get_connection(
                queryset=service.get_model().objects.none(),
                first=first,
                after=after,
                last=last,
                before=before,
            )

        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def distributors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: DistributorFiltersInput | None = None,
    ) -> CountableConnection[types.Distributor]:
        """Get all distributors."""
        service = DistributorQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def distributor(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Distributor | None:
        """Get a single distributor."""
        try:
            service = DistributorQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            distributor = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
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
    @strawberry.field
    async def public_retailer(
        self,
        info: strawberry.Info,
        request_url_name: str,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Retailer]:
        """Get public retailers filtered by tenant request_url_name."""
        service = RetailerQueriesService()
        try:
            tenant = await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            return await service.get_connection(
                queryset=service.get_model().objects.none(),
                first=first,
                after=after,
                last=last,
                before=before,
            )

        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def retailers(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: RetailerFiltersInput | None = None,
    ) -> CountableConnection[types.Retailer]:
        """Get all retailers."""
        service = RetailerQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def retailer(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Retailer | None:
        """Get a single retailer."""
        try:
            service = RetailerQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            retailer = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
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
    @strawberry.field
    async def public_request_type(
        self,
        info: strawberry.Info,
        request_url_name: str,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.RequestType]:
        """Get public request types filtered by tenant request_url_name."""
        service = RequestTypeQueriesService()
        try:
            tenant = await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            return await service.get_connection(
                queryset=service.get_model().objects.none(),
                first=first,
                after=after,
                last=last,
                before=before,
            )

        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: RequestTypeFiltersInput | None = None,
    ) -> CountableConnection[types.RequestType]:
        """Get all request types."""
        service = RequestTypeQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request_type(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.RequestType | None:
        """Get a single request type."""
        try:
            service = RequestTypeQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            request_type = await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
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
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request_statuses(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: RequestStatusFiltersInput | None = None,
    ) -> CountableConnection[types.RequestStatus]:
        """Get all request statuses."""
        service = RequestStatusQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=50,
            max_limit=100,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request_status(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.RequestStatus | None:
        """Get a single request status."""
        try:
            service = RequestStatusQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            return await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
        except GraphQLError:
            return None


class ProductTypeQueriesService(BaseEventQueriesService):
    """Service for product type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.ProductType


@strawberry.type
class ProductTypeQueries:
    @strawberry.field
    async def public_product_types(
        self,
        info: strawberry.Info,
        request_url_name: str,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.ProductType]:
        """Get public product types filtered by tenant request_url_name."""
        service = ProductTypeQueriesService()
        try:
            tenant = await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            return await service.get_connection(
                queryset=service.get_model().objects.none(),
                first=first,
                after=after,
                last=last,
                before=before,
            )

        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def product_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: ProductTypeFiltersInput | None = None,
    ) -> CountableConnection[types.ProductType]:
        """Get all product types."""
        service = ProductTypeQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=50,
            max_limit=100,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def product_type(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.ProductType | None:
        """Get a single product type."""
        try:
            service = ProductTypeQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            return await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
        except GraphQLError:
            return None


class ProductQueriesService(BaseEventQueriesService):
    """Service for product queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Product


@strawberry.type
class ProductQueries:
    @strawberry.field
    async def public_products(
        self,
        info: strawberry.Info,
        request_url_name: str,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: ProductFiltersInput | None = None,
    ) -> CountableConnection[types.Product]:
        """Get public products filtered by tenant request_url_name."""
        service = ProductQueriesService()
        try:
            tenant = await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            return await service.get_connection(
                queryset=service.get_model().objects.none(),
                first=first,
                after=after,
                last=last,
                before=before,
            )

        queryset = service.get_ordered_queryset(tenant_id=tenant.id, q=q)
        if filters and filters.product_type_id:
            queryset = queryset.filter(product_type_id=filters.product_type_id)

        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def products(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: ProductFiltersInput | None = None,
    ) -> CountableConnection[types.Product]:
        """Get all products."""
        service = ProductQueriesService()
        tenant_id: strawberry.ID | None = filters.tenant_id if filters else None
        tenant_uuid: strawberry.ID | None = filters.tenant_uuid if filters else None
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        queryset = service.get_ordered_queryset(
            tenant_id=resolved_tenant_id,
            q=q,
        )

        if filters and filters.product_type_id:
            queryset = queryset.filter(product_type_id=filters.product_type_id)

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def product(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Product | None:
        """Get a single product."""
        try:
            service = ProductQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            return await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
        except GraphQLError:
            return None


class TimeZoneQueriesService(BaseEventQueriesService):
    """Service for timezone queries."""

    ordering: tuple[str, ...] = ("name",)

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.TimeZone

    def get_filtered_queryset(
        self, tenant_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset


@strawberry.type
class TimeZoneQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def timezones(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.TimeZone]:
        """Get all timezones."""
        service = TimeZoneQueriesService()
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=100,
        )

    @strawberry.field
    async def public_timezones(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.TimeZone]:
        """Get public timezones."""
        service = TimeZoneQueriesService()
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=100,
        )
