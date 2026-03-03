"""
Tests for generateInsights GraphQL mutation.

Covers sync success, enqueue, no feedback error, permissions, and tenant resolution.
"""
import pytest
from asgiref.sync import sync_to_async
from datetime import date, timedelta
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from django.test import override_settings

from config.schema_spark import schema_spark
from tenants.models import Insights
from tenants.tests.base import BaseGraphQLTestCase
from recaps.models import Recap, ConsumerFeedback
from events.models import Event, EventType, EventStatus

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestGenerateInsightsMutation(BaseGraphQLTestCase):
    """GraphQL tests for generateInsights mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up roles, tenant, schema, and optional event/recap/feedback."""
        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.tenant = self.create_tenant(name="Test Tenant")

    async def _create_client_user(self) -> User:
        """Create a client user for testing."""
        return await sync_to_async(self.create_user)(
            username="client-gen",
            email="client-gen@test.com",
            role=self.roles["client"],
            password="password123",
        )

    async def _create_spark_admin_user(self) -> User:
        """Create a Spark admin user for testing."""
        return await sync_to_async(self.create_user)(
            username="spark-gen",
            email="spark-gen@test.com",
            role=self.roles["spark_admin"],
            password="password123",
        )

    def _create_event_and_recap_with_feedback(self):
        """Create event, recap, and ConsumerFeedback for the tenant."""
        system_user = self.get_system_user()
        event_type = EventType.objects.create(
            name="Test Type",
            tenant=self.tenant,
            created_by=system_user,
        )
        event_status = EventStatus.objects.create(
            name="Approved",
            tenant=self.tenant,
            created_by=system_user,
        )
        event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=event_type,
            status=event_status,
            address="123 Test St",
            created_by=system_user,
        )
        recap = Recap.objects.create(
            name="Test Recap",
            event=event,
            created_by=system_user,
        )
        ConsumerFeedback.objects.create(
            recap=recap,
            feedback="Test feedback",
            quotes="Test quotes",
            created_by=system_user,
        )
        return event, recap

    @pytest.mark.asyncio
    @override_settings(GEMINI_API_KEY="test-key")
    @patch("google.generativeai.GenerativeModel")
    @patch("google.generativeai.list_models")
    @patch("google.generativeai.configure")
    async def test_generate_insights_sync_success(
        self, mock_configure, mock_list_models, mock_model_class
    ):
        """Authenticated user calls generateInsights without enqueue; new Insights created."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )
        await sync_to_async(self._create_event_and_recap_with_feedback)()

        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = """{
            "insights": [
                {"title": "Test Insight 1", "content": "Content 1", "priority": "high"},
                {"title": "Test Insight 2", "content": "Content 2", "priority": "medium"}
            ]
        }"""
        mock_model.generate_content.return_value = mock_response
        mock_model_class.return_value = mock_model
        mock_list_models.return_value = [
            MagicMock(
                name="models/gemini-pro",
                supported_generation_methods=["generateContent"],
            )
        ]

        mutation = """
        mutation GenerateInsights {
            generateInsights {
                success
                enqueued
                insights {
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
        }
        """
        result = await self._execute_mutation(
            mutation, {}, self.endpoint_path, user=user
        )

        assert result.errors is None, result.errors
        assert result.data is not None
        payload = result.data["generateInsights"]
        assert payload["success"] is True
        assert payload["enqueued"] is False
        assert payload["insights"] is not None
        assert payload["insights"]["tenantId"] == str(self.tenant.id)
        assert payload["insights"]["totalFeedbackCount"] == 1
        assert len(payload["insights"]["reports"]) == 2

        # Verify DB: new Insights record created
        count = await sync_to_async(Insights.objects.filter(tenant=self.tenant).count)()
        assert count == 1

    @pytest.mark.asyncio
    @patch("tenants.dashboard.mutations.Queues")
    async def test_generate_insights_enqueue(self, mock_queues_class):
        """Call with enqueue: true; job enqueued, response enqueued true, no insights."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )

        mock_queue = MagicMock()
        mock_queues_class.return_value.default = mock_queue

        mutation = """
        mutation GenerateInsightsEnqueue {
            generateInsights(enqueue: true) {
                success
                enqueued
                insights {
                    id
                }
            }
        }
        """
        result = await self._execute_mutation(
            mutation, {}, self.endpoint_path, user=user
        )

        assert result.errors is None, result.errors
        assert result.data is not None
        payload = result.data["generateInsights"]
        assert payload["success"] is True
        assert payload["enqueued"] is True
        assert payload["insights"] is None

        mock_queue.add.assert_called_once()
        call_args = mock_queue.add.call_args
        assert call_args[0][0].__name__ == "generate_insights_for_tenant"
        assert call_args[0][1] == self.tenant.id

    @pytest.mark.asyncio
    @override_settings(GEMINI_API_KEY="test-key")
    @patch("google.generativeai.configure")
    async def test_generate_insights_no_feedback(self, mock_configure):
        """Tenant has no feedback in date range; graceful error."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )
        # No event/recap/ConsumerFeedback created

        mutation = """
        mutation GenerateInsightsNoFeedback {
            generateInsights {
                success
                enqueued
                insights { id }
            }
        }
        """
        result = await self._execute_mutation(
            mutation, {}, self.endpoint_path, user=user
        )

        assert result.errors is not None
        assert any(
            "ConsumerFeedback" in str(e) or "feedback" in str(e).lower()
            for e in result.errors
        )

    @pytest.mark.asyncio
    async def test_generate_insights_requires_authentication(self):
        """Unauthenticated request returns error."""
        from django.contrib.auth.models import AnonymousUser

        mutation = """
        mutation GenerateInsightsAnon {
            generateInsights {
                success
                enqueued
            }
        }
        """
        result = await self._execute_mutation(
            mutation, {}, self.endpoint_path, user=AnonymousUser()
        )

        assert result.errors is not None

    @pytest.mark.asyncio
    async def test_generate_insights_tenant_resolution_uses_user_tenant(self):
        """With no tenantId, use current user's tenant."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )

        mock_queue = MagicMock()
        with patch("tenants.dashboard.mutations.Queues") as mock_queues_class:
            mock_queues_class.return_value.default = mock_queue

            mutation = """
            mutation GenerateInsightsNoTenantId {
                generateInsights(enqueue: true) {
                    success
                    enqueued
                }
            }
            """
            result = await self._execute_mutation(
                mutation, {}, self.endpoint_path, user=user
            )

        assert result.errors is None, result.errors
        assert result.data["generateInsights"]["success"] is True
        mock_queue.add.assert_called_once()
        assert mock_queue.add.call_args[0][1] == self.tenant.id

    @pytest.mark.asyncio
    async def test_generate_insights_tenant_member_cannot_generate_for_other_tenant(self):
        """Tenant member can only generate for their own tenant."""
        user = await self._create_client_user()
        await sync_to_async(self.create_tenanted_user)(
            user=user, tenant=self.tenant, is_active=True
        )
        other_tenant = await sync_to_async(self.create_tenant)(name="Other Tenant")

        mutation = """
        mutation GenerateInsightsOtherTenant($tenantId: ID!) {
            generateInsights(tenantId: $tenantId, enqueue: true) {
                success
                enqueued
            }
        }
        """
        result = await self._execute_mutation(
            mutation,
            {"tenantId": str(other_tenant.id)},
            self.endpoint_path,
            user=user,
        )

        assert result.errors is not None
        assert any(
            "permission" in str(e).lower() or "tenant" in str(e).lower()
            for e in result.errors
        )

    @pytest.mark.asyncio
    async def test_generate_insights_spark_admin_can_generate_for_any_tenant(self):
        """Spark admin can generate for a specific tenant by tenantId."""
        spark_user = await self._create_spark_admin_user()
        await sync_to_async(self.create_tenanted_user)(
            user=spark_user, tenant=self.tenant, is_active=True
        )
        other_tenant = await sync_to_async(self.create_tenant)(name="Other Tenant")

        mock_queue = MagicMock()
        with patch("tenants.dashboard.mutations.Queues") as mock_queues_class:
            mock_queues_class.return_value.default = mock_queue

            mutation = """
            mutation GenerateInsightsSparkAdmin($tenantId: ID!) {
                generateInsights(tenantId: $tenantId, enqueue: true) {
                    success
                    enqueued
                }
            }
            """
            result = await self._execute_mutation(
                mutation,
                {"tenantId": str(other_tenant.id)},
                self.endpoint_path,
                user=spark_user,
            )

        assert result.errors is None, result.errors
        assert result.data["generateInsights"]["success"] is True
        mock_queue.add.assert_called_once()
        assert mock_queue.add.call_args[0][1] == other_tenant.id
