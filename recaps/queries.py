from typing import List

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.db.models import (
    CharField,
    QuerySet,
    Model,
    Prefetch,
    Q,
    Max,
    Count,
    Value,
)
from django.db.models.functions import Coalesce, Concat

from recaps import types
from recaps import models
from events import types as event_types
from ambassadors import models as ambassador_models
from recaps.inputs import (
    CustomRecapFiltersInput,
    CustomRecapTemplateFiltersInput,
    FileRecapCategoryFiltersInput,
    RecapFiltersInput,
    TypeOfGoodFiltersInput,
)
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    resolve_request_user_access,
    _is_admin_access,
    IGNITE_EMAIL_DOMAIN,
    IGNITE_ADMIN_EXCLUDE,
    email_grants_ignite_admin,
)
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)


# Page ceiling for the web "Your recaps" LIST resolvers (`recaps` +
# `customRecaps` on the clients schema). Filters (status / retailer /
# state / RMM / ambassador / date / search) run SERVER-SIDE, so a
# 50-row page is enough — the old 2000 pull existed only because the
# web list filtered in the browser (Liquid Death blew a 50-row cap).
RECAPS_LIST_MAX_LIMIT = 50


def _activation_bucket_name_q(field: str, bucket: str) -> Q | None:
    """Build a Q that matches request-type / template names for ``bucket``.

    Mirrors ``tenant_overview._activation_bucket_for_type_name`` order
    (Retail → On-premise → Events). List UI folds On-premise into Retail,
    so ``bucket="retail"`` includes both retail and onprem classifications.
    ``bucket="event"`` requires the event patterns and excludes names that
    would have classified as retail/onprem first.
    """
    key = (bucket or "").strip().lower()
    retail_q = Q(**{f"{field}__icontains": "retail"})
    onprem_q = (
        Q(**{f"{field}__iregex": r"on[-\s]?prem"})
        | Q(**{f"{field}__icontains": "bar"})
        | Q(**{f"{field}__icontains": "venue"})
    )
    event_q = (
        Q(**{f"{field}__icontains": "event"})
        | Q(**{f"{field}__icontains": "activation"})
        | Q(**{f"{field}__icontains": "festival"})
        | Q(**{f"{field}__iregex": r"pop[-\s]?up"})
    )
    if key == "retail":
        return retail_q | onprem_q
    if key == "event":
        return event_q & ~retail_q & ~onprem_q
    return None


def _apply_activation_bucket_filter(
    queryset: QuerySet,
    *,
    activation_bucket: str | None,
    name_field: str,
) -> QuerySet:
    """Narrow the list queryset by activation program bucket."""
    bucket_q = _activation_bucket_name_q(name_field, activation_bucket or "")
    if bucket_q is None:
        return queryset
    return queryset.filter(bucket_q)


def _apply_name_code_filters(
    queryset: QuerySet,
    *,
    retailer_name: str | None = None,
    state_code: str | None = None,
    ambassador_name: str | None = None,
) -> QuerySet:
    """Apply the web-list retailer-name / state-code / BA-name filters.

    The Spark recaps list dropdowns are labeled by retailer NAME and
    2-letter state CODE (not Relay ids). Match the same sources the
    cards display: recap FK, event retailer, request retailer, event
    state, and a ``, TX``-style address fallback for address-only events.

    BA name is a contains-match on the linked Ambassador's first/last/
    full name and on ``external_ba_name`` (write-in / 3rd-party BAs).
    The ambassadors list resolver hard-caps at 50, so the Recaps filter
    cannot rely on picking an ``ambassador_id`` from that dropdown.
    """
    name = (retailer_name or "").strip()
    if name:
        queryset = queryset.filter(
            Q(retailer__name__iexact=name)
            | Q(event__retailer__name__iexact=name)
            | Q(event__request__retailer__name__iexact=name)
        )
    code = (state_code or "").strip()
    if code:
        queryset = queryset.filter(
            Q(state__code__iexact=code)
            | Q(event__state__code__iexact=code)
            | Q(event__retailer__location__state__code__iexact=code)
            | Q(event__address__icontains=f", {code}")
            | Q(event__address__icontains=f" {code} ")
        )
    ba = (ambassador_name or "").strip()
    if ba:
        full_name = Concat(
            Coalesce("ambassador__user__first_name", Value("")),
            Value(" "),
            Coalesce("ambassador__user__last_name", Value("")),
            output_field=CharField(),
        )
        queryset = queryset.annotate(_ba_full_name=full_name).filter(
            Q(_ba_full_name__icontains=ba)
            | Q(ambassador__user__first_name__icontains=ba)
            | Q(ambassador__user__last_name__icontains=ba)
            | Q(external_ba_name__icontains=ba)
        )
    return queryset


