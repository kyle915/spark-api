"""
GraphQL tests for goals: queries, mutations, types, and inputs.

Covers:
- goals query (Goal type, with/without current values)
- eventDashboard.goalsProgress (GoalProgress list)
- setGoals mutation (SetGoalsInput -> Goal)
- enqueueCreateGoalsForTenant mutation (EnqueueCreateGoalsForTenantPayload)
- enqueueCreateGoalsForAllTenants mutation (EnqueueCreateGoalsForAllTenantsPayload)
"""
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from tenants.dashboard.tests.base import DashboardGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestGoalsQuery(DashboardGraphQLTestCase):
    """Tests for the goals query and Goal type."""

    @pytest.mark.asyncio
    async def test_goals_query_returns_none_when_no_goal_exists(self):
        """goals(tenantId, year) returns null when user has no goal for that year."""
        query = """
        query GetGoals($tenantId: ID!, $year: Int!) {
            goals(tenantId: $tenantId, year: $year) {
                id
                year
                eventTargetGoal
            }
        }
        """
        result = await self._execute_query_authenticated(
            query,
            {"tenantId": str(self.tenant.id), "year": 2025},
            self.client_user,
        )
        assert result.errors is None
        assert result.data is not None
        assert result.data["goals"] is None

    @pytest.mark.asyncio
    async def test_goals_query_returns_goal_after_set_goals(self):
        """After setGoals, goals query returns the Goal with all target fields."""
        mutation = """
        mutation SetGoals($input: SetGoalsInput!) {
            setGoals(input: $input) {
                id
                uuid
                tenantId
                userId
                year
                eventTargetGoal
                consumerSamplingGoal
                brandAwarenessGoal
                purchaseIntentGoal
                firstTimeBuyersGoal
            }
        }
        """
        mut_result = await self._execute_mutation(
            mutation,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "year": 2025,
                    "eventTargetGoal": 50,
                    "consumerSamplingGoal": 1000,
                    "brandAwarenessGoal": 75.0,
                }
            },
            self.endpoint_path,
            user=self.client_user,
        )
        assert mut_result.errors is None, mut_result.errors
        assert mut_result.data is not None
        payload = mut_result.data["setGoals"]
        assert payload["year"] == 2025
        assert payload["eventTargetGoal"] == 50
        assert payload["consumerSamplingGoal"] == 1000
        assert payload["brandAwarenessGoal"] == 75.0
        assert payload["tenantId"] == str(self.tenant.id)
        assert payload["userId"] == str(self.client_user.id)

        query = """
        query GetGoals($tenantId: ID!, $year: Int!) {
            goals(tenantId: $tenantId, year: $year) {
                id
                year
                eventTargetGoal
                consumerSamplingGoal
                brandAwarenessGoal
            }
        }
        """
        result = await self._execute_query_authenticated(
            query,
            {"tenantId": str(self.tenant.id), "year": 2025},
            self.client_user,
        )
        assert result.errors is None
        assert result.data["goals"] is not None
        assert result.data["goals"]["year"] == 2025
        assert result.data["goals"]["eventTargetGoal"] == 50

    @pytest.mark.asyncio
    async def test_goals_query_with_date_range_includes_current_values(self):
        """goals(tenantId, year, startDate, endDate) includes current_* when date range provided."""
        from tenants.dashboard.goals_service import get_or_create_goal

        await sync_to_async(get_or_create_goal)(
            self.tenant.id, self.client_user.id, 2025
        )

        query = """
        query GetGoalsWithCurrent($tenantId: ID!, $year: Int!, $startDate: String!, $endDate: String!) {
            goals(tenantId: $tenantId, year: $year, startDate: $startDate, endDate: $endDate) {
                id
                year
                currentEventsCount
                currentConsumerSampling
                currentBrandAwareness
                currentPurchaseIntent
            }
        }
        """
        result = await self._execute_query_authenticated(
            query,
            {
                "tenantId": str(self.tenant.id),
                "year": 2025,
                "startDate": "2025-01-01",
                "endDate": "2025-12-31",
            },
            self.client_user,
        )
        assert result.errors is None
        assert result.data["goals"] is not None
        # Current values may be 0 if no events in range; type must be present
        assert "currentEventsCount" in result.data["goals"]
        assert "currentConsumerSampling" in result.data["goals"]

    @pytest.mark.asyncio
    async def test_goals_query_requires_tenant_access(self):
        """goals returns null when user has no access to the tenant."""
        from tenants.models import Tenant

        other_tenant = await sync_to_async(self.create_tenant)(
            name="Other Tenant"
        )
        query = """
        query GetGoals($tenantId: ID!, $year: Int!) {
            goals(tenantId: $tenantId, year: $year) { id year }
        }
        """
        result = await self._execute_query_authenticated(
            query,
            {"tenantId": str(other_tenant.id), "year": 2025},
            self.client_user,
        )
        assert result.errors is None
        assert result.data["goals"] is None


