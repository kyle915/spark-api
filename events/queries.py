import datetime
import logging
from datetime import timedelta
from typing import Annotated, List

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
    Q,
    QuerySet,
    Value,
    When,
)
from django.utils import timezone
from django.db.models.functions import Cast, Coalesce

from events import types
from events import models
from tenants.models import Tenant, TenantedUser, Role
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
    BillingEntityFiltersInput,
    RequestStatusFiltersInput,
    ProductTypeFiltersInput,
    ProductFiltersInput,
    RequestStoreManagerFiltersInput,
    DistanceUnit,
)

from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)

logger = logging.getLogger(__name__)


def _resolve_filter_id(value: strawberry.ID | None, label: str) -> int | None:
    """Resolve relay/global IDs used in filters to database IDs."""
    if value in (None, ""):
        return None
    try:
        return resolve_id_to_int(value)
    except (TypeError, ValueError, GraphQLError) as exc:
        raise GraphQLError(f"Invalid {label} ID.") from exc


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
        default_limit: int = 100,
        max_limit: int = 100,
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
            try:
                filters["id"] = resolve_id_to_int(id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid ID.") from exc
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if tenant_id:
            filters["tenant_id"] = tenant_id
        if "id" not in filters and "uuid" not in filters:
            raise GraphQLError("Record not found.")

        try:
            return await sync_to_async(self.get_queryset().get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")

    async def get_record_by_uuid(
        self, uuid: str, tenant_id: strawberry.ID | None = None
    ) -> Model | None:
        """Get a single record by UUID."""
        return await self.get_record(uuid=uuid, tenant_id=tenant_id)

    def has_unrestricted_tenant_access(self, user) -> bool:
        """Return True when role can query any tenant without membership.

        Staff / superuser ALSO bypass — Kyle reported a blank
        /request/view/<uuid> page when the request belonged to a
        tenant he wasn't currently switched to. Same cross-tenant
        deep-link pattern as the staff-bypass we shipped on the
        tenants resolver (PR #531). Without this, a platform owner
        clicking a request URL from email or Slack lands on
        "Request not found" unless they happen to be on the right
        tenant first.
        """
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return True
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
        resolved_tenant_id: int | None = None

        if tenant_id is not None:
            try:
                resolved_tenant_id = resolve_id_to_int(tenant_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid tenant ID.") from exc

        should_filter = not unrestricted or has_explicit_tenant
        if not should_filter:
            return None

        if unrestricted and has_explicit_tenant:
            tenant = await self._get_tenant_without_membership(
                tenant_id=resolved_tenant_id,
                tenant_uuid=tenant_uuid,
            )
            return tenant.id

        try:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=resolved_tenant_id,
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
        return self.get_model().objects.select_related(
            "tenant",
            "timezone",
            "request",
            "request__location",
            "request__state",
            "custom_recap_template",
            "retailer",
            "location",
            "state",
            "rmm_asigned",
        )


@strawberry.type
class EventPnlRow:
    """One event's cost picture — labor from clock pairs × booked rate
    (scheduled-duration fallback flags `estimated`), spend from expense
    receipts. `missingRates` counts BAs with no booked rate so a low
    number is visibly incomplete rather than silently wrong."""
    event_id: strawberry.ID
    uuid: str
    name: str
    date: str | None
    ba_count: int
    hours: float
    labor_cost: float
    spend: float
    total_cost: float
    estimated: bool
    missing_rates: int


@strawberry.type
class DuplicateEventEntry:
    """One event inside a duplicate cluster — enough context to pick
    the keeper (the row with the recaps/roster usually wins)."""
    id: strawberry.ID
    uuid: str
    name: str
    date: str | None
    address: str
    recaps_filed: int
    roster_count: int


@strawberry.type
class DuplicateEventCluster:
    key: str
    events: List[DuplicateEventEntry]


@strawberry.type
class EventQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_pnl(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str,
        end_date: str,
    ) -> List[EventPnlRow]:
        """Per-event labor + spend roll-up for a date range. Admin-only;
        clients get [] (cost data is Ignite-internal)."""
        from datetime import date as _date

        from utils.graphql.permissions import (
            _is_admin_access,
            resolve_request_user_access,
        )

        user = info.context.request.user
        role_slug, is_staff, is_super, email = (
            await resolve_request_user_access(user)
        )
        if not _is_admin_access(role_slug, is_staff, is_super, email):
            return []
        try:
            tid = resolve_id_to_int(str(tenant_id))
            start = _date.fromisoformat(str(start_date))
            end = _date.fromisoformat(str(end_date))
        except Exception:  # noqa: BLE001
            return []

        from events.pnl import event_pnl_rows

        rows = await sync_to_async(event_pnl_rows)(tid, start, end)
        return [
            EventPnlRow(
                event_id=strawberry.ID(str(r["event_id"])),
                uuid=r["uuid"],
                name=r["name"],
                date=r["date"],
                ba_count=r["ba_count"],
                hours=r["hours"],
                labor_cost=r["labor_cost"],
                spend=r["spend"],
                total_cost=r["total_cost"],
                estimated=r["estimated"],
                missing_rates=r["missing_rates"],
            )
            for r in rows
        ]

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def duplicate_event_clusters(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> List[DuplicateEventCluster]:
        """Same-name events on the same/adjacent dates for one tenant —
        the bulk-upload double-run signature. Admin-only; clients get []
        (this is an Ignite cleanup tool, not a client surface)."""
        from utils.graphql.permissions import (
            _is_admin_access,
            resolve_request_user_access,
        )

        user = info.context.request.user
        role_slug, is_staff, is_super, email = (
            await resolve_request_user_access(user)
        )
        if not _is_admin_access(role_slug, is_staff, is_super, email):
            return []
        try:
            tid = resolve_id_to_int(str(tenant_id))
        except Exception:  # noqa: BLE001
            return []

        from events.dedupe import find_duplicate_clusters

        clusters = await sync_to_async(find_duplicate_clusters)(tid)
        return [
            DuplicateEventCluster(
                key=c["key"],
                events=[
                    DuplicateEventEntry(
                        id=strawberry.ID(str(e["id"])),
                        uuid=e["uuid"],
                        name=e["name"],
                        date=e["date"],
                        address=e["address"],
                        recaps_filed=e["recaps_filed"],
                        roster_count=e["roster_count"],
                    )
                    for e in c["events"]
                ],
            )
            for c in clusters
        ]

    @staticmethod
    async def _apply_rmm_asigned_filter(
        queryset: QuerySet, rmm_asigned: strawberry.ID
    ) -> QuerySet:
        """Filter events by rmm assignment or notification-group reach for the given user."""
        user_id = _resolve_filter_id(rmm_asigned, "rmm asigned")
        user_group_ids = await sync_to_async(list)(
            models.NotificationGroupUser.objects.filter(user_id=user_id)
            .values_list("notification_group_id", flat=True)
            .distinct()
        )

        queryset = queryset.filter(
            tenant__tenanted_users__user_id=user_id,
            tenant__tenanted_users__is_active=True,
            tenant__tenanted_users__user__is_active=True,
        )

        rmm_filter = Q(rmm_asigned_id=user_id) | Q(request__rmm_asigned_id=user_id)
        if not user_group_ids:
            return queryset.filter(rmm_filter).distinct()

        notification_group_filter = (
            Q(
                retailer__location__notification_group_location__notification_group_id__in=user_group_ids,
                retailer__location__notification_group_location__notification_group__state=False,
            )
            | Q(
                distributor__location__notification_group_location__notification_group_id__in=user_group_ids,
                distributor__location__notification_group_location__notification_group__state=False,
            )
            | Q(
                request__retailer__location__notification_group_location__notification_group_id__in=user_group_ids,
                request__retailer__location__notification_group_location__notification_group__state=False,
            )
            | Q(
                request__distributor__location__notification_group_location__notification_group_id__in=user_group_ids,
                request__distributor__location__notification_group_location__notification_group__state=False,
            )
            | Q(
                retailer__location__state__notification_group_location__notification_group_id__in=user_group_ids,
                retailer__location__state__notification_group_location__notification_group__state=True,
            )
            | Q(
                distributor__location__state__notification_group_location__notification_group_id__in=user_group_ids,
                distributor__location__state__notification_group_location__notification_group__state=True,
            )
            | Q(
                request__retailer__location__state__notification_group_location__notification_group_id__in=user_group_ids,
                request__retailer__location__state__notification_group_location__notification_group__state=True,
            )
            | Q(
                request__distributor__location__state__notification_group_location__notification_group_id__in=user_group_ids,
                request__distributor__location__state__notification_group_location__notification_group__state=True,
            )
        )

        return queryset.filter(rmm_filter | notification_group_filter).distinct()

    @staticmethod
    def _filter_events_for_local_today(queryset: QuerySet) -> QuerySet:
        """Filter events whose local date (based on event/request timezone) matches 'today'."""
        offset_value = Coalesce(
            F("timezone__offset"), F("request__timezone__offset"), Value(0)
        )
        offset_minutes = Case(
            When(timezone__offset__lt=-24, then=F("timezone__offset")),
            When(timezone__offset__gt=24, then=F("timezone__offset")),
            When(
                request__timezone__offset__lt=-24, then=F("request__timezone__offset")
            ),
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

    @staticmethod
    def _apply_event_date_filters(
        queryset: QuerySet, filters: EventFiltersInput
    ) -> QuerySet:
        """Apply date filters to events, supporting exact date and date ranges."""
        if filters.date:
            return queryset.filter(date__date=filters.date)
        if filters.start_date:
            queryset = queryset.filter(date__date__gte=filters.start_date)
        if filters.end_date:
            queryset = queryset.filter(date__date__lte=filters.end_date)
        return queryset

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
            if filters.rmm_asigned:
                queryset = await EventQueries._apply_rmm_asigned_filter(
                    queryset, filters.rmm_asigned
                )
            if filters.event_type_id:
                event_type_id = _resolve_filter_id(filters.event_type_id, "event type")
                queryset = queryset.filter(event_type_id=event_type_id)
            if filters.event_status:
                queryset = queryset.filter(status__slug=filters.event_status.value)
            if filters.request_id:
                request_id = _resolve_filter_id(filters.request_id, "request")
                queryset = queryset.filter(request_id=request_id)
            if filters.custom_recap_template_id:
                custom_recap_template_id = _resolve_filter_id(
                    filters.custom_recap_template_id, "custom recap template"
                )
                queryset = queryset.filter(
                    custom_recap_template_id=custom_recap_template_id
                )
            if filters.retailer_id:
                retailer_id = _resolve_filter_id(filters.retailer_id, "retailer")
                queryset = queryset.filter(
                    Q(retailer_id=retailer_id) | Q(request__retailer_id=retailer_id)
                )
            if filters.distributor_id:
                distributor_id = _resolve_filter_id(
                    filters.distributor_id, "distributor"
                )
                queryset = queryset.filter(
                    Q(distributor_id=distributor_id)
                    | Q(request__distributor_id=distributor_id)
                )
            if filters.location_id:
                location_id = _resolve_filter_id(filters.location_id, "location")
                queryset = queryset.filter(
                    Q(location_id=location_id) | Q(request__location_id=location_id)
                )
            if filters.state_id:
                state_id = _resolve_filter_id(filters.state_id, "state")
                queryset = queryset.filter(
                    Q(state_id=state_id) | Q(request__state_id=state_id)
                )
            if filters.retailer_state_id:
                retailer_state_id = _resolve_filter_id(
                    filters.retailer_state_id, "retailer state"
                )
                queryset = queryset.filter(
                    Q(retailer__location__state_id=retailer_state_id)
                    | Q(request__retailer__location__state_id=retailer_state_id)
                )
            if filters.distributor_state_id:
                distributor_state_id = _resolve_filter_id(
                    filters.distributor_state_id, "distributor state"
                )
                queryset = queryset.filter(
                    Q(distributor__location__state_id=distributor_state_id)
                    | Q(request__distributor__location__state_id=distributor_state_id)
                )
            queryset = EventQueries._apply_event_date_filters(queryset, filters)
            if filters.edited is not None:
                queryset = queryset.filter(updated_by__isnull=not filters.edited)

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
            else:
                queryset = queryset.order_by("-date")
        else:
            queryset = queryset.order_by("-date")

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
            if filters.rmm_asigned:
                queryset = await EventQueries._apply_rmm_asigned_filter(
                    queryset, filters.rmm_asigned
                )
            if filters.event_type_id:
                event_type_id = _resolve_filter_id(filters.event_type_id, "event type")
                queryset = queryset.filter(event_type_id=event_type_id)
            if filters.event_status:
                queryset = queryset.filter(status__slug=filters.event_status.value)
            if filters.request_id:
                request_id = _resolve_filter_id(filters.request_id, "request")
                queryset = queryset.filter(request_id=request_id)
            if filters.custom_recap_template_id:
                custom_recap_template_id = _resolve_filter_id(
                    filters.custom_recap_template_id, "custom recap template"
                )
                queryset = queryset.filter(
                    custom_recap_template_id=custom_recap_template_id
                )
            if filters.retailer_id:
                retailer_id = _resolve_filter_id(filters.retailer_id, "retailer")
                queryset = queryset.filter(
                    Q(retailer_id=retailer_id) | Q(request__retailer_id=retailer_id)
                )
            if filters.distributor_id:
                distributor_id = _resolve_filter_id(
                    filters.distributor_id, "distributor"
                )
                queryset = queryset.filter(
                    Q(distributor_id=distributor_id)
                    | Q(request__distributor_id=distributor_id)
                )
            if filters.location_id:
                location_id = _resolve_filter_id(filters.location_id, "location")
                queryset = queryset.filter(
                    Q(location_id=location_id) | Q(request__location_id=location_id)
                )
            if filters.state_id:
                state_id = _resolve_filter_id(filters.state_id, "state")
                queryset = queryset.filter(
                    Q(state_id=state_id) | Q(request__state_id=state_id)
                )
            if filters.retailer_state_id:
                retailer_state_id = _resolve_filter_id(
                    filters.retailer_state_id, "retailer state"
                )
                queryset = queryset.filter(
                    Q(retailer__location__state_id=retailer_state_id)
                    | Q(request__retailer__location__state_id=retailer_state_id)
                )
            if filters.distributor_state_id:
                distributor_state_id = _resolve_filter_id(
                    filters.distributor_state_id, "distributor state"
                )
                queryset = queryset.filter(
                    Q(distributor__location__state_id=distributor_state_id)
                    | Q(request__distributor__location__state_id=distributor_state_id)
                )
            queryset = EventQueries._apply_event_date_filters(queryset, filters)
            if filters.edited is not None:
                queryset = queryset.filter(updated_by__isnull=not filters.edited)

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
            if filters.rmm_asigned:
                queryset = await EventQueries._apply_rmm_asigned_filter(
                    queryset, filters.rmm_asigned
                )
            if filters.event_type_id:
                event_type_id = _resolve_filter_id(filters.event_type_id, "event type")
                queryset = queryset.filter(event_type_id=event_type_id)
            if filters.event_status:
                queryset = queryset.filter(status__slug=filters.event_status.value)
            if filters.request_id:
                request_id = _resolve_filter_id(filters.request_id, "request")
                queryset = queryset.filter(request_id=request_id)
            if filters.custom_recap_template_id:
                custom_recap_template_id = _resolve_filter_id(
                    filters.custom_recap_template_id, "custom recap template"
                )
                queryset = queryset.filter(
                    custom_recap_template_id=custom_recap_template_id
                )
            if filters.retailer_id:
                retailer_id = _resolve_filter_id(filters.retailer_id, "retailer")
                queryset = queryset.filter(
                    Q(retailer_id=retailer_id) | Q(request__retailer_id=retailer_id)
                )
            if filters.distributor_id:
                distributor_id = _resolve_filter_id(
                    filters.distributor_id, "distributor"
                )
                queryset = queryset.filter(
                    Q(distributor_id=distributor_id)
                    | Q(request__distributor_id=distributor_id)
                )
            if filters.location_id:
                location_id = _resolve_filter_id(filters.location_id, "location")
                queryset = queryset.filter(
                    Q(location_id=location_id) | Q(request__location_id=location_id)
                )
            if filters.state_id:
                state_id = _resolve_filter_id(filters.state_id, "state")
                queryset = queryset.filter(
                    Q(state_id=state_id) | Q(request__state_id=state_id)
                )
            if filters.retailer_state_id:
                retailer_state_id = _resolve_filter_id(
                    filters.retailer_state_id, "retailer state"
                )
                queryset = queryset.filter(
                    Q(retailer__location__state_id=retailer_state_id)
                    | Q(request__retailer__location__state_id=retailer_state_id)
                )
            if filters.distributor_state_id:
                distributor_state_id = _resolve_filter_id(
                    filters.distributor_state_id, "distributor state"
                )
                queryset = queryset.filter(
                    Q(distributor__location__state_id=distributor_state_id)
                    | Q(request__distributor__location__state_id=distributor_state_id)
                )
            queryset = EventQueries._apply_event_date_filters(queryset, filters)
            if filters.edited is not None:
                queryset = queryset.filter(updated_by__isnull=not filters.edited)

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

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_location_trail(
        self,
        info: strawberry.Info,
        event_uuid: strawberry.ID,
    ) -> List[Annotated["LocationPingType", strawberry.lazy("ambassadors.types")]]:
        """All GPS pings for an event, oldest first.

        Powers the "shift replay" panel on /request/view and the
        Event detail page — admins can scrub through the BA's path
        during the activation to audit on-site presence + dispute
        resolution. Returns up to 1000 points; further capping is
        future work (resample at ~50m for very long shifts).
        """
        from ambassadors.models import LocationPing as LocationPingModel
        from ambassadors.types import LocationPingType
        from events.models import Event as EventModel

        service = EventQueriesService()
        tenant_id = await service.resolve_tenant_id(info)

        def _fetch() -> List:
            event = (
                EventModel.objects.filter(uuid=str(event_uuid))
                .only("id", "tenant_id")
                .first()
            )
            if not event:
                return []
            # Tenant scoping — never leak another tenant's BA path.
            if tenant_id and event.tenant_id != tenant_id:
                # Spark-admin gets cross-tenant access via the
                # standard mixin; if resolve_tenant_id returned a
                # value it means the request is tenant-bound.
                return []
            qs = (
                LocationPingModel.objects.filter(event_id=event.id)
                .select_related("ambassador", "ambassador__user", "event")
                .order_by("recorded_at")[:1000]
            )
            out: List = []
            for p in qs:
                name = (
                    f"{(p.ambassador.user.first_name or '').strip()} "
                    f"{(p.ambassador.user.last_name or '').strip()}"
                ).strip() or (p.ambassador.user.email or "(BA)")
                out.append(
                    LocationPingType(
                        uuid=strawberry.ID(str(p.uuid)),
                        lat=p.lat,
                        lng=p.lng,
                        accuracy_meters=p.accuracy_meters,
                        recorded_at=p.recorded_at.isoformat(),
                        source=p.source,
                        ambassador_uuid=strawberry.ID(str(p.ambassador.uuid)),
                        ambassador_name=name,
                        event_uuid=strawberry.ID(str(p.event.uuid)),
                        event_name=p.event.name or "(event)",
                    )
                )
            return out

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recent_location_pings(
        self,
        info: strawberry.Info,
        within_minutes: int = 30,
        tenant_id: strawberry.ID | None = None,
    ) -> List[Annotated["LocationPingType", strawberry.lazy("ambassadors.types")]]:
        """Latest GPS ping per Ambassador within the last N minutes.

        Powers the "Today, on the ground" admin map. Returns one row
        per Ambassador (the freshest ping), filtered to today's events
        in the requested tenant. Older pings get superseded by newer
        ones so the map shows the BA's current location, not a trail.

        within_minutes defaults to 30 (recent enough to be "live" given
        the mobile pinger fires every ~2 min). Bumping to 60+ lets ops
        see slightly-stale pings during connectivity dropouts.
        """
        from ambassadors.models import LocationPing as LocationPingModel
        from ambassadors.types import LocationPingType
        from django.db.models import Max

        service = EventQueriesService()
        resolved_tenant_id = await service.resolve_tenant_id(
            info, tenant_id=tenant_id
        )

        cutoff = timezone.now() - timedelta(minutes=within_minutes)

        def _fetch() -> List:
            qs = (
                LocationPingModel.objects.filter(
                    recorded_at__gte=cutoff,
                    event__tenant_id=resolved_tenant_id,
                )
                .select_related(
                    "ambassador",
                    "ambassador__user",
                    "event",
                )
                .order_by("ambassador_id", "-recorded_at")
            )
            # Collapse to latest-per-ambassador in Python rather than
            # PostgreSQL's DISTINCT ON, so the query plan stays portable.
            latest_per_ba: dict[int, LocationPingModel] = {}
            for p in qs:
                if p.ambassador_id not in latest_per_ba:
                    latest_per_ba[p.ambassador_id] = p
            out: List = []
            for p in latest_per_ba.values():
                name = (
                    f"{(p.ambassador.user.first_name or '').strip()} "
                    f"{(p.ambassador.user.last_name or '').strip()}"
                ).strip() or (p.ambassador.user.email or "(BA)")
                out.append(
                    LocationPingType(
                        uuid=strawberry.ID(str(p.uuid)),
                        lat=p.lat,
                        lng=p.lng,
                        accuracy_meters=p.accuracy_meters,
                        recorded_at=p.recorded_at.isoformat(),
                        source=p.source,
                        ambassador_uuid=strawberry.ID(str(p.ambassador.uuid)),
                        ambassador_name=name,
                        event_uuid=strawberry.ID(str(p.event.uuid)),
                        event_name=p.event.name or "(event)",
                    )
                )
            return out

        return await sync_to_async(_fetch, thread_sensitive=True)()


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

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service.

        Excludes soft-deleted requests (deleted_at IS NOT NULL) so the
        deleteRequest mutation effectively hides the row from every
        list, detail, single-fetch, and export path that flows through
        this base queryset. The row stays in the DB so its activity log
        + linked events / recaps survive intact.
        """
        return (
            self.get_model()
            .objects.filter(deleted_at__isnull=True)
            .select_related(
                "tenant",
                "timezone",
                "billing_entity__state",
                "distributor__location__state",
                "retailer__location__state",
                "location",
                "state",
                "rmm_asigned",
                "created_by",
                "updated_by",
            )
            .prefetch_related(
                "requests_stores_manager",
                "request_product__product",
                "event_set",
                "event_set__tenant",
                # Master Tracker RECAP chip + /request/view Field
                # Reports panel both traverse Request → events → recaps.
                # Without this prefetch each row would trigger a
                # separate `Event.objects.filter(...)` *and* a
                # `Recap.objects.filter(...)` query (N+1×2 per page).
                # Limit to id-only fields on Recap since neither
                # consumer needs the full recap detail at list time.
                "event_set__recaps",
                # Custom-template recaps live in a separate table. The
                # Master Tracker RECAP chip counts an event as "filed" when
                # EITHER recaps OR custom_recap is non-empty (same rule as
                # /recaps/missing). Prefetch this too so Event.customRecaps
                # reads the cache instead of an N+1 per row — and so a filed
                # custom recap actually clears the DUE chip.
                "event_set__custom_recap",
                # Master Tracker "BA assigned" indicator traverses
                # Request → events → ambassadors_events to count
                # assigned/confirmed BAs per event. Without this prefetch
                # Event.assignedAmbassadorsCount /
                # confirmedAmbassadorsCount would each fire a COUNT query
                # per event (N+1). The count resolvers read from this
                # prefetched list when present.
                "event_set__ambassadors_events",
                # Admin shift-swap visibility: Request.openShifts traverses
                # Request → events → open_shifts (+ who dropped / claimed).
                # Prefetch so the Master Tracker chip + Request View panel read
                # from cache instead of an N+1 per row.
                "event_set__open_shifts",
                "event_set__open_shifts__released_by",
                "event_set__open_shifts__claimed_by",
            )
        )


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
            if filters.rmm_asigned:
                user_id = _resolve_filter_id(filters.rmm_asigned, "rmm asigned")
                user_group_ids = await sync_to_async(list)(
                    models.NotificationGroupUser.objects.filter(
                        user_id=user_id
                    )
                    .values_list("notification_group_id", flat=True)
                    .distinct()
                )
                if not user_group_ids:
                    queryset = queryset.filter(
                        tenant__tenanted_users__user_id=user_id,
                        tenant__tenanted_users__is_active=True,
                        tenant__tenanted_users__user__is_active=True,
                        rmm_asigned_id=user_id,
                    )
                else:
                    queryset = queryset.filter(
                        tenant__tenanted_users__user_id=user_id,
                        tenant__tenanted_users__is_active=True,
                        tenant__tenanted_users__user__is_active=True,
                    ).filter(
                        Q(rmm_asigned_id=user_id)
                        |
                        Q(
                            retailer__location__notification_group_location__notification_group_id__in=user_group_ids,
                            retailer__location__notification_group_location__notification_group__state=False,
                        )
                        | Q(
                            distributor__location__notification_group_location__notification_group_id__in=user_group_ids,
                            distributor__location__notification_group_location__notification_group__state=False,
                        )
                        | Q(
                            retailer__location__state__notification_group_location__notification_group_id__in=user_group_ids,
                            retailer__location__state__notification_group_location__notification_group__state=True,
                        )
                        | Q(
                            distributor__location__state__notification_group_location__notification_group_id__in=user_group_ids,
                            distributor__location__state__notification_group_location__notification_group__state=True,
                        )
                    )
            if filters.status_id:
                status_id = _resolve_filter_id(filters.status_id, "status")
                queryset = queryset.filter(status_id=status_id)
            if filters.client_id:
                client_id = _resolve_filter_id(filters.client_id, "client")
                queryset = queryset.filter(client_id=client_id)
            if filters.billing_entity_id:
                billing_entity_id = _resolve_filter_id(
                    filters.billing_entity_id, "billing entity"
                )
                queryset = queryset.filter(billing_entity_id=billing_entity_id)
            if filters.retailer_id:
                retailer_id = _resolve_filter_id(filters.retailer_id, "retailer")
                queryset = queryset.filter(retailer_id=retailer_id)
            if filters.distributor_id:
                distributor_id = _resolve_filter_id(
                    filters.distributor_id, "distributor"
                )
                queryset = queryset.filter(distributor_id=distributor_id)
            if filters.location_id:
                location_id = _resolve_filter_id(filters.location_id, "location")
                queryset = queryset.filter(location_id=location_id)
            if filters.state_id:
                state_id = _resolve_filter_id(filters.state_id, "state")
                queryset = queryset.filter(state_id=state_id)
            if filters.request_type_id:
                request_type_id = _resolve_filter_id(
                    filters.request_type_id, "request type"
                )
                queryset = queryset.filter(request_type_id=request_type_id)
            if filters.retailer_state_id:
                retailer_state_id = _resolve_filter_id(
                    filters.retailer_state_id, "retailer state"
                )
                queryset = queryset.filter(
                    retailer__location__state_id=retailer_state_id
                )
            if filters.distributor_state_id:
                distributor_state_id = _resolve_filter_id(
                    filters.distributor_state_id, "distributor state"
                )
                queryset = queryset.filter(
                    distributor__location__state_id=distributor_state_id
                )
            if filters.store_number:
                queryset = queryset.filter(store_number__icontains=filters.store_number)
            if filters.name:
                # Palette/tracker text search — a request is findable by its
                # own name OR its retailer ("Vons" surfaces Vons requests).
                from django.db.models import Q as _Q

                queryset = queryset.filter(
                    _Q(name__icontains=filters.name)
                    | _Q(retailer__name__icontains=filters.name)
                )
            if filters.date:
                queryset = queryset.filter(date__date=filters.date)
            else:
                # Inclusive event-date range — only applied when an exact
                # `date` wasn't given. Powers the tracker's quick-filter
                # chips (This week / Upcoming / Past).
                if filters.start_date:
                    queryset = queryset.filter(date__date__gte=filters.start_date)
                if filters.end_date:
                    queryset = queryset.filter(date__date__lte=filters.end_date)
            if filters.created_within_hours is not None:
                if filters.created_within_hours <= 0:
                    raise GraphQLError(
                        "created_within_hours must be greater than 0."
                    )
                created_after = timezone.now() - datetime.timedelta(
                    hours=filters.created_within_hours
                )
                queryset = queryset.filter(created_at__gte=created_after)
            if filters.edited is not None:
                queryset = queryset.filter(updated_by__isnull=not filters.edited)
            if filters.reviewed is not None:
                queryset = queryset.filter(reviewed=filters.reviewed)

        # Event-date sort direction for the tracker's clickable Date
        # column. Default stays "-date" (furthest-future first) so the
        # landing view is unchanged; "asc" flips to soonest-first.
        date_sort = (getattr(filters, "date_sort", None) or "desc").lower() if filters else "desc"
        order_field = "date" if date_sort == "asc" else "-date"
        queryset = queryset.order_by(order_field).distinct()

        # The Master Tracker loads the whole tenant in one page (it does
        # status bucketing, counts and date grouping client-side), so it
        # asks for `first: 1000`. The shared service default caps a page
        # at 100; without lifting it here the connection silently
        # truncated to the 100 newest requests and dropped the oldest
        # past events — including every event still owing a recap, which
        # are by definition in the past. That made ~18 overdue-recap
        # events that ARE counted in totalCount (118) invisible in the
        # tracker even on the ALL filter. Lift the ceiling so a request
        # for the full tenant pages through everything; default page size
        # is unchanged for callers that don't pass `first`.
        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
            default_limit=100,
            max_limit=2000,
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

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def activity_logs(
        self,
        info: strawberry.Info,
        first: int | None = 50,
        kind: str | None = None,
    ) -> list[types.RequestActivityLogEntry]:
        """Tenant-wide audit feed.

        Returns the most recent RequestActivityLog rows across every
        request in the current tenant — sorted newest-first. Powers
        the /audit admin page that surfaces "who did what when" for
        support / compliance debugging. Capped at 200 to keep payload
        sane; pagination not built yet (add when needed).

        `kind`, when set, filters to a single KIND_* value (e.g.
        'status_changed', 'recap_filed') for narrowing.
        """
        from events.models import RequestActivityLog
        from asgiref.sync import sync_to_async

        service = RequestQueriesService()
        tenant_id = await service.resolve_tenant_id(info)
        first_capped = max(1, min(first or 50, 200))

        def fetch():
            qs = (
                RequestActivityLog.objects.select_related(
                    "actor_user", "request"
                )
                .order_by("-created_at")
            )
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)
            if kind:
                qs = qs.filter(kind=kind)
            rows = list(qs[:first_capped])
            return [
                types.RequestActivityLogEntry(
                    uuid=str(row.uuid),
                    kind=row.kind,
                    summary=row.summary or "",
                    metadata_json=__import__("json").dumps(row.metadata or {}),
                    actor_email=getattr(row.actor_user, "email", None),
                    actor_name=(
                        " ".join(
                            filter(None, [
                                getattr(row.actor_user, "first_name", None),
                                getattr(row.actor_user, "last_name", None),
                            ])
                        )
                        or None
                    ) if row.actor_user else None,
                    created_at=row.created_at.isoformat(),
                )
                for row in rows
            ]

        return await sync_to_async(fetch)()


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
    async def rmm_clients(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        tenant_uuid: strawberry.ID | None = None,
        q: str | None = None,
    ) -> List[types.SparkUserType]:
        """Get active client users for RMM filter selects."""
        service = ClientQueriesService()
        resolved_tenant_id = await service.resolve_tenant_id(
            info,
            tenant_id=tenant_id,
            tenant_uuid=tenant_uuid,
        )

        queryset = (
            TenantedUser.objects.filter(
                is_active=True,
                user__is_active=True,
                user__role__slug=Role.CLIENT_SLUG,
            )
            .select_related("user")
            .order_by(
                "user__first_name",
                "user__last_name",
                "user__email",
                "user_id",
            )
        )

        if resolved_tenant_id:
            queryset = queryset.filter(tenant_id=resolved_tenant_id)

        if q:
            query = q.strip()
            if query:
                queryset = queryset.filter(
                    Q(user__first_name__icontains=query)
                    | Q(user__last_name__icontains=query)
                    | Q(user__email__icontains=query)
                    | Q(user__username__icontains=query)
                )

        rows = await sync_to_async(list)(queryset)
        users_by_id: dict[int, object] = {}
        for row in rows:
            if row.user_id not in users_by_id:
                users_by_id[row.user_id] = row.user

        return list(users_by_id.values())

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

    def get_filtered_queryset(
        self, tenant_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """Return global location queryset."""
        queryset = self.get_queryset().select_related("state")
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    async def get_record(
        self,
        id: strawberry.ID | None = None,
        tenant_id: strawberry.ID | None = None,
        uuid: str | None = None,
    ) -> Model | None:
        """Get a single location using id/uuid (global scope)."""
        queryset = self.get_queryset().select_related("state")

        filters: dict[str, object] = {}
        if id not in (None, ""):
            try:
                filters["id"] = resolve_id_to_int(id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid ID.") from exc
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if "id" not in filters and "uuid" not in filters:
            raise GraphQLError("Record not found.")

        try:
            return await sync_to_async(queryset.get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")


@strawberry.type
class LocationQueries:
    @strawberry.field
    async def public_locations(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Location]:
        """Get public locations globally."""
        service = LocationQueriesService()
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

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
        await service.get_user(info)
        queryset = service.get_ordered_queryset(q=q)
        if filters and filters.state_id:
            state_id = _resolve_filter_id(filters.state_id, "state")
            queryset = queryset.filter(state_id=state_id)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=50,
            max_limit=100,
            queryset=queryset,
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
            await service.get_user(info)
            location = await service.get_record(id=id, uuid=str(uuid) if uuid else None)
            return location
        except GraphQLError:
            return None


class StateQueriesService(BaseEventQueriesService):
    """Service for state queries."""

    ordering: tuple[str, ...] = ("name",)

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.State

    def get_filtered_queryset(
        self, tenant_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset


@strawberry.type
class StateQueries:
    @strawberry.field
    async def public_states(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.State]:
        """Get all states without authentication."""
        service = StateQueriesService()
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=100,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def states(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.State]:
        """Get all states."""
        service = StateQueriesService()
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=100,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def state(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.State | None:
        """Get a single state."""
        try:
            service = StateQueriesService()
            return await service.get_record(id=id, uuid=str(uuid) if uuid else None)
        except GraphQLError:
            return None


class DistributorQueriesService(BaseEventQueriesService):
    """Service for distributor queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Distributor

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.select_related("location", "state")


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
        filters: DistributorFiltersInput | None = None,
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

        queryset = service.get_ordered_queryset(tenant_id=tenant.id, q=q)
        if filters and filters.location_id:
            location_id = _resolve_filter_id(filters.location_id, "location")
            queryset = queryset.filter(location_id=location_id)
        if filters and filters.state_id:
            state_id = _resolve_filter_id(filters.state_id, "state")
            queryset = queryset.filter(state_id=state_id)

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

        queryset = service.get_ordered_queryset(tenant_id=resolved_tenant_id, q=q)
        if filters and filters.location_id:
            location_id = _resolve_filter_id(filters.location_id, "location")
            queryset = queryset.filter(location_id=location_id)
        if filters and filters.state_id:
            state_id = _resolve_filter_id(filters.state_id, "state")
            queryset = queryset.filter(state_id=state_id)

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

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.select_related("location")


@strawberry.type
class RetailerQueries:
    @strawberry.field
    async def public_retailers(
        self,
        info: strawberry.Info,
        request_url_name: str,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: RetailerFiltersInput | None = None,
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

        queryset = service.get_ordered_queryset(tenant_id=tenant.id, q=q)
        if filters and filters.location_id:
            location_id = _resolve_filter_id(filters.location_id, "location")
            queryset = queryset.filter(location_id=location_id)
        if filters and filters.state_id:
            state_id = _resolve_filter_id(filters.state_id, "state")
            queryset = queryset.filter(location__state_id=state_id)
        if filters and filters.is_national is not None:
            queryset = queryset.filter(is_national=filters.is_national)

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

        queryset = service.get_ordered_queryset(tenant_id=resolved_tenant_id, q=q)
        if filters and filters.location_id:
            location_id = _resolve_filter_id(filters.location_id, "location")
            queryset = queryset.filter(location_id=location_id)
        if filters and filters.state_id:
            state_id = _resolve_filter_id(filters.state_id, "state")
            queryset = queryset.filter(location__state_id=state_id)
        if filters and filters.is_national is not None:
            queryset = queryset.filter(is_national=filters.is_national)

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


class BillingEntityQueriesService(BaseEventQueriesService):
    """Service for billing entity queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.BillingEntity

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.select_related("state")


@strawberry.type
class BillingEntityQueries:
    @strawberry.field
    async def public_billing_entities(
        self,
        info: strawberry.Info,
        request_url_name: str,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.BillingEntity]:
        """Get public billing entities filtered by tenant request_url_name."""
        service = BillingEntityQueriesService()
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
    async def billing_entities(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: BillingEntityFiltersInput | None = None,
    ) -> CountableConnection[types.BillingEntity]:
        """Get all billing entities."""
        service = BillingEntityQueriesService()
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
    async def billing_entity(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.BillingEntity | None:
        """Get a single billing entity."""
        try:
            service = BillingEntityQueriesService()
            tenant_id = await service.resolve_tenant_id(info)
            return await service.get_record(
                id=id, uuid=str(uuid) if uuid else None, tenant_id=tenant_id
            )
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
            product_type_id = _resolve_filter_id(
                filters.product_type_id, "product type"
            )
            queryset = queryset.filter(product_type_id=product_type_id)

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
            product_type_id = _resolve_filter_id(
                filters.product_type_id, "product type"
            )
            queryset = queryset.filter(product_type_id=product_type_id)

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
        # Defense-in-depth: the unique constraint added in
        # events/migrations/0048_dedupe_timezones prevents duplicate rows, but
        # .distinct() guarantees the timezones / public_timezones resolvers never
        # surface a zone twice even if dupes somehow reappear.
        return queryset.distinct()


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
