import strawberry
from typing import List
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.db.models import QuerySet, Model, Prefetch

from recaps import types
from recaps import models
from ambassadors import models as ambassador_models
from recaps.inputs import RecapFiltersInput
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.mixins import SparkGraphQLMixin
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
            .objects.select_related("event", "recap_file")
            .prefetch_related(
                "recap_recap_file__recap_file",
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
        self, event_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if event_id:
            queryset = queryset.filter(event_id=event_id)
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        event_id: int | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return the filtered queryset with ordering applied."""
        queryset = self.get_filtered_queryset(event_id, q)
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        event_id: int | None = None,
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
            queryset = self.get_ordered_queryset(event_id, q, ordering)
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
        event_id: int | None = None,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return recaps linked to events assigned to the ambassador user."""
        queryset = self.get_ordered_queryset(event_id=event_id, q=q, ordering=ordering)
        return queryset.filter(
            event__ambassadors_events__ambassador__user=user
        ).distinct()

    async def get_ambassador_record_by_uuid(self, *, user, uuid: str) -> Model:
        """Return a single recap linked to the ambassador user by UUID."""
        try:
            queryset = self.get_ambassador_queryset(user=user)
            return await sync_to_async(queryset.get)(uuid=uuid)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")


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

        event_id: int | None = (
            int(filters.event_id) if filters and filters.event_id else None
        )
        queryset = service.get_ordered_queryset(event_id=event_id, q=q)

        return await service.get_connection(
            event_id=event_id,
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

        event_id: int | None = (
            int(filters.event_id) if filters and filters.event_id else None
        )
        queryset = service.get_ambassador_queryset(
            user=user,
            event_id=event_id,
            q=q,
        )

        return await service.get_connection(
            event_id=event_id,
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