@pytest.mark.django_db(transaction=True)
class TestEventDashboardGoalsProgress(DashboardGraphQLTestCase):
    """Tests for eventDashboard.goalsProgress (GoalProgress type)."""

    @pytest.mark.asyncio
    async def test_event_dashboard_goals_progress_field_present(self):
        """eventDashboard returns goalsProgress (list or null)."""
        query = """
        query EventDashboardGoalsProgress {
            eventDashboard {
                metrics { totalEvents consumersSampled }
                goalsProgress {
                    name
                    target
                    current
                    percentageComplete
                }
            }
        }
        """
        result = await self._execute_query_authenticated(
            query, {}, self.client_user
        )
        assert result.errors is None
        assert result.data is not None
        dashboard = result.data["eventDashboard"]
        assert "goalsProgress" in dashboard
        # May be null if no goal for user/year, or list
        assert dashboard["goalsProgress"] is None or isinstance(
            dashboard["goalsProgress"], list
        )

    @pytest.mark.asyncio
    async def test_event_dashboard_goals_progress_when_goal_set(self):
        """When user has goals with targets, goalsProgress returns GoalProgress items."""
        year = timezone.now().year
        mutation = """
        mutation SetGoals($input: SetGoalsInput!) {
            setGoals(input: $input) {
                id year eventTargetGoal consumerSamplingGoal
            }
        }
        """
        await self._execute_mutation(
            mutation,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "year": year,
                    "eventTargetGoal": 10,
                    "consumerSamplingGoal": 200,
                }
            },
            self.endpoint_path,
            user=self.client_user,
        )

        query = """
        query EventDashboardWithGoalsProgress {
            eventDashboard {
                goalsProgress {
                    name
                    target
                    current
                    percentageComplete
                }
            }
        }
        """
        result = await self._execute_query_authenticated(
            query, {}, self.client_user
        )
        assert result.errors is None
        goals_progress = result.data["eventDashboard"]["goalsProgress"]
        assert goals_progress is not None
        assert len(goals_progress) >= 1
        for item in goals_progress:
            assert "name" in item
            assert "target" in item
            assert "current" in item
            assert "percentageComplete" in item
            assert 0 <= item["percentageComplete"] <= 100


