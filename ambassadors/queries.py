import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError
from django.db.models import QuerySet, Model

from ambassadors import types
from ambassadors import models
from ambassadors import inputs
from utils.graphql.permissions import StrictIsAuthenticated, IsClientOrSparkAdmin
from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)


class BaseAmbassadorQueriesService(SparkGraphQLMixin):
    """Service for ambassador queries."""

    ordering: tuple[str, ...] = ("-created_at",)

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.all()

    def get_filtered_queryset(self, q: str | None = None) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return the filtered queryset with ordering applied."""
        queryset = self.get_filtered_queryset(q)
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
        """Return a Relay compliant connection for the queryset."""
        if queryset is None:
            queryset = self.get_ordered_queryset(q, ordering)
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
            return await sync_to_async(self.get_model().objects.get)(id=id)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")

    async def get_record_by_uuid(self, uuid: str) -> Model | None:
        """Get a single record by UUID."""
        try:
            return await sync_to_async(self.get_model().objects.get)(uuid=uuid)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")


class FileTypeQueriesService(BaseAmbassadorQueriesService):
    """Service for file type queries."""

    def get_model(self) -> type[models.FileType]:
        """Get the model for the service."""
        return models.FileType


@strawberry.type
class FileTypeQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.FileType]:
        """Get all file types using Relay pagination."""
        service = FileTypeQueriesService()
        user = await service.get_user(info)

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
    async def file_type(
        self, info: strawberry.Info, uuid: strawberry.ID
    ) -> types.FileType | None:
        """Get a single file type by UUID."""
        try:
            service = FileTypeQueriesService()
            user = await service.get_user(info)
            file_type = await service.get_record_by_uuid(str(uuid))
            return file_type
        except GraphQLError:
            return None


@strawberry.type
class AmbassadorManagementQueries:
    """Queries for managing ambassadors and invitations (client/spark-admin only)."""

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def sent_invitations(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorInvitationFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorInvitationType]:
        """Get sent invitations for a tenant (client/spark-admin only)."""
        from .services import AmbassadorInvitationQueriesService
        service = AmbassadorInvitationQueriesService()
        return await service.get_sent_invitations(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def available_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """Get available ambassadors for a tenant (client/spark-admin only)."""
        from .services import AmbassadorQueriesService
        service = AmbassadorQueriesService()
        return await service.get_available_ambassadors(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )
