import strawberry

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import CountableConnection
from utils.graphql.queries import BaseQueriesService
from jobs import models
from django.db.models import QuerySet
from django.db.models import Model
from jobs import types


class StatusQueriesService(BaseQueriesService):
    """Service for status queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Status


@strawberry.type
class StatusQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_job_statuses(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection[types.Status]:
        """Get all statuses."""
        service = StatusQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_job_status(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Status | None:
        """Get a single status."""
        service = StatusQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_record(id, tenant.id)
