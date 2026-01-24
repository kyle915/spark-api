"""
Tests for InsightsService.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date, timedelta
from django.utils import timezone

from tenants.models import Insights, InsightReport, Tenant
from tenants.insights.service import InsightsService
from recaps.models import Recap, ConsumerFeedback
from events.models import Event, EventType, EventStatus
from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db
class TestInsightsService(BaseGraphQLTestCase):
    """Tests for InsightsService."""

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

    @patch('google.generativeai.configure')
    def test_init_requires_gemini_api_key(self, mock_configure):
        """Test that InsightsService requires GEMINI_API_KEY in settings."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY=""):
            with pytest.raises(ValueError, match="GEMINI_API_KEY not configured"):
                InsightsService(self.tenant)

    @patch('google.generativeai.configure')
    def test_get_feedback_queryset_filters_by_tenant(self, mock_configure):
        """Test that _get_feedback_queryset filters by tenant."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            service = InsightsService(self.tenant)

            # Create ConsumerFeedback for this tenant
            feedback1 = ConsumerFeedback.objects.create(
                recap=self.recap,
                feedback="Test feedback 1",
                created_by=self.system_user,
            )

            # Create another tenant and feedback (should not be included)
            other_tenant = self.create_tenant(name="Other Tenant")
            other_event = Event.objects.create(
                name="Other Event",
                tenant=other_tenant,
                event_type=self.event_type,
                status=self.event_status,
                address="456 Other St",
                created_by=self.system_user,
            )
            other_recap = Recap.objects.create(
                name="Other Recap",
                event=other_event,
                created_by=self.system_user,
            )
            feedback2 = ConsumerFeedback.objects.create(
                recap=other_recap,
                feedback="Other feedback",
                created_by=self.system_user,
            )

            # Get queryset
            queryset = service._get_feedback_queryset()

            # Should only include feedback from this tenant
            assert feedback1 in queryset
            assert feedback2 not in queryset

    @patch('google.generativeai.configure')
    def test_get_feedback_queryset_filters_by_date_range(self, mock_configure):
        """Test that _get_feedback_queryset filters by date range."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            service = InsightsService(self.tenant)

            # Create feedback with different dates
            today = timezone.now().date()
            feedback_today = ConsumerFeedback.objects.create(
                recap=self.recap,
                feedback="Today feedback",
                created_by=self.system_user,
            )
            ConsumerFeedback.objects.filter(id=feedback_today.id).update(
                created_at=timezone.make_aware(
                    timezone.datetime.combine(today, timezone.datetime.min.time())
                )
            )

            feedback_old = ConsumerFeedback.objects.create(
                recap=self.recap,
                feedback="Old feedback",
                created_by=self.system_user,
            )
            old_date = today - timedelta(days=10)
            ConsumerFeedback.objects.filter(id=feedback_old.id).update(
                created_at=timezone.make_aware(
                    timezone.datetime.combine(old_date, timezone.datetime.min.time())
                )
            )

            # Get queryset with date range (last 5 days)
            from_date = today - timedelta(days=5)
            to_date = today
            queryset = service._get_feedback_queryset(from_date, to_date)

            # Should only include feedback from date range
            assert feedback_today in queryset
            assert feedback_old not in queryset

    @patch('google.generativeai.configure')
    def test_build_prompt_includes_feedback_data(self, mock_configure):
        """Test that _build_prompt includes all feedback fields."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            service = InsightsService(self.tenant)

            # Create feedback with all fields
            feedback = ConsumerFeedback.objects.create(
                recap=self.recap,
                feedback="Test feedback",
                quotes="Test quotes",
                positive_stories="Test positive stories",
                reasons_to_decline="Test reasons",
                created_by=self.system_user,
            )

            prompt = service._build_prompt([feedback])

            assert "Test feedback" in prompt
            assert "Test quotes" in prompt
            assert "Test positive stories" in prompt
            assert "Test reasons" in prompt
            assert "1-4" in prompt and "insight" in prompt.lower()
            assert "high" in prompt.lower() or "medium" in prompt.lower() or "low" in prompt.lower()

    @patch('google.generativeai.GenerativeModel')
    @patch('google.generativeai.list_models')
    @patch('google.generativeai.configure')
    def test_call_gemini_api_success(self, mock_configure, mock_list_models, mock_model_class):
        """Test successful Gemini API call."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            service = InsightsService(self.tenant)

            # Mock Gemini response
            mock_model = MagicMock()
            mock_response = MagicMock()
            mock_response.text = '{"insights": [{"title": "Test Insight", "content": "Test content", "priority": "high"}]}'
            mock_model.generate_content.return_value = mock_response
            mock_model_class.return_value = mock_model
            mock_list_models.return_value = [
                MagicMock(name="models/gemini-pro", supported_generation_methods=["generateContent"])
            ]

            result = service._call_gemini_api("Test prompt")

            assert "insights" in result
            assert len(result["insights"]) == 1
            assert result["insights"][0]["title"] == "Test Insight"

    @patch('google.generativeai.configure')
    def test_parse_insights_response_validates_structure(self, mock_configure):
        """Test that _parse_insights_response validates response structure."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            service = InsightsService(self.tenant)

            # Test missing insights key
            with pytest.raises(ValueError, match="missing 'insights' key"):
                service._parse_insights_response({})

            # Test invalid insights type
            with pytest.raises(ValueError, match="must be a list"):
                service._parse_insights_response({"insights": "not a list"})

            # Test too many insights
            with pytest.raises(ValueError, match="Expected 1-4 insights"):
                service._parse_insights_response({
                    "insights": [
                        {"title": f"Insight {i}", "content": "Content", "priority": "high"}
                        for i in range(5)
                    ]
                })

            # Test missing required fields
            with pytest.raises(ValueError, match="missing required field"):
                service._parse_insights_response({
                    "insights": [{"title": "Test"}]  # Missing content and priority
                })

            # Test invalid priority
            with pytest.raises(ValueError, match="invalid priority"):
                service._parse_insights_response({
                    "insights": [{
                        "title": "Test",
                        "content": "Content",
                        "priority": "invalid"
                    }]
                })

    @patch('google.generativeai.configure')
    def test_parse_insights_response_success(self, mock_configure):
        """Test successful parsing of insights response."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            service = InsightsService(self.tenant)

            response = {
                "insights": [
                    {
                        "title": "High Priority Insight",
                        "content": "This is a high priority insight.",
                        "priority": "high"
                    },
                    {
                        "title": "Medium Priority Insight",
                        "content": "This is a medium priority insight.",
                        "priority": "medium"
                    },
                    {
                        "title": "Low Priority Insight",
                        "content": "This is a low priority insight.",
                        "priority": "low"
                    }
                ]
            }

            parsed = service._parse_insights_response(response)

            assert len(parsed) == 3
            assert parsed[0]["title"] == "High Priority Insight"
            assert parsed[0]["priority"] == "high"
            assert parsed[1]["priority"] == "medium"
            assert parsed[2]["priority"] == "low"

    @patch('google.generativeai.configure')
    def test_generate_insights_no_feedback(self, mock_configure):
        """Test that generate_insights raises error when no feedback exists."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            service = InsightsService(self.tenant)

            with pytest.raises(ValueError, match="No ConsumerFeedback records found"):
                service.generate_insights()

    @patch('google.generativeai.GenerativeModel')
    @patch('google.generativeai.list_models')
    @patch('google.generativeai.configure')
    def test_generate_insights_success(self, mock_configure, mock_list_models, mock_model_class):
        """Test successful insights generation."""
        from django.conf import settings
        from django.test import override_settings

        with override_settings(GEMINI_API_KEY="test-key"):
            service = InsightsService(self.tenant)

            # Create feedback
            feedback = ConsumerFeedback.objects.create(
                recap=self.recap,
                feedback="Test feedback",
                quotes="Test quotes",
                created_by=self.system_user,
            )

            # Mock Gemini API
            mock_model = MagicMock()
            mock_response = MagicMock()
            mock_response.text = '''{
                "insights": [
                    {"title": "Test Insight 1", "content": "Content 1", "priority": "high"},
                    {"title": "Test Insight 2", "content": "Content 2", "priority": "medium"}
                ]
            }'''
            mock_model.generate_content.return_value = mock_response
            mock_model_class.return_value = mock_model
            mock_list_models.return_value = [
                MagicMock(name="models/gemini-pro", supported_generation_methods=["generateContent"])
            ]

            # Generate insights
            insights = service.generate_insights(created_by=self.system_user)

            # Verify Insights was created
            assert insights is not None
            assert insights.tenant == self.tenant
            assert insights.total_feedback_count == 1
            assert insights.reports.count() == 2

            # Verify reports were created
            reports = list(insights.reports.all())
            assert len(reports) == 2
            assert reports[0].title == "Test Insight 1"
            assert reports[0].priority == "high"
            assert reports[1].title == "Test Insight 2"
            assert reports[1].priority == "medium"
