"""
Dashboard GraphQL mutations.

Provides generateInsights mutation to create a new AI-Powered Insights run
from the latest data (sync or enqueue to RQ). Never updates existing insights.
"""
import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from tenants.insights.service import InsightsService
from tenants.insights.tasks import generate_insights_for_tenant
from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.validation import parse_iso_date_optional
from utils.queues import Queues

from . import types
from .queries import insights_model_to_graphql


@strawberry.type
class GenerateInsightsPayload:
    """Result of generateInsights mutation."""

    success: bool
    enqueued: bool
    insights: types.Insights | None = None


@strawberry.type
class DashboardMutations(SparkGraphQLMixin):
    """Dashboard mutations for Spark and Clients schemas."""

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_insights(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        enqueue: bool = False,
    ) -> GenerateInsightsPayload:
        """
        Generate a new AI-Powered Insights run from the latest data in the database.

        Does not update existing insights; always creates a new Insights record.
        Optional date range defaults to last 24 hours. If enqueue is True, the job
        is queued to RQ and the response indicates enqueued (no insights in response).
        """
        user = await self.get_user(info)
        tenant = await self.get_user_tenant(info, tenant_id=tenant_id, user=user)

        from_d = parse_iso_date_optional(from_date, "fromDate")
        to_d = parse_iso_date_optional(to_date, "toDate")

        if enqueue:
            Queues().default.add(
                generate_insights_for_tenant,
                tenant.id,
                from_d,
                to_d,
            )
            return GenerateInsightsPayload(
                success=True,
                enqueued=True,
                insights=None,
            )

        try:
            service = InsightsService(tenant)
            insights_model = await sync_to_async(
                lambda: service.generate_insights(
                    from_date=from_d,
                    to_date=to_d,
                    created_by=user,
                )
            )()
            insights_gql = await insights_model_to_graphql(insights_model)
            return GenerateInsightsPayload(
                success=True,
                enqueued=False,
                insights=insights_gql,
            )
        except ValueError as e:
            raise GraphQLError(str(e))