@pytest.mark.django_db(transaction=True)
class TestSetGoalsMutation(DashboardGraphQLTestCase):
    """Tests for setGoals mutation and SetGoalsInput."""

    @pytest.mark.asyncio
    async def test_set_goals_creates_goal_with_targets(self):
        """setGoals creates a new Goal and returns Goal type with all target fields."""
        mutation = """
        mutation SetGoals($input: SetGoalsInput!) {
            setGoals(input: $input) {
                id
                uuid
                tenantId
                userId
                year
                eventTargetGoal
                consumerSamplingGoal
                brandAwarenessGoal
                purchaseIntentGoal
                femaleParticipationGoal
                firstTimeBuyersGoal
            }
        }
        """
        result = await self._execute_mutation(
            mutation,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "year": 2026,
                    "eventTargetGoal": 25,
                    "consumerSamplingGoal": 500,
                    "brandAwarenessGoal": 80.0,
                    "purchaseIntentGoal": 60.0,
                    "firstTimeBuyersGoal": 100,
                }
            },
            self.endpoint_path,
            user=self.client_user,
        )
        assert result.errors is None, result.errors
        data = result.data["setGoals"]
        assert data["year"] == 2026
        assert data["eventTargetGoal"] == 25
        assert data["consumerSamplingGoal"] == 500
        assert data["brandAwarenessGoal"] == 80.0
        assert data["purchaseIntentGoal"] == 60.0
        assert data["firstTimeBuyersGoal"] == 100
        assert data["femaleParticipationGoal"] is None

    @pytest.mark.asyncio
    async def test_set_goals_updates_existing_goal_partial(self):
        """setGoals with partial input updates only provided fields."""
        mutation = """
        mutation SetGoals($input: SetGoalsInput!) {
            setGoals(input: $input) {
                id year eventTargetGoal consumerSamplingGoal
            }
        }
        """
        await self._execute_mutation(
            mutation,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "year": 2025,
                    "eventTargetGoal": 30,
                }
            },
            self.endpoint_path,
            user=self.client_user,
        )
        await self._execute_mutation(
            mutation,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "year": 2025,
                    "consumerSamplingGoal": 600,
                }
            },
            self.endpoint_path,
            user=self.client_user,
        )
        result = await self._execute_query_authenticated(
            """
            query GetGoals($tenantId: ID!, $year: Int!) {
                goals(tenantId: $tenantId, year: $year) {
                    eventTargetGoal
                    consumerSamplingGoal
                }
            }
            """,
            {"tenantId": str(self.tenant.id), "year": 2025},
            self.client_user,
        )
        assert result.errors is None
        g = result.data["goals"]
        assert g["eventTargetGoal"] == 30
        assert g["consumerSamplingGoal"] == 600

    @pytest.mark.asyncio
    async def test_set_goals_requires_authentication(self):
        """setGoals returns error when not authenticated."""
        from django.contrib.auth.models import AnonymousUser

        mutation = """
        mutation SetGoals($input: SetGoalsInput!) {
            setGoals(input: $input) { id year }
        }
        """
        result = await self._execute_mutation(
            mutation,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "year": 2025,
                    "eventTargetGoal": 10,
                }
            },
            self.endpoint_path,
            user=AnonymousUser(),
        )
        assert result.errors is not None


@pytest.mark.django_db(transaction=True)
class TestEnqueueCreateGoalsForTenantMutation(DashboardGraphQLTestCase):
    """Tests for enqueueCreateGoalsForTenant and EnqueueCreateGoalsForTenantPayload."""

    @pytest.mark.asyncio
    @patch("tenants.dashboard.mutations.Queues")
    async def test_enqueue_create_goals_for_tenant_success(self, mock_queues_class):
        """Enqueues job and returns success + enqueued true."""
        mock_queue = MagicMock()
        mock_queues_class.return_value.default = mock_queue

        mutation = """
        mutation EnqueueCreateGoalsForTenant($tenantId: ID!, $year: Int!) {
            enqueueCreateGoalsForTenant(tenantId: $tenantId, year: $year) {
                success
                enqueued
            }
        }
        """
        result = await self._execute_mutation(
            mutation,
            {"tenantId": str(self.tenant.id), "year": 2025},
            self.endpoint_path,
            user=self.client_user,
        )
        assert result.errors is None
        payload = result.data["enqueueCreateGoalsForTenant"]
        assert payload["success"] is True
        assert payload["enqueued"] is True
        mock_queue.add.assert_called_once()
        call_args = mock_queue.add.call_args[0]
        assert call_args[0].__name__ == "create_goals_for_tenant"
        assert call_args[1] == self.tenant.id
        assert call_args[2] == 2025


@pytest.mark.django_db(transaction=True)
class TestEnqueueCreateGoalsForAllTenantsMutation(DashboardGraphQLTestCase):
    """Tests for enqueueCreateGoalsForAllTenants and EnqueueCreateGoalsForAllTenantsPayload."""

    @pytest.mark.asyncio
    @patch("tenants.dashboard.mutations.Queues")
    async def test_enqueue_create_goals_for_all_tenants_success(
        self, mock_queues_class
    ):
        """Enqueues one job (create_goals_for_all_tenants) and returns success + enqueued."""
        mock_queue = MagicMock()
        mock_queues_class.return_value.default = mock_queue

        mutation = """
        mutation EnqueueCreateGoalsForAllTenants($year: Int!) {
            enqueueCreateGoalsForAllTenants(year: $year) {
                success
                enqueued
            }
        }
        """
        result = await self._execute_mutation(
            mutation,
            {"year": 2025},
            self.endpoint_path,
            user=self.client_user,
        )
        assert result.errors is None
        payload = result.data["enqueueCreateGoalsForAllTenants"]
        assert payload["success"] is True
        assert payload["enqueued"] is True
        mock_queue.add.assert_called_once()
        call_args = mock_queue.add.call_args[0]
        assert call_args[0].__name__ == "create_goals_for_all_tenants"
        assert call_args[1] == 2025
