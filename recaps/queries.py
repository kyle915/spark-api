import strawberry
from typing import List
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.db.models import QuerySet, Model, Prefetch

from recaps import types
from recaps import models
from ambassadors import models as ambassador_models
from recaps.inputs import (
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
        retailer_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
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
        if retailer_id:
            queryset = queryset.filter(retailer_id=retailer_id)
        if state_id:
            queryset = queryset.filter(
                job__event__retailer__location__state_id=state_id
            )
        if event_date:
            queryset = queryset.filter(event__date__date=event_date)
        if start_date:
            queryset = queryset.filter(event__date__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(event__date__date__lte=end_date)
        if event_address:
            queryset = queryset.filter(event__address__icontains=event_address)
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        tenant_id: int | None = None,
        event_id: int | None = None,
        event_type_id: int | None = None,
        rmm_asigned_id: int | None = None,
        retailer_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return the filtered queryset with ordering applied."""
        queryset = self.get_filtered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            retailer_id=retailer_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
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
        retailer_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
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
                retailer_id=retailer_id,
                state_id=state_id,
                event_date=event_date,
                start_date=start_date,
                end_date=end_date,
                event_address=event_address,
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
        retailer_id: int | None = None,
        state_id: int | None = None,
        event_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_address: str | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return recaps linked to events assigned to the ambassador user."""
        queryset = self.get_ordered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            retailer_id=retailer_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
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
        retailer_id: int | None = (
            resolve_id_to_int(filters.retailer_id)
            if filters and filters.retailer_id
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
        queryset = service.get_ordered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            retailer_id=retailer_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            q=q,
        )
        if filters and filters.edited is not None:
            queryset = queryset.filter(updated_by__isnull=not filters.edited)

        return await service.get_connection(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            retailer_id=retailer_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
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
        retailer_id: int | None = (
            resolve_id_to_int(filters.retailer_id)
            if filters and filters.retailer_id
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
        queryset = service.get_ambassador_queryset(
            user=user,
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            retailer_id=retailer_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
            q=q,
        )
        if filters and filters.edited is not None:
            queryset = queryset.filter(updated_by__isnull=not filters.edited)

        return await service.get_connection(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type_id=event_type_id,
            rmm_asigned_id=rmm_asigned_id,
            retailer_id=retailer_id,
            state_id=state_id,
            event_date=event_date,
            start_date=start_date,
            end_date=end_date,
            event_address=event_address,
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
