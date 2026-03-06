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
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.validation import parse_iso_date_optional
from utils.queues import Queues

from . import types, inputs
from .goals_service import extract_goal_updates, upsert_goals
from .queries import _goal_model_to_graphql, insights_model_to_graphql
from .services import DashboardQueriesService
from .tasks import create_goals_for_tenant, create_goals_for_all_tenants


@strawberry.type
class GenerateInsightsPayload:
    """Result of generateInsights mutation."""

    success: bool
    enqueued: bool
    insights: types.Insights | None = None


# When schema uses merge_types, root can be None; use this instance for mixin methods.
_mixin = SparkGraphQLMixin()


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
        root = self if self is not None else _mixin
        user = await root.get_user(info)
        tenant = await root.get_user_tenant(info, tenant_id=tenant_id, user=user)

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

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_goals(
        self,
        info: strawberry.Info,
        input: inputs.SetGoalsInput,
    ) -> types.Goal:
        """Create or update goals for a user for the given tenant and year."""
        from tenants import models as tenant_models

        root = self if self is not None else _mixin
        user = await root.get_user(info)
        tenant = await root.get_user_tenant(info, tenant_id=input.tenant_id, user=user)
        target_user_id = user.id

        if input.user_id is not None:
            try:
                target_user_id = resolve_id_to_int(input.user_id)
            except (TypeError, ValueError):
                raise GraphQLError("Invalid userId")

            if target_user_id != user.id:
                is_spark_admin = await user.role.is_spark_admin if getattr(user, "role", None) else False
                is_client = await user.role.is_client if getattr(user, "role", None) else False
                if not (is_spark_admin or is_client):
                    raise GraphQLError("You do not have permission to set goals for other users.")

            target_user_in_tenant = await sync_to_async(
                tenant_models.TenantedUser.objects.filter(
                    user_id=target_user_id,
                    tenant_id=tenant.id,
                    is_active=True,
                ).exists
            )()
            if not target_user_in_tenant:
                raise GraphQLError("Target user does not belong to the selected tenant.")

        goal_model = await sync_to_async(upsert_goals)(
            tenant.id,
            target_user_id,
            input.year,
            extract_goal_updates(input),
        )
        await sync_to_async(DashboardQueriesService.invalidate_cache_for_tenant)(
            tenant.id, ["event_dashboard"]
        )
        await sync_to_async(DashboardQueriesService.invalidate_cache_for_tenant)(
            0, ["event_dashboard"]
        )
        return _goal_model_to_graphql(goal_model, current=None)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def enqueue_create_goals_for_tenant(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        year: int,
    ) -> types.EnqueueCreateGoalsForTenantPayload:
        """Enqueue a job to create goals for every user in the tenant for the given year."""
        root = self if self is not None else _mixin
        user = await root.get_user(info)
        tenant = await root.get_user_tenant(info, tenant_id=tenant_id, user=user)
        Queues().default.add(create_goals_for_tenant, tenant.id, year)
        return types.EnqueueCreateGoalsForTenantPayload(success=True, enqueued=True)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def enqueue_create_goals_for_all_tenants(
        self,
        info: strawberry.Info,
        year: int,
    ) -> types.EnqueueCreateGoalsForAllTenantsPayload:
        """Enqueue a job that will enqueue one create_goals_for_tenant job per tenant for the given year."""
        Queues().default.add(create_goals_for_all_tenants, year)
        return types.EnqueueCreateGoalsForAllTenantsPayload(success=True, enqueued=True)