class BaseRecapQueriesService(SparkGraphQLMixin):
    """Service for recap queries."""

    ordering: tuple[str, ...] = ("-created_at",)

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return (
            self.get_model()
            .objects.select_related(
                "event",
                "event__event_type",
                "event__timezone",
                "ambassador",
                "ambassador__user",
                "job",
                "retailer",
                "location",
                "state",
            )
            .prefetch_related(
                Prefetch(
                    "recap_files",
                    queryset=models.RecapFile.objects.select_related(
                        "file_recap_category",
                        "file_type",
                    ),
                ),
                "consumer_engagements",
                Prefetch(
                    "product_samples",
                    queryset=models.ProductSamples.objects.select_related("product"),
                ),
                Prefetch(
                    "sales_performance",
                    queryset=models.SalesPerformance.objects.select_related("product"),
                ),
                "consumer_feedback",
                "account_feedback",
                Prefetch(
                    "event__ambassadors_events",
                    queryset=ambassador_models.AmbassadorEvent.objects.select_related(
                        "ambassador",
                        "ambassador__user",
                    ),
                ),
                Prefetch("event__request__requests_stores_manager"),
            )
            .all()
        )

    def get_filtered_queryset(
        self,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        ambassador_id: int | None = None,
        retailer_id: int | None = None,
        location_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        approved: bool | None = None,
        q: str | None = None,
    ) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(event__tenant_id=tenant_id)
        if event_id:
            queryset = queryset.filter(event_id=event_id)
        if event_type_id:
            queryset = queryset.filter(event__event_type_id=event_type_id)
        if rmm_asigned_id:
            queryset = queryset.filter(event__rmm_asigned_id=rmm_asigned_id)
        if ambassador_id:
            queryset = queryset.filter(ambassador_id=ambassador_id)
        if retailer_id:
            queryset = queryset.filter(retailer_id=retailer_id)
        if location_id:
            queryset = queryset.filter(location_id=location_id)
        if state_id:
            queryset = queryset.filter(
                Q(state_id=state_id)
                | Q(job__event__retailer__location__state_id=state_id)
            )
        if event_date:
            queryset = queryset.filter(event__date__date=event_date)
        if start_date:
            queryset = queryset.filter(event__date__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(event__date__date__lte=end_date)
        if event_address:
            queryset = queryset.filter(event__address__icontains=event_address)
        if approved is not None:
            queryset = queryset.filter(approved=approved)
        if q:
            queryset = queryset.filter(
                Q(name__icontains=q)
                | Q(event__name__icontains=q)
                | Q(event__address__icontains=q)
                | Q(retailer__name__icontains=q)
                | Q(event__retailer__name__icontains=q)
                | Q(event__request__retailer__name__icontains=q)
            )
        return queryset

    def get_ordered_queryset(
        self,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        ambassador_id: int | None = None,
        retailer_id: int | None = None,
        location_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        approved: bool | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return the filtered queryset with ordering applied."""
        queryset = self.get_filtered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            ambassador_id=ambassador_id,
            retailer_id=retailer_id,
            location_id=location_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            q=q,
        )
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        ambassador_id: int | None = None,
        retailer_id: int | None = None,
        location_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        approved: bool | None = None,
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
            queryset = self.get_ordered_queryset(
                tenant_id=tenant_id,
                event_id=event_id,
                event_type_id=event_type_id,
                rmm_asigned_id=rmm_asigned_id,
                ambassador_id=ambassador_id,
                retailer_id=retailer_id,
                location_id=location_id,
                state_id=state_id,
                event_date=event_date,
                start_date=start_date,
                end_date=end_date,
                event_address=event_address,
                approved=approved,
                q=q,
                ordering=ordering,
            )
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

    async def get_record(self, id: strawberry.ID) -> Model | None:
        """Get a single record."""
        try:
            return await sync_to_async(self.get_queryset().get)(id=id)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")

    async def get_record_by_uuid(self, uuid: str) -> Model | None:
        """Get a single record by UUID."""
        try:
            return await sync_to_async(self.get_queryset().get)(uuid=uuid)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")


class RecapQueriesService(BaseRecapQueriesService):
    """Service for recap queries."""

    def get_model(self) -> type[models.Recap]:
        """Get the model for the service."""
        return models.Recap

    def get_ambassador_queryset(
        self,
        *,
        user,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        ambassador_id: int | None = None,
        retailer_id: int | None = None,
        location_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        approved: bool | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return recaps linked to events assigned to the ambassador user."""
        queryset = self.get_ordered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            ambassador_id=ambassador_id,
            retailer_id=retailer_id,
            location_id=location_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            q=q,
            ordering=ordering,
        )
        return queryset.filter(
            event__ambassadors_events__ambassador__user=user,
            ambassador__user=user,
        ).distinct()

    async def get_ambassador_record_by_uuid(self, *, user, uuid: str) -> Model:
        """Return a single recap linked to the ambassador user by UUID."""
        try:
            queryset = self.get_ambassador_queryset(user=user)
            return await sync_to_async(queryset.get)(uuid=uuid)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")


class TypeOfGoodQueriesService(SparkGraphQLMixin):
    """Service for TypeOfGood queries."""

    ordering: tuple[str, ...] = ("name",)

    def get_model(self) -> type[models.TypeOfGood]:
        """Return the model for the service."""
        return models.TypeOfGood

    def get_queryset(self) -> QuerySet:
        """Base queryset."""
        return self.get_model().objects.all()

    def get_filtered_queryset(
        self, q: str | None = None, tenant_id: int | None = None
    ) -> QuerySet:
        """Filter by name substring."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        q: str | None = None,
        tenant_id: int | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Apply ordering to filtered queryset."""
        queryset = self.get_filtered_queryset(q=q, tenant_id=tenant_id)
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
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
        """Return a Relay compliant connection for TypeOfGood."""
        if queryset is None:
            queryset = self.get_ordered_queryset(q=q, ordering=ordering)
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
        self, id: strawberry.ID | None = None, uuid: str | None = None
    ) -> Model:
        """Return a single TypeOfGood by id or uuid."""
        filters: dict[str, object] = {}
        if id not in (None, ""):
            filters["id"] = id
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if not filters:
            raise GraphQLError("Type of good not found.")

        try:
            return await sync_to_async(self.get_queryset().get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Type of good not found.")


class FileRecapCategoryQueriesService(SparkGraphQLMixin):
    """Service for FileRecapCategory queries."""

    ordering: tuple[str, ...] = ("name",)

    def get_model(self) -> type[models.FileRecapCategory]:
        """Return the model for the service."""
        return models.FileRecapCategory

    def get_queryset(self) -> QuerySet:
        """Base queryset."""
        return self.get_model().objects.all()

    def get_filtered_queryset(
        self, q: str | None = None, tenant_id: int | None = None
    ) -> QuerySet:
        """Filter by name substring."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        q: str | None = None,
        tenant_id: int | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Apply ordering to filtered queryset."""
        queryset = self.get_filtered_queryset(q=q, tenant_id=tenant_id)
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        q: str | None = None,
        tenant_id: int | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        default_limit: int = 10,
        max_limit: int = 50,
        ordering: tuple[str, ...] | None = None,
        queryset: QuerySet | None = None,
    ) -> CountableConnection[Model]:
        """Return a Relay compliant connection for FileRecapCategory."""
        if queryset is None:
            queryset = self.get_ordered_queryset(
                q=q, tenant_id=tenant_id, ordering=ordering
            )
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
        self, id: strawberry.ID | None = None, uuid: str | None = None
    ) -> Model:
        """Return a single FileRecapCategory by id or uuid."""
        filters: dict[str, object] = {}
        if id not in (None, ""):
            filters["id"] = id
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if not filters:
            raise GraphQLError("File recap category not found.")

        try:
            return await sync_to_async(self.get_queryset().get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("File recap category not found.")


class CustomRecapTemplateQueriesService(SparkGraphQLMixin):
    """Service for CustomRecapTemplate queries."""

    ordering: tuple[str, ...] = ("name", "id")

    def get_model(self) -> type[models.CustomRecapTemplate]:
        """Return the model for the service."""
        return models.CustomRecapTemplate

    def get_queryset(self) -> QuerySet:
        """Base queryset with custom fields preloaded."""
        return (
            self.get_model()
            .objects.select_related(
                "event_type",
                "tenant",
            )
            .prefetch_related(
                Prefetch(
                    "custom_field",
                    queryset=models.CustomField.objects.select_related(
                        "custom_field_type",
                        "recap_section",
                    ).order_by("recap_section__order", "recap_section_id", "order", "id"),
                )
            )
            .all()
        )

    def get_filtered_queryset(
        self,
        tenant_id: int | None = None,
        event_type_id: int | None = None,
    ) -> QuerySet:
        """Return CustomRecapTemplate queryset filtered by tenant or event type."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if event_type_id:
            queryset = queryset.filter(event_type_id=event_type_id)
        return queryset.distinct()

    def get_ordered_queryset(
        self,
        tenant_id: int | None = None,
        event_type_id: int | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Apply ordering to filtered CustomRecapTemplate queryset."""
        queryset = self.get_filtered_queryset(
            tenant_id=tenant_id,
            event_type_id=event_type_id,
        )
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        tenant_id: int | None = None,
        event_type_id: int | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        default_limit: int = 10,
        max_limit: int = 50,
        ordering: tuple[str, ...] | None = None,
        queryset: QuerySet | None = None,
    ) -> CountableConnection[Model]:
        """Return a Relay compliant connection for CustomRecapTemplate."""
        if queryset is None:
            queryset = self.get_ordered_queryset(
                tenant_id=tenant_id,
                event_type_id=event_type_id,
                ordering=ordering,
            )
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
        uuid: str | None = None,
        tenant_id: int | None = None,
        event_type_id: int | None = None,
    ) -> Model:
        """Return a single CustomRecapTemplate by id, uuid, tenant, or event type."""
        filters: dict[str, object] = {}
        if id not in (None, ""):
            filters["id"] = id
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if tenant_id:
            filters["tenant_id"] = tenant_id
        if event_type_id:
            filters["event_type_id"] = event_type_id
        if not filters:
            raise GraphQLError("Custom recap template not found.")

        try:
            return await sync_to_async(self.get_queryset().get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Custom recap template not found.")


class CustomRecapFieldTypeQueriesService(SparkGraphQLMixin):
    """Service for CustomRecapFieldType queries."""

    ordering: tuple[str, ...] = ("name",)

    def get_model(self) -> type[models.CustomRecapFieldType]:
        """Return the model for the service."""
        return models.CustomRecapFieldType

    def get_queryset(self) -> QuerySet:
        """Base queryset."""
        return self.get_model().objects.all()

    def get_filtered_queryset(self, q: str | None = None) -> QuerySet:
        """Filter by name substring."""
        queryset = self.get_queryset()
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Apply ordering to filtered queryset."""
        queryset = self.get_filtered_queryset(q=q)
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
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
        """Return a Relay compliant connection for CustomRecapFieldType."""
        if queryset is None:
            queryset = self.get_ordered_queryset(q=q, ordering=ordering)
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
        self, id: strawberry.ID | None = None, uuid: str | None = None
    ) -> Model:
        """Return a single CustomRecapFieldType by id or uuid."""
        filters: dict[str, object] = {}
        if id not in (None, ""):
            filters["id"] = id
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if not filters:
            raise GraphQLError("Custom recap field type not found.")

        try:
            return await sync_to_async(self.get_queryset().get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Custom recap field type not found.")


class RecapSectionQueriesService(SparkGraphQLMixin):
    """Service for RecapSection queries."""

    ordering: tuple[str, ...] = ("name",)

    def get_model(self) -> type[models.RecapSection]:
        """Return the model for the service."""
        return models.RecapSection

    def get_queryset(self) -> QuerySet:
        """Base queryset."""
        return self.get_model().objects.select_related("tenant").all()

    def get_filtered_queryset(
        self, q: str | None = None, tenant_id: int | None = None
    ) -> QuerySet:
        """Filter by name substring and tenant."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        q: str | None = None,
        tenant_id: int | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Apply ordering to filtered queryset."""
        queryset = self.get_filtered_queryset(q=q, tenant_id=tenant_id)
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        q: str | None = None,
        tenant_id: int | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        default_limit: int = 10,
        max_limit: int = 50,
        ordering: tuple[str, ...] | None = None,
        queryset: QuerySet | None = None,
    ) -> CountableConnection[Model]:
        """Return a Relay compliant connection for RecapSection."""
        if queryset is None:
            queryset = self.get_ordered_queryset(
                q=q,
                tenant_id=tenant_id,
                ordering=ordering,
            )
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
        self, id: strawberry.ID | None = None, uuid: str | None = None
    ) -> Model:
        """Return a single RecapSection by id or uuid."""
        filters: dict[str, object] = {}
        if id not in (None, ""):
            filters["id"] = id
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if not filters:
            raise GraphQLError("Recap section not found.")

        try:
            return await sync_to_async(self.get_queryset().get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Recap section not found.")


class CustomRecapQueriesService(SparkGraphQLMixin):
    """Service for CustomRecap queries."""

    ordering: tuple[str, ...] = ("-created_at",)

    def get_model(self) -> type[models.CustomRecap]:
        """Return the model for the service."""
        return models.CustomRecap

    def get_queryset(self) -> QuerySet:
        """Base queryset with related template and custom field values preloaded."""
        return (
            self.get_model()
            .objects.select_related(
                "event",
                "event__event_type",
                "event__timezone",
                "event__retailer",
                "event__retailer__location",
                "event__retailer__location__state",
                "event__location",
                "event__location__state",
                "event__state",
                "event__request",
                "event__request__retailer",
                "ambassador",
                "ambassador__user",
                "job",
                "retailer",
                "location",
                "state",
                "custom_recap_template",
                "custom_recap_template__event_type",
            )
            .prefetch_related(
                Prefetch(
                    "custom_recap_template__custom_field",
                    queryset=models.CustomField.objects.select_related(
                        "custom_field_type",
                        "recap_section",
                    ).order_by("recap_section__order", "recap_section_id", "order", "id"),
                ),
                Prefetch(
                    "custom_field_value",
                    queryset=models.CustomFieldValue.objects.select_related(
                        "custom_field",
                        "custom_field__custom_field_type",
                        "custom_field__recap_section",
                    ),
                ),
                Prefetch(
                    "custom_recap_product_sample",
                    queryset=models.CustomRecapProductSample.objects.select_related(
                        "product"
                    ),
                ),
                Prefetch(
                    "custom_recap_sale_performance",
                    queryset=models.CustomRecapSalePerformance.objects.select_related(
                        "product",
                        "type_of_good",
                    ),
                ),
                Prefetch(
                    "custom_recap_files",
                    queryset=models.CustomRecapFile.objects.select_related(
                        "file_type",
                        "file_recap_category",
                    ),
                ),
            )
            .all()
        )

    def get_filtered_queryset(
        self,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        custom_recap_template_id: int | None = None,
        ambassador_id: int | None = None,
        retailer_id: int | None = None,
        location_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        approved: bool | None = None,
        edited: bool | None = None,
        q: str | None = None,
    ) -> QuerySet:
        """Get filtered custom recaps queryset."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if event_id:
            queryset = queryset.filter(event_id=event_id)
        if event_type_id:
            queryset = queryset.filter(event__event_type_id=event_type_id)
        if rmm_asigned_id:
            queryset = queryset.filter(event__rmm_asigned_id=rmm_asigned_id)
        if custom_recap_template_id:
            queryset = queryset.filter(custom_recap_template_id=custom_recap_template_id)
        if ambassador_id:
            queryset = queryset.filter(ambassador_id=ambassador_id)
        if retailer_id:
            queryset = queryset.filter(
                Q(retailer_id=retailer_id) | Q(event__retailer_id=retailer_id)
            )
        if location_id:
            queryset = queryset.filter(location_id=location_id)
        if state_id:
            queryset = queryset.filter(
                Q(state_id=state_id)
                | Q(event__state_id=state_id)
                | Q(job__event__retailer__location__state_id=state_id)
            )
        if event_date:
            queryset = queryset.filter(event__date__date=event_date)
        if start_date:
            queryset = queryset.filter(event__date__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(event__date__date__lte=end_date)
        if event_address:
            queryset = queryset.filter(event__address__icontains=event_address)
        if approved is not None:
            queryset = queryset.filter(approved=approved)
        if edited is not None:
            queryset = queryset.filter(updated_by__isnull=not edited)
        if q:
            queryset = queryset.filter(
                Q(name__icontains=q)
                | Q(event__name__icontains=q)
                | Q(event__address__icontains=q)
                | Q(retailer__name__icontains=q)
                | Q(event__retailer__name__icontains=q)
            )
        return queryset

    def get_ordered_queryset(
        self,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        custom_recap_template_id: int | None = None,
        ambassador_id: int | None = None,
        retailer_id: int | None = None,
        location_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        approved: bool | None = None,
        edited: bool | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Apply ordering to filtered custom recaps queryset."""
        queryset = self.get_filtered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            custom_recap_template_id=custom_recap_template_id,
            ambassador_id=ambassador_id,
            retailer_id=retailer_id,
            location_id=location_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            edited=edited,
            q=q,
        )
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        custom_recap_template_id: int | None = None,
        ambassador_id: int | None = None,
        retailer_id: int | None = None,
        location_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        approved: bool | None = None,
        edited: bool | None = None,
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
        """Return a Relay compliant connection for CustomRecap."""
        if queryset is None:
            queryset = self.get_ordered_queryset(
                tenant_id=tenant_id,
                event_id=event_id,
                event_type_id=event_type_id,
                rmm_asigned_id=rmm_asigned_id,
                custom_recap_template_id=custom_recap_template_id,
                ambassador_id=ambassador_id,
                retailer_id=retailer_id,
                location_id=location_id,
                state_id=state_id,
                event_date=event_date,
                start_date=start_date,
                end_date=end_date,
                event_address=event_address,
                approved=approved,
                edited=edited,
                q=q,
                ordering=ordering,
            )
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
        self, id: strawberry.ID | None = None, uuid: str | None = None
    ) -> Model:
        """Return a single CustomRecap by id or uuid."""
        filters: dict[str, object] = {}
        if id not in (None, ""):
            filters["id"] = id
        if uuid not in (None, ""):
            filters["uuid"] = uuid
        if not filters:
            raise GraphQLError("Custom recap not found.")

        try:
            return await sync_to_async(self.get_queryset().get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Custom recap not found.")

    def get_ambassador_queryset(
        self,
        *,
        user,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        custom_recap_template_id: int | None = None,
        ambassador_id: int | None = None,
        retailer_id: int | None = None,
        location_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        approved: bool | None = None,
        edited: bool | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return custom recaps linked to authenticated ambassador user."""
        queryset = self.get_ordered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            custom_recap_template_id=custom_recap_template_id,
            ambassador_id=ambassador_id,
            retailer_id=retailer_id,
            location_id=location_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            edited=edited,
            q=q,
            ordering=ordering,
        )
        return queryset.filter(
            event__ambassadors_events__ambassador__user=user,
            ambassador__user=user,
        ).distinct()

    async def get_ambassador_record_by_uuid(self, *, user, uuid: str) -> Model:
        """Return a single custom recap linked to ambassador user by UUID."""
        try:
            queryset = self.get_ambassador_queryset(user=user)
            return await sync_to_async(queryset.get)(uuid=uuid)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Custom recap not found.")


# ---------------------------------------------------------------------------
# Tenant scoping helper — used to enforce per-client data isolation on the
# recap-side resolvers. The events/requests resolvers already do this via
# `resolve_tenant_id` on their service; recap resolvers historically bypassed
# the same pattern, which let a client user with the right token pass a
# different tenant_id (or omit it) and read cross-tenant recaps. This helper
# closes that gap by forcing the tenant_id for client-role users to their own
# tenant, regardless of what came in via filters.tenant_id.
# ---------------------------------------------------------------------------
async def _enforce_client_tenant(
    service: SparkGraphQLMixin,
    info: strawberry.Info,
    filters_tenant_id: int | None,
) -> int | None:
    user = await service.get_user(info)
    role_slug = service.get_role_slug(user)
    if role_slug == "client":
        # Reuse the tenant resolver from the shared mixin. It validates that
        # the user actually has access to the requested tenant; passing
        # tenant_id=None falls back to the user's default tenant.
        tenant = await service.get_user_tenant(info, tenant_id=filters_tenant_id)
        return tenant.id
    return filters_tenant_id


# ---------------------------------------------------------------------------
# Client visibility gate for unapproved recaps.
#
# Clients (the tenant-side users — Liquid Death, Girl Beer, Borjomi, etc.)
# should not see recaps until an Ignite admin has approved them. This avoids
# the client seeing half-filled drafts, BA typos, or in-flight reconciliation
# work. Admins (spark-admin / is_staff / is_super / any @igniteproductions.co
# email) always see everything.
#
# We resolve role authoritatively via resolve_request_user_access — the JWT
# user.role FK is often unhydrated inside async resolvers, so a naive
# `get_role_slug(user) == "client"` check returns "" for real clients and the
# gate would silently no-op. Re-reading the user row from the DB closes that
# gap (same fix pattern used by IsClientOrSparkAdmin and the tenants/users
# resolvers).
# ---------------------------------------------------------------------------
async def _is_client_only_user(info: strawberry.Info) -> bool:
    """True for tenant-role users with no admin escalation.

    A user counts as "client-only" iff:
      - role_slug is "client", AND
      - is_staff is False, AND
      - is_superuser is False, AND
      - email does NOT end in @igniteproductions.co

    Anything that grants admin access (per _is_admin_access in
    utils/graphql/permissions.py) flips this to False so admins get the
    unrestricted view. Unauthenticated requests bottom out as False —
    StrictIsAuthenticated rejects them upstream.
    """
    request = getattr(info.context, "request", None)
    user = getattr(request, "user", None) if request else None
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    role_slug, is_staff, is_super, email = await resolve_request_user_access(
        user
    )
    # A removed Ignite admin (on IGNITE_ADMIN_EXCLUDE) is no longer privileged:
    # treat them as a restricted user here so they get the approved-only view,
    # never the admin "see drafts" path below. Checked first since this is an
    # inverted gate (False = unrestricted/admin).
    if (email or "").lower() in IGNITE_ADMIN_EXCLUDE:
        return True
    if is_staff or is_super:
        return False
    if email_grants_ignite_admin(email):
        return False
    return role_slug == "client"


# ---------------------------------------------------------------------------
# Cross-tenant READ gate for the single-recap detail-by-id/uuid resolvers
# (`recap`, `customRecap`). Follow-up to the recap WRITE IDOR sweep (#708),
# which gated the mutation cluster via
# RecapMutationService._assert_caller_authorized_for_recap_tenant but flagged
# the read side as still leaky.
#
# The single-recap query resolvers loaded a recap by raw id/uuid from an
# UNSCOPED queryset and only verified tenant ownership when
# `get_role_slug(user) == "client"`. That check is doubly unsafe:
#   * it never fires for a non-client role (e.g. a Brand Ambassador), so a BA
#     could read ANY tenant's recap by guessing its id/uuid; and
#   * `get_role_slug` reads the JWT user.role FK directly, which is often
#     unhydrated inside async resolvers — so it returns "" even for a genuine
#     client, silently skipping the tenant check.
#
# This helper mirrors the #708 admin-bypass + `user.get_tenant` membership
# pattern, resolving role/flags authoritatively from the DB row (same as
# `_is_client_only_user` above):
#   * admins (spark-admin / is_staff / is_superuser / @igniteproductions.co)
#     may read any tenant;
#   * every other role may read ONLY inside a tenant they belong to;
#   * a recap with no resolvable tenant is denied.
# Raises GraphQLError on denial — the resolvers catch GraphQLError and return
# None, so a cross-tenant lookup is indistinguishable from "not found" and
# never leaks the existence of another tenant's record.
# ---------------------------------------------------------------------------
async def _assert_caller_authorized_to_read_recap_tenant(
    user,
    tenant_id: int | None,
) -> None:
    if user is None:
        raise GraphQLError("Authentication required.")

    role_slug, is_staff, is_super, email = await resolve_request_user_access(
        user
    )
    if _is_admin_access(role_slug, is_staff, is_super, email):
        return

    if tenant_id is None:
        raise GraphQLError("Recap not found.")

    try:
        await sync_to_async(user.get_tenant)(tenant_id=tenant_id)
    except Exception:
        raise GraphQLError("Recap not found.")


# How long after a walk-in BA's LAST clock punch we consider the shift
# wrapped and the recap genuinely overdue. Long enough that a BA taking a
# break, or one whose clock-out failed, isn't chased while still on site.
WALKIN_WRAP_GRACE_HOURS = 4


@strawberry.type
class RecapQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recaps(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: RecapFiltersInput | None = None,
    ) -> CountableConnection[types.Recap]:
        """Get all recaps using Relay pagination."""
        service = RecapQueriesService()
        user = await service.get_user(info)

        # Client users see only their own tenant — even if filters.tenant_id
        # is unset or points elsewhere. Spark admins / ambassadors keep the
        # filters.tenant_id pass-through behavior.
        filters_tenant_id_raw: int | None = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        tenant_id = await _enforce_client_tenant(
            service, info, filters_tenant_id_raw
        )
        # Hard tenant scope for the recaps LIST (clients schema). Every
        # web consumer of this resolver is a per-tenant surface and always
        # passes the active tenant; the ONLY way `tenant_id` is None here
        # is an unrestricted role (staff / superuser / spark-admin) that
        # sent no tenant — in which case the old resolver returned EVERY
        # tenant's recaps, leaking cross-tenant rows into the "Your
        # recaps" list (the live Girl Beer bug: an LD recap and an
        # inflated count). Mirror `recapEventOptions`: with no tenant in
        # scope return an EMPTY page rather than all tenants. Clients are
        # already pinned to their own tenant by _enforce_client_tenant
        # above, so this only closes the admin-side, no-tenant footgun.
        if not tenant_id:
            empty = service.get_model().objects.none()
            return await service.get_connection(
                first=first,
                after=after,
                last=last,
                before=before,
                # Match the populated branch's ceiling so the empty page
                # validates `first` the same way (no behavior change — the
                # queryset is empty — but keeps the two paths consistent).
                max_limit=RECAPS_LIST_MAX_LIMIT,
                queryset=empty,
            )
        event_id: int | None = (
            resolve_id_to_int(filters.event_id)
            if filters and filters.event_id
            else None
        )
        event_type_id: int | None = (
            resolve_id_to_int(filters.event_type)
            if filters and filters.event_type
            else None
        )
        rmm_asigned_id: int | None = (
            resolve_id_to_int(filters.rmm_asigned_id)
            if filters and filters.rmm_asigned_id
            else None
        )
        ambassador_id: int | None = (
            resolve_id_to_int(filters.ambassador_id)
            if filters and filters.ambassador_id
            else None
        )
        retailer_id: int | None = (
            resolve_id_to_int(filters.retailer_id)
            if filters and filters.retailer_id
            else None
        )
        location_id: int | None = (
            resolve_id_to_int(filters.location_id)
            if filters and filters.location_id
            else None
        )
        state_id: int | None = (
            resolve_id_to_int(filters.state_id)
            if filters and filters.state_id
            else None
        )
        event_date = filters.event_date if filters else None
        start_date = filters.start_date if filters else None
        end_date = filters.end_date if filters else None
        event_address = filters.event_address if filters else None
        approved = filters.approved if filters else None
        retailer_name = filters.retailer_name if filters else None
        state_code = filters.state_code if filters else None
        ambassador_name = (
            getattr(filters, "ambassador_name", None) if filters else None
        )
        activation_bucket = (
            getattr(filters, "activation_bucket", None) if filters else None
        )
        # Force approved=True for client users — they never see drafts.
        # Overrides whatever the client (or a stale frontend filter) sent.
        # Also ignore `shared`: that flag is an admin publish step, not
        # the client visibility gate. A stale frontend that defaulted
        # My Recaps to shared=true hid every approved-but-unshared recap
        # (Feel Free: 118 approved, 0 shared).
        client_only = await _is_client_only_user(info)
        if client_only:
            approved = True
        queryset = service.get_ordered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            ambassador_id=ambassador_id,
            retailer_id=retailer_id,
            location_id=location_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            q=q,
        )
        if filters and filters.edited is not None:
            queryset = queryset.filter(updated_by__isnull=not filters.edited)
        if (
            filters
            and getattr(filters, "shared", None) is not None
            and not client_only
        ):
            queryset = queryset.filter(shared_at__isnull=not filters.shared)
        queryset = _apply_name_code_filters(
            queryset,
            retailer_name=retailer_name,
            state_code=state_code,
            ambassador_name=ambassador_name,
        )
        queryset = _apply_activation_bucket_filter(
            queryset,
            activation_bucket=activation_bucket,
            name_field="event__request__request_type__name",
        )
        # List cards only need hero + count + flat event labels.
        # Drop the fat default prefetches (engagements/samples/sales).
        queryset = (
            queryset.prefetch_related(None)
            .select_related(
                "event",
                "event__retailer",
                "event__retailer__location",
                "event__retailer__location__state",
                "event__state",
                "event__request",
                "event__request__retailer",
                "event__request__request_type",
                "ambassador",
                "ambassador__user",
                "retailer",
                "state",
            )
            .annotate(_recap_files_count=Count("recap_files", distinct=True))
            .prefetch_related(
                Prefetch(
                    "recap_files",
                    queryset=models.RecapFile.objects.filter(
                        Q(file__iendswith=".jpg")
                        | Q(file__iendswith=".jpeg")
                        | Q(file__iendswith=".png")
                        | Q(file__iendswith=".webp")
                        | Q(file__iendswith=".gif")
                        | Q(file__iendswith=".heic")
                        | Q(file__iendswith=".heif")
                    )
                    .select_related("file_recap_category", "file_type")
                    .order_by("id"),
                ),
            )
        )

        return await service.get_connection(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            ambassador_id=ambassador_id,
            retailer_id=retailer_id,
            location_id=location_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            max_limit=RECAPS_LIST_MAX_LIMIT,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap(
        self, info: strawberry.Info, uuid: strawberry.ID
    ) -> types.Recap | None:
        """Get a single recap by UUID.

        We look up the recap and then verify the caller is authorized for the
        recap's tenant. Cross-tenant uuids return None instead of raising —
        keeps the resolver indistinguishable from a "not found" lookup so we
        don't leak the existence of cross-tenant records.
        """
        try:
            service = RecapQueriesService()
            user = await service.get_user(info)
            recap = await service.get_record_by_uuid(str(uuid))
            if recap is None:
                return None
            # Cross-tenant READ gate (follow-up to #708). The previous check
            # only fired for the "client" role-slug — which never matched a BA
            # and was unreliable for clients (unhydrated role FK in async
            # context) — so any authenticated user could read another tenant's
            # recap by uuid. Authorize authoritatively (admins any tenant,
            # everyone else only their own). The legacy Recap has NO direct
            # tenant FK — its tenant is reached via the event, which the
            # service queryset select_related's, so this read is async-safe.
            await _assert_caller_authorized_to_read_recap_tenant(
                user, getattr(recap.event, "tenant_id", None)
            )
            # Client-only users never see unapproved drafts. Return None
            # rather than raising — same posture as the tenant mismatch
            # above, so we don't leak the existence of in-flight work.
            if await _is_client_only_user(info):
                if not getattr(recap, "approved", False):
                    return None
            return recap
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recaps(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: CustomRecapFiltersInput | None = None,
    ) -> CountableConnection[types.CustomRecap]:
        """Get custom recaps using Relay pagination.

        Client-role users are forced to their own tenant — same scoping
        pattern as the recaps resolver above.
        """
        service = CustomRecapQueriesService()
        await service.get_user(info)

        filters_tenant_id_raw = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        resolved_tenant_id = await _enforce_client_tenant(
            service, info, filters_tenant_id_raw
        )
        # Same hard tenant scope as the legacy `recaps` resolver: custom
        # recaps share the "Your recaps" list, so an unrestricted role
        # with no tenant in scope must get an EMPTY page, not every
        # tenant's custom recaps. Clients are already pinned by
        # _enforce_client_tenant above.
        if not resolved_tenant_id:
            empty = service.get_model().objects.none()
            return await service.get_connection(
                first=first,
                after=after,
                last=last,
                before=before,
                # Mirror the populated branch's ceiling (no behavior change
                # on an empty queryset; keeps the two paths consistent).
                max_limit=RECAPS_LIST_MAX_LIMIT,
                queryset=empty,
            )
        resolved_event_id = (
            resolve_id_to_int(filters.event_id)
            if filters and filters.event_id not in (None, "")
            else None
        )
        resolved_event_type_id = (
            resolve_id_to_int(filters.event_type)
            if filters and filters.event_type not in (None, "")
            else None
        )
        resolved_rmm_asigned_id = (
            resolve_id_to_int(filters.rmm_asigned_id)
            if filters and filters.rmm_asigned_id not in (None, "")
            else None
        )
        resolved_custom_recap_template_id = (
            resolve_id_to_int(filters.custom_recap_template_id)
            if filters and filters.custom_recap_template_id not in (None, "")
            else None
        )
        resolved_ambassador_id = (
            resolve_id_to_int(filters.ambassador_id)
            if filters and filters.ambassador_id not in (None, "")
            else None
        )
        resolved_retailer_id = (
            resolve_id_to_int(filters.retailer_id)
            if filters and filters.retailer_id not in (None, "")
            else None
        )
        resolved_location_id = (
            resolve_id_to_int(filters.location_id)
            if filters and filters.location_id not in (None, "")
            else None
        )
        resolved_state_id = (
            resolve_id_to_int(filters.state_id)
            if filters and filters.state_id not in (None, "")
            else None
        )
        event_date = filters.event_date if filters else None
        start_date = filters.start_date if filters else None
        end_date = filters.end_date if filters else None
        event_address = filters.event_address if filters else None
        approved = filters.approved if filters else None
        edited = filters.edited if filters else None
        retailer_name = filters.retailer_name if filters else None
        state_code = filters.state_code if filters else None
        ambassador_name = (
            getattr(filters, "ambassador_name", None) if filters else None
        )
        activation_bucket = (
            getattr(filters, "activation_bucket", None) if filters else None
        )

        # Force approved=True for client users on custom recaps too.
        # Ignore `shared` the same way as the legacy recaps list — clients
        # see every approved recap, not only ones stamped shared_at.
        client_only = await _is_client_only_user(info)
        if client_only:
            approved = True

        queryset = service.get_ordered_queryset(
            tenant_id=resolved_tenant_id,
            event_id=resolved_event_id,
            event_type_id=resolved_event_type_id,
            rmm_asigned_id=resolved_rmm_asigned_id,
            custom_recap_template_id=resolved_custom_recap_template_id,
            ambassador_id=resolved_ambassador_id,
            retailer_id=resolved_retailer_id,
            location_id=resolved_location_id,
            state_id=resolved_state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            edited=edited,
            q=q,
        )
        queryset = _apply_name_code_filters(
            queryset,
            retailer_name=retailer_name,
            state_code=state_code,
            ambassador_name=ambassador_name,
        )
        queryset = _apply_activation_bucket_filter(
            queryset,
            activation_bucket=activation_bucket,
            # Custom list cards label by template name (same as FE
            # activationBucketForTypeName on customRecapTemplate.name).
            name_field="custom_recap_template__name",
        )
        if (
            filters
            and getattr(filters, "shared", None) is not None
            and not client_only
        ):
            queryset = queryset.filter(shared_at__isnull=not filters.shared)
        if filters and getattr(filters, "needs_store_map", None) and not client_only:
            queryset = queryset.filter(
                is_third_party=True, store_mapping_status="unmatched"
            )
        queryset = (
            queryset.prefetch_related(None)
            .select_related(
                "event",
                "event__retailer",
                "event__state",
                "ambassador",
                "ambassador__user",
                "retailer",
                "state",
                "custom_recap_template",
            )
            .annotate(
                _custom_recap_files_count=Count("custom_recap_files", distinct=True)
            )
            .prefetch_related(
                Prefetch(
                    "custom_field_value",
                    queryset=models.CustomFieldValue.objects.select_related(
                        "custom_field",
                        "custom_field__custom_field_type",
                        "custom_field__recap_section",
                    ),
                ),
                Prefetch(
                    "custom_recap_files",
                    queryset=models.CustomRecapFile.objects.filter(
                        Q(url__iendswith=".jpg")
                        | Q(url__iendswith=".jpeg")
                        | Q(url__iendswith=".png")
                        | Q(url__iendswith=".webp")
                        | Q(url__iendswith=".gif")
                        | Q(url__iendswith=".heic")
                        | Q(url__iendswith=".heif")
                    )
                    .select_related("file_type", "file_recap_category")
                    .order_by("id"),
                ),
            )
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            event_id=resolved_event_id,
            event_type_id=resolved_event_type_id,
            rmm_asigned_id=resolved_rmm_asigned_id,
            custom_recap_template_id=resolved_custom_recap_template_id,
            ambassador_id=resolved_ambassador_id,
            retailer_id=resolved_retailer_id,
            location_id=resolved_location_id,
            state_id=resolved_state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            edited=edited,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            max_limit=RECAPS_LIST_MAX_LIMIT,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recap(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.CustomRecap | None:
        """Get a single custom recap by id or UUID.

        The caller only gets the record back if they're authorized for its
        tenant. Cross-tenant lookups silently return None (matches "not found"
        so we don't leak the existence of other-tenant records).
        """
        try:
            service = CustomRecapQueriesService()
            user = await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            if record is None:
                return None
            # Cross-tenant READ gate (follow-up to #708) — same fix as the
            # legacy `recap` resolver above. The old "client" role-slug check
            # never matched a BA and was unreliable for clients, leaving this
            # by-id/uuid accessor open cross-tenant. CustomRecap carries a
            # direct tenant FK.
            await _assert_caller_authorized_to_read_recap_tenant(
                user, record.tenant_id
            )
            # Hide unapproved drafts from client-only users — same
            # posture as the legacy Recap resolver above.
            if await _is_client_only_user(info):
                if not getattr(record, "approved", False):
                    return None
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def type_of_goods(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: TypeOfGoodFiltersInput | None = None,
    ) -> CountableConnection[types.TypeOfGood]:
        """List TypeOfGood records."""
        service = TypeOfGoodQueriesService()
        await service.get_user(info)
        resolved_tenant_id = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        queryset = service.get_ordered_queryset(q=q, tenant_id=resolved_tenant_id)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def type_of_good(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.TypeOfGood | None:
        """Return a single TypeOfGood."""
        try:
            service = TypeOfGoodQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_recap_categories(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: FileRecapCategoryFiltersInput | None = None,
    ) -> CountableConnection[types.FileRecapCategory]:
        """List FileRecapCategory records."""
        service = FileRecapCategoryQueriesService()
        await service.get_user(info)
        tenant_id: int | None = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id
            else None
        )
        queryset = service.get_ordered_queryset(q=q, tenant_id=tenant_id)

        return await service.get_connection(
            q=q,
            tenant_id=tenant_id,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_recap_category(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.FileRecapCategory | None:
        """Return a single FileRecapCategory."""
        try:
            service = FileRecapCategoryQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recap_template(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
        tenant_id: strawberry.ID | None = None,
        event_type_id: strawberry.ID | None = None,
    ) -> types.CustomRecapTemplate | None:
        """Return a single CustomRecapTemplate — tenant-scoped.

        Same leak surface as `customRecapTemplates`: the recap form can
        load a single template by event_type/tenant, and without scoping a
        cross-tenant lookup would hand back another client's template
        (wrong fields rendered). We resolve the active tenant the same way
        as the list resolver and verify the returned record belongs to it,
        returning None on a tenant mismatch — same "indistinguishable from
        not-found" posture as the single `recap`/`customRecap` resolvers,
        so we never leak the existence of another tenant's template.
        """
        from events.queries import EventQueriesService

        try:
            service = CustomRecapTemplateQueriesService()
            await service.get_user(info)
            explicit_tenant_id = (
                tenant_id if tenant_id not in (None, "") else None
            )
            resolved_event_type_id = (
                resolve_id_to_int(event_type_id)
                if event_type_id not in (None, "")
                else None
            )

            resolved_tenant_id: int | None = None
            try:
                resolved_tenant_id = (
                    await EventQueriesService().resolve_tenant_id(
                        info,
                        tenant_id=explicit_tenant_id,
                    )
                )
            except GraphQLError:
                resolved_tenant_id = None

            record = await service.get_record(
                id=resolve_id_to_int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
                tenant_id=resolved_tenant_id,
                event_type_id=resolved_event_type_id,
            )
            if record is None:
                return None
            # Defence in depth: when a tenant is in scope, a record from a
            # different tenant (e.g. fetched by raw id/uuid) is treated as
            # not found. When no tenant is in scope (unrestricted role,
            # no selection) we don't widen access here — get_record still
            # required some lookup key — but we refuse to hand back a
            # record we can't tie to the active tenant.
            if resolved_tenant_id and record.tenant_id != resolved_tenant_id:
                return None
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recap_templates(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: CustomRecapTemplateFiltersInput | None = None,
    ) -> CountableConnection[types.CustomRecapTemplate]:
        """Return CustomRecapTemplate records — STRICTLY tenant-scoped.

        This is what the recap-build form reads to pick the ACTIVE custom
        recap template for the tenant (the "NEW RECAP" form / Connecteam
        importer auto-populate from it). The template DEFINES which fields
        the form renders, so a cross-tenant leak here means the form draws
        the WRONG client's template — e.g. Girl Beer's form rendering
        Liquid Death's fields (the live bug Kyle hit).

        The old resolver passed `filters.tenant_id` straight through and
        applied NO tenant scoping when it was absent, so for an
        unrestricted role (staff / superuser / spark-admin) an empty
        tenant filter returned EVERY tenant's templates — and the frontend
        was the only thing keeping the picker on the active tenant, a
        guard that re-broke (same class as the #343 events leak).

        Fix mirrors `recapEventOptions`: resolve the active tenant on the
        SERVER (staff pass their selected dashboard tenant via
        `filters.tenant_id`; a client/tenant user resolves to their own
        membership tenant even with no filter) and return an EMPTY page
        when NO tenant is in scope, rather than every tenant's templates.
        The picker can no longer load another client's template no matter
        what the client sends.
        """
        from events.queries import EventQueriesService

        service = CustomRecapTemplateQueriesService()
        await service.get_user(info)

        explicit_tenant_id = (
            filters.tenant_id
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        resolved_event_type_id = (
            resolve_id_to_int(filters.event_type_id)
            if filters and filters.event_type_id not in (None, "")
            else None
        )

        # Resolve the active tenant exactly like the recap event picker:
        # staff/spark-admin acting inside a tenant pass it explicitly and
        # bypass the membership check; tenant users resolve to their own
        # membership; unrestricted roles with NO explicit tenant resolve
        # to None (the hard stop below).
        resolved_tenant_id: int | None = None
        try:
            resolved_tenant_id = await EventQueriesService().resolve_tenant_id(
                info,
                tenant_id=explicit_tenant_id,
            )
        except GraphQLError:
            resolved_tenant_id = None

        # Hard stop: a template picker without a tenant in scope returns
        # nothing — never every tenant's templates. This is the line that
        # stops the recap form from ever loading another client's
        # template fields.
        if not resolved_tenant_id:
            empty = service.get_model().objects.none()
            return await service.get_connection(
                tenant_id=None,
                event_type_id=resolved_event_type_id,
                first=first,
                after=after,
                last=last,
                before=before,
                queryset=empty,
            )

        queryset = service.get_ordered_queryset(
            tenant_id=resolved_tenant_id,
            event_type_id=resolved_event_type_id,
        )
        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            event_type_id=resolved_event_type_id,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recap_field_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.CustomRecapFieldType]:
        """List CustomRecapFieldType records."""
        service = CustomRecapFieldTypeQueriesService()
        await service.get_user(info)
        queryset = service.get_ordered_queryset(q=q)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recap_field_type(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.CustomRecapFieldType | None:
        """Return a single CustomRecapFieldType."""
        try:
            service = CustomRecapFieldTypeQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def connecteam_import_job(
        self,
        info: strawberry.Info,
        job_id: str,
    ) -> types.ConnecteamImportJob | None:
        """Poll an async Connecteam PDF import."""
        from recaps.mutation_parts.connecteam import read_connecteam_job

        payload = await sync_to_async(read_connecteam_job)((job_id or "").strip())
        if not payload:
            return None
        recap = None
        recap_id = payload.get("custom_recap_id")
        if recap_id:
            recap = await sync_to_async(
                lambda: models.CustomRecap.objects.filter(id=recap_id).first()
            )()
        return types.ConnecteamImportJob(
            job_id=job_id,
            status=str(payload.get("status") or "pending"),
            message=payload.get("message"),
            custom_recap=recap,
            matched_count=int(payload.get("matched_count") or 0),
            unmatched_count=int(payload.get("unmatched_count") or 0),
            images_attached=int(payload.get("images_attached") or 0),
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_sections(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        tenant_id: strawberry.ID | None = None,
    ) -> CountableConnection[types.RecapSection]:
        """List RecapSection records."""
        service = RecapSectionQueriesService()
        await service.get_user(info)
        resolved_tenant_id = (
            resolve_id_to_int(tenant_id) if tenant_id not in (None, "") else None
        )
        queryset = service.get_ordered_queryset(q=q, tenant_id=resolved_tenant_id)

        return await service.get_connection(
            q=q,
            tenant_id=resolved_tenant_id,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_section(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.RecapSection | None:
        """Return a single RecapSection."""
        try:
            service = RecapSectionQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def executive_summary(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        window_days: int = 7,
    ) -> types.ExecutiveSummaryType | None:
        """Top-line tenant rollup for the dashboard "Pace" widget.

        Same aggregator the weekly executive-summary email uses
        (`digest.exec_services.build_executive_summary`). Live query
        means kyle can refresh mid-week and see the delta without
        waiting for Monday's email.

        Returns null when no tenant is in scope — the caller should
        either pass `tenantId` explicitly or rely on the implicit
        tenant from their session (TODO: wire tenant context once
        we have it on `info.context`).
        """
        from tenants.models import Tenant
        from digest.exec_services import build_executive_summary

        if tenant_id in (None, ""):
            return None
        try:
            resolved_id = resolve_id_to_int(tenant_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid tenant id.")

        days = max(1, min(int(window_days or 7), 365))

        def _build() -> types.ExecutiveSummaryType | None:
            try:
                tenant = Tenant.objects.get(pk=resolved_id)
            except Tenant.DoesNotExist:
                return None
            summary = build_executive_summary(tenant, window_days=days)
            return types.ExecutiveSummaryType(
                tenant_id=strawberry.ID(str(summary.tenant_id)),
                tenant_name=summary.tenant_name,
                period_label=summary.period_label,
                recap_count=summary.recap_count,
                consumer_reach=summary.consumer_reach,
                samples_distributed=summary.samples_distributed,
                top_stores=[
                    types.ExecutiveSummaryRow(
                        label=r.label,
                        primary_metric=r.primary_metric,
                        secondary_metric=r.secondary_metric,
                    )
                    for r in summary.top_stores
                ],
                top_bas=[
                    types.ExecutiveSummaryRow(
                        label=r.label,
                        primary_metric=r.primary_metric,
                        secondary_metric=r.secondary_metric,
                    )
                    for r in summary.top_bas
                ],
                recap_count_delta=summary.recap_count_delta,
                consumer_reach_delta=summary.consumer_reach_delta,
                recap_count_delta_chip=summary.delta_chip("recaps"),
                consumer_reach_delta_chip=summary.delta_chip("reach"),
            )

        return await sync_to_async(_build, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def missing_recap_events(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        lookback_days: int = 30,
    ) -> List[types.MissingRecapEventType]:
        """Events that already wrapped (end_time < now) but have no
        *filed* recap. An empty clock-out stub does not count — those
        events still belong on /recaps/missing. Drives the admin page
        for "what does my team still owe me?"

        `lookback_days` caps how far back we go so the query stays
        cheap on long-running tenants. Default 30 days; bump for
        quarterly audits. Hard ceiling of 365 days to keep the
        select sane.

        Returns one row per Event, with all assigned BAs in
        `assigned_ambassadors` so the UI can offer a per-row
        "Nudge BA" / "File for them" action without an N+1 round
        trip per ambassador.
        """
        from datetime import timedelta
        from django.utils import timezone
        from events import models as event_models
        from ambassadors import models as a_models

        days = max(1, min(int(lookback_days or 30), 365))
        now = timezone.now()
        cutoff = now - timedelta(days=days)

        resolved_tenant_id: int | None = None
        if tenant_id not in (None, ""):
            try:
                resolved_tenant_id = resolve_id_to_int(tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant id.")

        def _fetch() -> List[types.MissingRecapEventType]:
            from recaps.filed import events_missing_filed_recap

            qs = (
                event_models.Event.objects.select_related(
                    "retailer",
                    "state",
                    "tenant",
                    "request",
                )
                # "Wrapped" has TWO shapes and only one was handled.
                #
                # A scheduled event wraps at end_time. A WALK-IN event — one a
                # BA conjured through the check-in link or a walk-up code — has
                # NO end_time at all, and a NULL satisfies neither comparison,
                # so every one of them was invisible here. /recaps/missing said
                # "All clear" for Total Wireless no matter how many check-in
                # shifts owed a recap: not a stale count, a whole class of
                # shift the query could never return.
                #
                # A walk-in counts as wrapped once its LAST clock punch is far
                # enough back that the BA is plainly done. Anything more recent
                # is someone still working, and nagging them mid-shift is how
                # this page loses its credibility.
                .annotate(last_punch=Max("attendance__clock_time"))
                .filter(
                    Q(end_time__lt=now, end_time__gte=cutoff)
                    | Q(
                        end_time__isnull=True,
                        last_punch__lt=now - timedelta(hours=WALKIN_WRAP_GRACE_HOURS),
                        last_punch__gte=cutoff,
                    )
                )
                # An event is missing a recap if neither family has a
                # *filed* row (photos / metrics / submitted_at). An empty
                # clock-out stub must still show here — existence-only
                # hid those shifts from /recaps/missing.
                # Borjomi-style tenants file via custom_recap; both
                # families go through recaps.filed.events_missing_filed_recap.
            )
            qs = events_missing_filed_recap(qs)
            qs = (
                qs.annotate(wrapped_at=Coalesce("end_time", "last_punch"))
                .order_by("-wrapped_at")
            )
            if resolved_tenant_id is not None:
                qs = qs.filter(tenant_id=resolved_tenant_id)

            # Prefetch ambassador assignments so the per-event loop
            # below doesn't do N+1. Event → AmbassadorEvent reverse
            # accessor is `ambassadors_events` (note: plural at the
            # start) — `ambassador_events` was a typo that crashed
            # the /recaps/missing page with a prefetch_related error.
            qs = qs.prefetch_related(
                "ambassadors_events__ambassador__user",
            )

            rows: List[types.MissingRecapEventType] = []
            for ev in qs[:200]:  # safety cap — UI paginates client-side
                hours_overdue: int | None = None
                end = getattr(ev, "end_time", None) or getattr(ev, "start_time", None)
                if end:
                    delta = now - end
                    hours_overdue = max(0, int(delta.total_seconds() // 3600))

                ambassadors: List[types.MissingRecapAmbassadorInfo] = []
                for ae in ev.ambassadors_events.all():
                    amb = getattr(ae, "ambassador", None)
                    user = getattr(amb, "user", None) if amb else None
                    name = (
                        " ".join(
                            filter(
                                None,
                                [
                                    getattr(user, "first_name", "") or "",
                                    getattr(user, "last_name", "") or "",
                                ],
                            )
                        ).strip()
                        or getattr(user, "email", None)
                        or "(unnamed)"
                    )
                    ambassadors.append(
                        types.MissingRecapAmbassadorInfo(
                            ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                            ambassador_uuid=strawberry.ID(
                                str(getattr(amb, "uuid", "")) if amb else ""
                            ),
                            name=name,
                            email=getattr(user, "email", None) if user else None,
                            is_approved=bool(getattr(ae, "is_approved", False)),
                        )
                    )

                venue = (
                    getattr(ev, "name", None)
                    or getattr(getattr(ev, "retailer", None), "name", None)
                )
                state_code = getattr(getattr(ev, "state", None), "code", None)
                req_uuid = getattr(getattr(ev, "request", None), "uuid", None)

                rows.append(
                    types.MissingRecapEventType(
                        event_uuid=strawberry.ID(str(ev.uuid)),
                        event_name=venue or "(shift)",
                        venue=venue,
                        address=getattr(ev, "address", None),
                        state_code=state_code,
                        date=(
                            ev.date.isoformat() if getattr(ev, "date", None) else None
                        ),
                        start_time=(
                            ev.start_time.isoformat()
                            if getattr(ev, "start_time", None)
                            else None
                        ),
                        end_time=(
                            ev.end_time.isoformat()
                            if getattr(ev, "end_time", None)
                            else None
                        ),
                        hours_overdue=hours_overdue,
                        request_uuid=(
                            strawberry.ID(str(req_uuid)) if req_uuid else None
                        ),
                        assigned_ambassadors=ambassadors,
                    )
                )
            return rows

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_event_options(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        tenant_uuid: strawberry.ID | None = None,
        q: str | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection[event_types.Event]:
        """Events selectable when FILING A RECAP — STRICTLY tenant-scoped.

        This is the picker behind "NEW RECAP → Tell us how it went →
        search events". Unlike the general `events` query (which is an
        all-tenants admin surface and intentionally returns every
        tenant's events for Ignite staff), this resolver is dedicated to
        the recap-build form and MUST NEVER surface another client's
        events: you can only file a recap against an event in the tenant
        you're currently acting in.

        The general `events` resolver leaks cross-tenant rows for
        unrestricted roles (staff / superuser / spark-admin) whenever the
        caller omits a tenant filter — the old picker relied on the
        client always passing `filters.tenantId`, a frontend-only guard
        (#343) that re-broke here. This resolver closes that hole on the
        server: it resolves the active tenant and refuses to return
        anything unless one is in scope, so no client mistake can ever
        leak another brand's events into the recap picker.

        Tenant resolution mirrors the other *Client resolvers:
          - staff/spark-admin acting inside a tenant pass `tenantId`
            (their currently-selected dashboard tenant) → scoped to it,
            no membership check (same staff-bypass as PR #531);
          - a client/tenant user resolves to their own membership tenant
            even if they pass nothing;
          - if NO tenant can be resolved we return an EMPTY page rather
            than every tenant's events.
        """
        from events.queries import EventQueriesService

        service = EventQueriesService()
        resolved_tenant_id: int | None = None
        try:
            resolved_tenant_id = await service.resolve_tenant_id(
                info,
                tenant_id=tenant_id,
                tenant_uuid=tenant_uuid,
            )
        except GraphQLError:
            resolved_tenant_id = None

        # Hard stop: a recap picker without a tenant in scope returns
        # nothing. This is the line that makes cross-tenant leakage
        # impossible regardless of what the client passes.
        if not resolved_tenant_id:
            empty = service.get_model().objects.none()
            return await connection_from_queryset_async(
                empty,
                first=first,
                after=after,
                last=last,
                before=before,
                default_limit=30,
                max_limit=500,
            )

        queryset = service.get_ordered_queryset(
            tenant_id=resolved_tenant_id, q=q
        ).order_by("-date").distinct()

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
            default_limit=30,
            max_limit=500,
        )


@strawberry.type
class RecapMobileQueries:
    @strawberry.field(
        permission_classes=[StrictIsAuthenticated],
        description="Recaps scoped to the authenticated ambassador (mobile).",
    )
    async def recaps_mobile(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: RecapFiltersInput | None = None,
    ) -> CountableConnection[types.Recap]:
        """Get recaps for the logged ambassador using Relay pagination."""
        service = RecapQueriesService()
        user = await service.get_user(info)

        tenant_id: int | None = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        event_id: int | None = (
            resolve_id_to_int(filters.event_id)
            if filters and filters.event_id
            else None
        )
        event_type_id: int | None = (
            resolve_id_to_int(filters.event_type)
            if filters and filters.event_type
            else None
        )
        rmm_asigned_id: int | None = (
            resolve_id_to_int(filters.rmm_asigned_id)
            if filters and filters.rmm_asigned_id
            else None
        )
        ambassador_id: int | None = (
            resolve_id_to_int(filters.ambassador_id)
            if filters and filters.ambassador_id
            else None
        )
        retailer_id: int | None = (
            resolve_id_to_int(filters.retailer_id)
            if filters and filters.retailer_id
            else None
        )
        location_id: int | None = (
            resolve_id_to_int(filters.location_id)
            if filters and filters.location_id
            else None
        )
        state_id: int | None = (
            resolve_id_to_int(filters.state_id)
            if filters and filters.state_id
            else None
        )
        event_date = filters.event_date if filters else None
        start_date = filters.start_date if filters else None
        end_date = filters.end_date if filters else None
        event_address = filters.event_address if filters else None
        approved = filters.approved if filters else None
        queryset = service.get_ambassador_queryset(
            user=user,
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            ambassador_id=ambassador_id,
            retailer_id=retailer_id,
            location_id=location_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            q=q,
        )
        if filters and filters.edited is not None:
            queryset = queryset.filter(updated_by__isnull=not filters.edited)

        return await service.get_connection(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            ambassador_id=ambassador_id,
            retailer_id=retailer_id,
            location_id=location_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(
        permission_classes=[StrictIsAuthenticated],
        description="Single recap scoped to the authenticated ambassador (mobile).",
    )
    async def recap_mobile(
        self,
        info: strawberry.Info,
        uuid: strawberry.ID,
    ) -> types.Recap | None:
        """Get a single recap for the logged ambassador by UUID."""
        try:
            service = RecapQueriesService()
            user = await service.get_user(info)
            recap = await service.get_ambassador_record_by_uuid(
                user=user, uuid=str(uuid)
            )
            return recap
        except GraphQLError:
            return None

    @strawberry.field(
        permission_classes=[StrictIsAuthenticated],
        description="Custom recaps scoped to the authenticated ambassador (mobile).",
    )
    async def custom_recaps_mobile(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: CustomRecapFiltersInput | None = None,
    ) -> CountableConnection[types.CustomRecap]:
        """Get custom recaps for logged ambassador using Relay pagination."""
        service = CustomRecapQueriesService()
        user = await service.get_user(info)

        resolved_tenant_id = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        resolved_event_id = (
            resolve_id_to_int(filters.event_id)
            if filters and filters.event_id not in (None, "")
            else None
        )
        resolved_event_type_id = (
            resolve_id_to_int(filters.event_type)
            if filters and filters.event_type not in (None, "")
            else None
        )
        resolved_rmm_asigned_id = (
            resolve_id_to_int(filters.rmm_asigned_id)
            if filters and filters.rmm_asigned_id not in (None, "")
            else None
        )
        resolved_custom_recap_template_id = (
            resolve_id_to_int(filters.custom_recap_template_id)
            if filters and filters.custom_recap_template_id not in (None, "")
            else None
        )
        # Mobile endpoint is always scoped to authenticated ambassador user.
        resolved_ambassador_id = None
        resolved_retailer_id = (
            resolve_id_to_int(filters.retailer_id)
            if filters and filters.retailer_id not in (None, "")
            else None
        )
        resolved_location_id = (
            resolve_id_to_int(filters.location_id)
            if filters and filters.location_id not in (None, "")
            else None
        )
        resolved_state_id = (
            resolve_id_to_int(filters.state_id)
            if filters and filters.state_id not in (None, "")
            else None
        )
        event_date = filters.event_date if filters else None
        start_date = filters.start_date if filters else None
        end_date = filters.end_date if filters else None
        event_address = filters.event_address if filters else None
        approved = filters.approved if filters else None
        edited = filters.edited if filters else None

        queryset = service.get_ambassador_queryset(
            user=user,
            tenant_id=resolved_tenant_id,
            event_id=resolved_event_id,
            event_type_id=resolved_event_type_id,
            rmm_asigned_id=resolved_rmm_asigned_id,
            custom_recap_template_id=resolved_custom_recap_template_id,
            ambassador_id=resolved_ambassador_id,
            retailer_id=resolved_retailer_id,
            location_id=resolved_location_id,
            state_id=resolved_state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            edited=edited,
            q=q,
        )

        return await service.get_connection(
            tenant_id=resolved_tenant_id,
            event_id=resolved_event_id,
            event_type_id=resolved_event_type_id,
            rmm_asigned_id=resolved_rmm_asigned_id,
            custom_recap_template_id=resolved_custom_recap_template_id,
            ambassador_id=resolved_ambassador_id,
            retailer_id=resolved_retailer_id,
            location_id=resolved_location_id,
            state_id=resolved_state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            approved=approved,
            edited=edited,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(
        permission_classes=[StrictIsAuthenticated],
        description="Single custom recap scoped to the authenticated ambassador (mobile).",
    )
    async def custom_recap_mobile(
        self,
        info: strawberry.Info,
        uuid: strawberry.ID,
    ) -> types.CustomRecap | None:
        """Get a single custom recap for logged ambassador by UUID."""
        try:
            service = CustomRecapQueriesService()
            user = await service.get_user(info)
            recap = await service.get_ambassador_record_by_uuid(
                user=user, uuid=str(uuid)
            )
            return recap
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def type_of_goods(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: TypeOfGoodFiltersInput | None = None,
    ) -> CountableConnection[types.TypeOfGood]:
        """List TypeOfGood records (mobile)."""
        service = TypeOfGoodQueriesService()
        await service.get_user(info)
        resolved_tenant_id = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        queryset = service.get_ordered_queryset(q=q, tenant_id=resolved_tenant_id)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def type_of_good(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.TypeOfGood | None:
        """Return a single TypeOfGood (mobile)."""
        try:
            service = TypeOfGoodQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_recap_categories(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: FileRecapCategoryFiltersInput | None = None,
    ) -> CountableConnection[types.FileRecapCategory]:
        """List FileRecapCategory records (mobile)."""
        service = FileRecapCategoryQueriesService()
        await service.get_user(info)
        tenant_id: int | None = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id
            else None
        )
        queryset = service.get_ordered_queryset(q=q, tenant_id=tenant_id)

        return await service.get_connection(
            q=q,
            tenant_id=tenant_id,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_recap_category(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.FileRecapCategory | None:
        """Return a single FileRecapCategory (mobile)."""
        try:
            service = FileRecapCategoryQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recap_field_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.CustomRecapFieldType]:
        """List CustomRecapFieldType records (mobile)."""
        service = CustomRecapFieldTypeQueriesService()
        await service.get_user(info)
        queryset = service.get_ordered_queryset(q=q)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recap_field_type(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.CustomRecapFieldType | None:
        """Return a single CustomRecapFieldType (mobile)."""
        try:
            service = CustomRecapFieldTypeQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            return record
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_sections(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        tenant_id: strawberry.ID | None = None,
    ) -> CountableConnection[types.RecapSection]:
        """List RecapSection records (mobile)."""
        service = RecapSectionQueriesService()
        await service.get_user(info)
        resolved_tenant_id = (
            resolve_id_to_int(tenant_id) if tenant_id not in (None, "") else None
        )
        queryset = service.get_ordered_queryset(q=q, tenant_id=resolved_tenant_id)

        return await service.get_connection(
            q=q,
            tenant_id=resolved_tenant_id,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_section(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.RecapSection | None:
        """Return a single RecapSection (mobile)."""
        try:
            service = RecapSectionQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
            return record
        except GraphQLError:
            return None
