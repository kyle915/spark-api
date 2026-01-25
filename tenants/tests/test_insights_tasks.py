"""
Tests for insights generation RQ tasks.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date, timedelta
from django.utils import timezone

from tenants.models import Insights, InsightReport, Tenant
from tenants.insights.tasks import (
    generate_insights_for_tenant,
    generate_insights_for_all_tenants,
)
from recaps.models import Recap, ConsumerFeedback
from events.models import Event, EventType, EventStatus
from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db
class TestInsightsTasks(BaseGraphQLTestCase):
    """Tests for insights generation RQ tasks."""

    def setup_method(self):
        """Set up test data."""
        self.tenant = self.create_tenant(name="Test Tenant")
        self.system_user = self.get_system_user()

        # Create event type and status
        self.event_type = EventType.objects.create(
            name="Test Type",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.event_status = EventStatus.objects.create(
            name="Approved",
            tenant=self.tenant,
            created_by=self.system_user,
        )

        # Create event
        self.event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            created_by=self.system_user,
        )

        # Create recap
        self.recap = Recap.objects.create(
            name="Test Recap",
            event=self.event,
            created_by=self.system_user,
        )

        # Create feedback
        self.feedback = ConsumerFeedback.objects.create(
            recap=self.recap,
            feedback="Test feedback",
            quotes="Test quotes",
            created_by=self.system_user,
        )

    @patch('tenants.insights.tasks.InsightsService')
    def test_generate_insights_for_tenant_success(self, mock_service_class):
        """Test successful insights generation for a tenant."""
        # Mock service
        mock_service = MagicMock()
        mock_insights = MagicMock()
        mock_insights.id = 1
        mock_insights.total_feedback_count = 1
        mock_service.generate_insights.return_value = mock_insights
        mock_service_class.return_value = mock_service

        # Execute task
        result = generate_insights_for_tenant(self.tenant.id)

        # Verify service was called
        mock_service_class.assert_called_once()
        mock_service.generate_insights.assert_called_once()
        assert result is None  # Task doesn't return value

    @patch('tenants.insights.tasks.InsightsService')
    def test_generate_insights_for_tenant_with_date_range(self, mock_service_class):
        """Test insights generation with custom date range."""
        mock_service = MagicMock()
        mock_insights = MagicMock()
        mock_insights.id = 1
        mock_insights.total_feedback_count = 1
        mock_service.generate_insights.return_value = mock_insights
        mock_service_class.return_value = mock_service

        from_date = date.today() - timedelta(days=7)
        to_date = date.today()

        result = generate_insights_for_tenant(
            self.tenant.id, from_date=from_date, to_date=to_date
        )

        mock_service.generate_insights.assert_called_once_with(
            from_date=from_date, to_date=to_date
        )
        assert result is None

    def test_generate_insights_for_tenant_tenant_not_found(self):
        """Test that task raises error when tenant doesn't exist."""
        with pytest.raises(Tenant.DoesNotExist):
            generate_insights_for_tenant(99999)

    @patch('tenants.insights.tasks.InsightsService')
    def test_generate_insights_for_tenant_no_feedback(self, mock_service_class):
        """Test that task handles ValueError when no feedback exists."""
        from tenants.insights.service import InsightsService

        # Mock service to raise ValueError
        mock_service = MagicMock()
        mock_service.generate_insights.side_effect = ValueError("No feedback found")
        mock_service_class.return_value = mock_service

        # Task should return None (not raise) when no feedback
        result = generate_insights_for_tenant(self.tenant.id)

        assert result is None

    @patch('tenants.insights.tasks.Queues')
    def test_generate_insights_for_all_tenants(self, mock_queues_class):
        """Test generating insights for all tenants."""
        # Create another tenant
        tenant2 = self.create_tenant(name="Test Tenant 2")

        # Mock Queues
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Execute task
        result = generate_insights_for_all_tenants()

        # Verify jobs were enqueued for both tenants
        assert mock_queues.default.add.call_count == 2
        calls = mock_queues.default.add.call_args_list
        # All calls should target generate_insights_for_tenant
        assert all(
            call[0][0] == generate_insights_for_tenant for call in calls
        )
        assert result is None

    @patch('tenants.insights.tasks.Queues')
    def test_generate_insights_for_all_tenants_with_date_range(self, mock_queues_class):
        """Test generating insights for all tenants with custom date range."""
        # Create another tenant
        tenant2 = self.create_tenant(name="Test Tenant 2")

        # Mock Queues
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        from_date = date.today() - timedelta(days=7)
        to_date = date.today()

        result = generate_insights_for_all_tenants(
            from_date=from_date, to_date=to_date
        )

        # Verify date range was passed to enqueued tasks
        calls = mock_queues.default.add.call_args_list
        for call in calls:
            # Check that from_date and to_date are in the call arguments
            args = call[0]
            assert args[2] == from_date  # Third argument is from_date
            assert args[3] == to_date  # Fourth argument is to_date

        assert result is None
