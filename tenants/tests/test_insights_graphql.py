"""
Tests for insights GraphQL queries.
"""
import pytest
from asgiref.sync import sync_to_async
from datetime import date, timedelta
from django.contrib.auth import get_user_model

from config.schema_spark import schema_spark
from tenants.models import Insights, InsightReport, Tenant
from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestInsightsGraphQL(BaseGraphQLTestCase):
    """GraphQL tests for insights queries."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up roles, tenant, and schema."""
        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        # Create tenant
        self.tenant = self.create_tenant(name="Test Tenant")

    async def _create_client_user(self) -> User:
        """Create a client user for testing."""
        return await sync_to_async(self.create_user)(
            username="client-insights",
            email="client-insights@test.com",
            role=self.roles["client"],
            password="password123",
        )

    async def _create_insights_with_reports(self, user: User) -> Insights:
        """Create Insights with reports for testing."""
        from_date = date.today() - timedelta(days=5)
        to_date = date.today()

        insights = await sync_to_async(Insights.objects.create)(
            tenant=self.tenant,
            from_date=from_date,
            to_date=to_date,
            total_feedback_count=10,
            created_by=user,
        )

        # Create reports
        report1 = await sync_to_async(InsightReport.objects.create)(
            insights=insights,
            title="High Priority Insight",
            content="This is a high priority insight.",
            priority="high",
            created_by=user,
        )

        report2 = await sync_to_async(InsightReport.objects.create)(
            insights=insights,
            title="Medium Priority Insight",
            content="This is a medium priority insight.",
            priority="medium",
            created_by=user,
        )

        report3 = await sync_to_async(InsightReport.objects.create)(
            insights=insights,
            title="Low Priority Insight",
            content="This is a low priority insight.",
            priority="low",
            created_by=user,
        )

        return insights

    @pytest.mark.asyncio
    async def test_latest_insights_returns_latest_for_tenant(self):
        """Test that latest_insights returns the most recent Insights."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )

        # Create multiple insights
        insights1 = await self._create_insights_with_reports(user)
        # Wait a moment to ensure different created_at
        import asyncio
        await asyncio.sleep(0.1)
        insights2 = await self._create_insights_with_reports(user)

        query = """
        query {
            latestInsights {
                id
                uuid
                tenantId
                fromDate
                toDate
                totalFeedbackCount
                createdAt
                reports {
                    id
                    title
                    content
                    priority
                    createdAt
                }
            }
        }
        """

        result = await self._execute_mutation(query, {}, self.endpoint_path, user=user)

        assert result.data is not None
        latest = result.data["latestInsights"]
        assert latest is not None
        assert latest["id"] == str(insights2.id)  # Should be the latest
        assert latest["totalFeedbackCount"] == 10
        assert len(latest["reports"]) == 3

    @pytest.mark.asyncio
    async def test_latest_insights_with_tenant_id(self):
        """Test latest_insights with explicit tenant_id parameter."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )

        insights = await self._create_insights_with_reports(user)

        query = """
        query GetLatestInsights($tenantId: ID) {
            latestInsights(tenantId: $tenantId) {
                id
                tenantId
                reports {
                    title
                    priority
                }
            }
        }
        """

        variables = {"tenantId": str(self.tenant.id)}

        result = await self._execute_mutation(query, variables, self.endpoint_path, user=user)

        assert result.data is not None
        latest = result.data["latestInsights"]
        assert latest is not None
        assert latest["tenantId"] == str(self.tenant.id)

    @pytest.mark.asyncio
    async def test_latest_insights_returns_none_when_no_insights(self):
        """Test that latest_insights returns None when no insights exist."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )

        query = """
        query {
            latestInsights {
                id
            }
        }
        """

        result = await self._execute_mutation(query, {}, self.endpoint_path, user=user)

        assert result.data is not None
        assert result.data["latestInsights"] is None

    @pytest.mark.asyncio
    async def test_latest_insights_requires_authentication(self):
        """Test that latest_insights requires authentication."""
        from django.contrib.auth.models import AnonymousUser

        query = """
        query {
            latestInsights {
                id
            }
        }
        """

        result = await self._execute_mutation(query, {}, self.endpoint_path, user=AnonymousUser())

        # Should have errors due to authentication requirement
        assert result.errors is not None

    @pytest.mark.asyncio
    async def test_latest_insights_reports_ordered_by_priority(self):
        """Test that reports are ordered by priority (high, medium, low)."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )

        insights = await self._create_insights_with_reports(user)

        query = """
        query {
            latestInsights {
                reports {
                    title
                    priority
                }
            }
        }
        """

        result = await self._execute_mutation(query, {}, self.endpoint_path, user=user)

        assert result.data is not None
        reports = result.data["latestInsights"]["reports"]
        assert len(reports) == 3
        # Should be ordered: high, medium, low
        assert reports[0]["priority"] == "high"
        assert reports[1]["priority"] == "medium"
        assert reports[2]["priority"] == "low"

    @pytest.mark.asyncio
    async def test_latest_insights_includes_all_report_fields(self):
        """Test that all InsightReport fields are returned."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )

        insights = await self._create_insights_with_reports(user)

        query = """
        query {
            latestInsights {
                id
                uuid
                tenantId
                fromDate
                toDate
                totalFeedbackCount
                createdAt
                reports {
                    id
                    uuid
                    title
                    content
                    priority
                    createdAt
                }
            }
        }
        """

        result = await self._execute_mutation(query, {}, self.endpoint_path, user=user)

        assert result.data is not None
        latest = result.data["latestInsights"]
        assert latest["id"] is not None
        assert latest["uuid"] is not None
        assert latest["tenantId"] == str(self.tenant.id)
        assert latest["fromDate"] is not None
        assert latest["toDate"] is not None
        assert latest["totalFeedbackCount"] == 10
        assert latest["createdAt"] is not None

        report = latest["reports"][0]
        assert report["id"] is not None
        assert report["uuid"] is not None
        assert report["title"] is not None
        assert report["content"] is not None
        assert report["priority"] in ["high", "medium", "low"]
        assert report["createdAt"] is not None
