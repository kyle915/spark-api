"""
Tests for insights management commands.
"""
import pytest
from unittest.mock import patch, MagicMock, call
from datetime import date, timedelta
from django.core.management import call_command
from django.core.management.base import CommandError
from io import StringIO
from django.utils import timezone

from tenants.models import Insights, InsightReport, Tenant
from recaps.models import Recap, ConsumerFeedback
from events.models import Event, EventType, EventStatus
from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db
class TestGenerateInsightsCommand(BaseGraphQLTestCase):
    """Tests for generate_insights management command."""

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

    @patch('google.generativeai.GenerativeModel')
    @patch('google.generativeai.list_models')
    @patch('google.generativeai.configure')
    def test_generate_insights_single_tenant_success(self, mock_configure, mock_list_models, mock_model_class):
        """Test generating insights for a single tenant."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            # Mock Gemini API
            mock_model = MagicMock()
            mock_response = MagicMock()
            mock_response.text = '''{
                "insights": [
                    {"title": "Test Insight", "content": "Content", "priority": "high"}
                ]
            }'''
            mock_model.generate_content.return_value = mock_response
            mock_model_class.return_value = mock_model
            mock_list_models.return_value = [
                MagicMock(name="models/gemini-pro", supported_generation_methods=["generateContent"])
            ]

            out = StringIO()
            call_command(
                'generate_insights',
                '--tenant-id', str(self.tenant.id),
                stdout=out
            )

            output = out.getvalue()
            assert "Success!" in output
            assert str(self.tenant.id) in output
            assert "Insights ID" in output

            # Verify insights were created
            assert Insights.objects.filter(tenant=self.tenant).exists()
            insights = Insights.objects.filter(tenant=self.tenant).first()
            assert insights.reports.count() == 1

    def test_generate_insights_tenant_not_found(self):
        """Test error when tenant doesn't exist."""
        out = StringIO()
        err = StringIO()

        with pytest.raises(CommandError, match="does not exist"):
            call_command(
                'generate_insights',
                '--tenant-id', '99999',
                stdout=out,
                stderr=err
            )

    def test_generate_insights_missing_arguments(self):
        """Test error when neither tenant-id nor all-tenants is provided."""
        out = StringIO()
        err = StringIO()

        with pytest.raises(CommandError, match="Either --tenant-id or --all-tenants"):
            call_command(
                'generate_insights',
                stdout=out,
                stderr=err
            )

    def test_generate_insights_conflicting_arguments(self):
        """Test error when both tenant-id and all-tenants are provided."""
        out = StringIO()
        err = StringIO()

        with pytest.raises(CommandError, match="Cannot specify both"):
            call_command(
                'generate_insights',
                '--tenant-id', str(self.tenant.id),
                '--all-tenants',
                stdout=out,
                stderr=err
            )

    def test_generate_insights_invalid_date_format(self):
        """Test error when date format is invalid."""
        out = StringIO()
        err = StringIO()

        with pytest.raises(CommandError, match="Invalid --from-date format"):
            call_command(
                'generate_insights',
                '--tenant-id', str(self.tenant.id),
                '--from-date', 'invalid-date',
                stdout=out,
                stderr=err
            )

    @patch('tenants.management.commands.generate_insights.Queues')
    def test_generate_insights_enqueue(self, mock_queues_class):
        """Test enqueuing insights generation task."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        out = StringIO()
        call_command(
            'generate_insights',
            '--tenant-id', str(self.tenant.id),
            '--enqueue',
            stdout=out
        )

        output = out.getvalue()
        assert "enqueued" in output.lower()
        mock_queues.default.add.assert_called_once()


@pytest.mark.django_db
class TestGenerateConsumerFeedbackCommand(BaseGraphQLTestCase):
    """Tests for generate_consumer_feedback management command."""

    def setup_method(self):
        """Set up test data."""
        self.tenant = self.create_tenant(name="Test Tenant")
        self.system_user = self.get_system_user()

    def test_generate_consumer_feedback_success(self):
        """Test successful generation of ConsumerFeedback records."""
        out = StringIO()
        call_command(
            'generate_consumer_feedback',
            '--tenant-id', str(self.tenant.id),
            '--total-to-create', '5',
            stdout=out
        )

        output = out.getvalue()
        assert "Created Event" in output
        assert "Created Recap" in output
        assert "Created 5 ConsumerFeedback records" in output

        # Verify records were created
        assert Event.objects.filter(tenant=self.tenant).exists()
        event = Event.objects.filter(tenant=self.tenant).first()
        assert Recap.objects.filter(event=event).exists()
        recap = Recap.objects.filter(event=event).first()
        assert ConsumerFeedback.objects.filter(recap=recap).count() == 5

    def test_generate_consumer_feedback_tenant_not_found(self):
        """Test error when tenant doesn't exist."""
        out = StringIO()
        err = StringIO()

        with pytest.raises(CommandError, match="does not exist"):
            call_command(
                'generate_consumer_feedback',
                '--tenant-id', '99999',
                '--total-to-create', '5',
                stdout=out,
                stderr=err
            )

    def test_generate_consumer_feedback_creates_event_and_recap(self):
        """Test that command creates Event and Recap."""
        out = StringIO()
        call_command(
            'generate_consumer_feedback',
            '--tenant-id', str(self.tenant.id),
            '--total-to-create', '3',
            stdout=out
        )

        # Verify Event was created
        events = Event.objects.filter(tenant=self.tenant)
        assert events.count() == 1
        event = events.first()
        assert event.name in [
            "Summer Product Launch Event",
            "Holiday Sampling Campaign",
            "Spring Brand Awareness Event",
            "Community Engagement Fair",
            "Retail Store Demo Day",
        ]

        # Verify Recap was created
        assert Recap.objects.filter(event=event).exists()
        recap = Recap.objects.filter(event=event).first()
        assert "Recap for" in recap.name

    def test_generate_consumer_feedback_creates_feedback_with_date_range(self):
        """Test that ConsumerFeedback records are created within date range."""
        out = StringIO()
        call_command(
            'generate_consumer_feedback',
            '--tenant-id', str(self.tenant.id),
            '--total-to-create', '10',
            stdout=out
        )

        event = Event.objects.filter(tenant=self.tenant).first()
        recap = Recap.objects.filter(event=event).first()
        feedbacks = ConsumerFeedback.objects.filter(recap=recap)

        # Check that all feedbacks have created_at within last 5 days
        today = timezone.now().date()
        from_date = today - timedelta(days=5)

        for feedback in feedbacks:
            feedback_date = feedback.created_at.date()
            assert from_date <= feedback_date <= today

    def test_generate_consumer_feedback_uses_sample_data(self):
        """Test that ConsumerFeedback uses sample data from the dictionary."""
        out = StringIO()
        call_command(
            'generate_consumer_feedback',
            '--tenant-id', str(self.tenant.id),
            '--total-to-create', '30',  # More than available samples
            stdout=out
        )

        event = Event.objects.filter(tenant=self.tenant).first()
        recap = Recap.objects.filter(event=event).first()
        feedbacks = ConsumerFeedback.objects.filter(recap=recap)

        # Verify feedbacks have content (should use sample data)
        for feedback in feedbacks:
            # At least one field should have content
            assert (
                feedback.feedback or
                feedback.quotes or
                feedback.positive_stories or
                feedback.reasons_to_decline
            )
