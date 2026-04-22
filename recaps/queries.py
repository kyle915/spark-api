import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.db.models import QuerySet, Model, Prefetch, Q

from recaps import types
from recaps import models
from ambassadors import models as ambassador_models
from recaps.inputs import (
    CustomRecapFiltersInput,
    CustomRecapTemplateFiltersInput,
    FileRecapCategoryFiltersInput,
    RecapFiltersInput,
    TypeOfGoodFiltersInput,
)
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)


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
                "product_samples",
                "sales_performance",
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
            queryset = queryset.filter(name__icontains=q)
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
                    ).order_by("id"),
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
                    ).order_by("id"),
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
        if edited is not None:
            queryset = queryset.filter(updated_by__isnull=not edited)
        if q:
            queryset = queryset.filter(name__icontains=q)
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

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap(
        self, info: strawberry.Info, uuid: strawberry.ID
    ) -> types.Recap | None:
        """Get a single recap by UUID."""
        try:
            service = RecapQueriesService()
            user = await service.get_user(info)
            recap = await service.get_record_by_uuid(str(uuid))
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
        """Get custom recaps using Relay pagination."""
        service = CustomRecapQueriesService()
        await service.get_user(info)

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

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def custom_recap(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.CustomRecap | None:
        """Get a single custom recap by id or UUID."""
        try:
            service = CustomRecapQueriesService()
            await service.get_user(info)
            record = await service.get_record(
                id=int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
            )
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
        """Return a single CustomRecapTemplate including custom fields."""
        try:
            service = CustomRecapTemplateQueriesService()
            await service.get_user(info)
            resolved_tenant_id = (
                resolve_id_to_int(tenant_id) if tenant_id not in (None, "") else None
            )
            resolved_event_type_id = (
                resolve_id_to_int(event_type_id)
                if event_type_id not in (None, "")
                else None
            )
            record = await service.get_record(
                id=resolve_id_to_int(id) if id not in (None, "") else None,
                uuid=str(uuid) if uuid not in (None, "") else None,
                tenant_id=resolved_tenant_id,
                event_type_id=resolved_event_type_id,
            )
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
        """Return CustomRecapTemplate records filtered by tenant or event type."""
        service = CustomRecapTemplateQueriesService()
        await service.get_user(info)
        resolved_tenant_id = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        resolved_event_type_id = (
            resolve_id_to_int(filters.event_type_id)
            if filters and filters.event_type_id not in (None, "")
            else None
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
