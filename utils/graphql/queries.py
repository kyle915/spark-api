import strawberry
from typing import Any
from graphql import GraphQLError
from asgiref.sync import sync_to_async

from django.db.models import QuerySet
from django.db.models import Model

from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.relay import CountableConnection
from utils.graphql.relay import connection_from_queryset_async


class BaseQueriesService(SparkGraphQLMixin):
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
            queryset = self.get_ordered_queryset(
                tenant_id, q, ordering)
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
        """Get a single record by id or uuid."""
        if id is None and uuid is None:
            raise GraphQLError("Record identifier is required.")

        filters: dict[str, Any] = {}
        if id is not None:
            filters["id"] = id
        if uuid is not None:
            filters["uuid"] = uuid
        if tenant_id is not None:
            filters["tenant_id"] = tenant_id

        try:
            return await sync_to_async(self.get_model().objects.get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")
        except self.get_model().MultipleObjectsReturned:
            raise GraphQLError("Multiple records found for the given identifier.")

    async def resolve_query_tenant_id(
        self,
        info: strawberry.Info,
        *,
        filters: SparkGraphQLInput | None = None,
    ) -> int | None:
        """Return tenant id for queries; bypass for Spark unless explicitly provided."""
        user = await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)
        filters_tenant_id = getattr(filters, "tenant_id", None) if filters else None

        should_filter_by_tenant = (
            not is_spark_request or filters_tenant_id is not None
        )

        if should_filter_by_tenant:
            tenant = await self.get_user_tenant(
                info, tenant_id=filters_tenant_id, user=user
            )
            return tenant.id

        return None

    async def get_single_record(
        self,
        info: strawberry.Info,
        *,
        id: strawberry.ID | None = None,
        uuid: str | None = None,
        enforce_tenant: bool = True,
    ) -> Model | None:
        """Fetch a single record, bypassing tenant filter for Spark admins."""
        user = await self.get_user(info)
        if enforce_tenant and not self.is_spark_schema_request(info, user=user):
            tenant = await self.get_user_tenant(info, user=user)
            return await self.get_record(id=id, tenant_id=tenant.id, uuid=uuid)
        return await self.get_record(id=id, uuid=uuid)
