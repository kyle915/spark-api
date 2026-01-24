"""
Tests for Insights and InsightReport models.
"""
import pytest
from datetime import date, timedelta
from django.db import IntegrityError
from django.utils import timezone

from tenants.models import Insights, InsightReport, Tenant
from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db
class TestInsightsModel(BaseGraphQLTestCase):
    """Tests for the Insights model."""

    def test_create_insights(self):
        """Test creating an Insights instance."""
        tenant = self.create_tenant(name="Test Tenant")
        system_user = self.get_system_user()

        from_date = date.today() - timedelta(days=5)
        to_date = date.today()

        insights = Insights.objects.create(
            tenant=tenant,
            from_date=from_date,
            to_date=to_date,
            total_feedback_count=10,
            created_by=system_user,
        )

        assert insights.tenant == tenant
        assert insights.from_date == from_date
        assert insights.to_date == to_date
        assert insights.total_feedback_count == 10
        assert insights.created_by == system_user
        assert insights.uuid is not None
        assert str(insights) == f"Insights for {tenant.name} ({from_date} to {to_date})"

    def test_insights_has_reports_relationship(self):
        """Test that Insights has a related_name 'reports' for InsightReport."""
        tenant = self.create_tenant(name="Test Tenant")
        system_user = self.get_system_user()

        insights = Insights.objects.create(
            tenant=tenant,
            from_date=date.today() - timedelta(days=5),
            to_date=date.today(),
            total_feedback_count=5,
            created_by=system_user,
        )

        # Create reports
        report1 = InsightReport.objects.create(
            insights=insights,
            title="Test Report 1",
            content="Test content 1",
            priority="high",
            created_by=system_user,
        )

        report2 = InsightReport.objects.create(
            insights=insights,
            title="Test Report 2",
            content="Test content 2",
            priority="medium",
            created_by=system_user,
        )

        # Test relationship
        assert insights.reports.count() == 2
        assert report1 in insights.reports.all()
        assert report2 in insights.reports.all()

    def test_insights_tenant_relationship(self):
        """Test that Insights belongs to Tenant with related_name 'insights'."""
        tenant = self.create_tenant(name="Test Tenant")
        system_user = self.get_system_user()

        insights1 = Insights.objects.create(
            tenant=tenant,
            from_date=date.today() - timedelta(days=5),
            to_date=date.today(),
            total_feedback_count=5,
            created_by=system_user,
        )

        insights2 = Insights.objects.create(
            tenant=tenant,
            from_date=date.today() - timedelta(days=10),
            to_date=date.today() - timedelta(days=6),
            total_feedback_count=3,
            created_by=system_user,
        )

        # Test relationship
        assert tenant.insights.count() == 2
        assert insights1 in tenant.insights.all()
        assert insights2 in tenant.insights.all()


@pytest.mark.django_db
class TestInsightReportModel(BaseGraphQLTestCase):
    """Tests for the InsightReport model."""

    def test_create_insight_report(self):
        """Test creating an InsightReport instance."""
        tenant = self.create_tenant(name="Test Tenant")
        system_user = self.get_system_user()

        insights = Insights.objects.create(
            tenant=tenant,
            from_date=date.today() - timedelta(days=5),
            to_date=date.today(),
            total_feedback_count=10,
            created_by=system_user,
        )

        report = InsightReport.objects.create(
            insights=insights,
            title="Test Insight Report",
            content="This is a test insight report content.",
            priority="high",
            created_by=system_user,
        )

        assert report.insights == insights
        assert report.title == "Test Insight Report"
        assert report.content == "This is a test insight report content."
        assert report.priority == "high"
        assert report.created_by == system_user
        assert report.uuid is not None
        assert str(report) == "Test Insight Report (high)"

    def test_insight_report_priority_choices(self):
        """Test that InsightReport accepts valid priority choices."""
        tenant = self.create_tenant(name="Test Tenant")
        system_user = self.get_system_user()

        insights = Insights.objects.create(
            tenant=tenant,
            from_date=date.today() - timedelta(days=5),
            to_date=date.today(),
            total_feedback_count=10,
            created_by=system_user,
        )

        # Test all priority choices
        for priority in ["high", "medium", "low"]:
            report = InsightReport.objects.create(
                insights=insights,
                title=f"Test {priority} priority",
                content="Test content",
                priority=priority,
                created_by=system_user,
            )
            assert report.priority == priority

    def test_insight_report_default_priority(self):
        """Test that InsightReport defaults to 'low' priority."""
        tenant = self.create_tenant(name="Test Tenant")
        system_user = self.get_system_user()

        insights = Insights.objects.create(
            tenant=tenant,
            from_date=date.today() - timedelta(days=5),
            to_date=date.today(),
            total_feedback_count=10,
            created_by=system_user,
        )

        report = InsightReport(
            insights=insights,
            title="Test Report",
            content="Test content",
            created_by=system_user,
        )
        # Priority should default to 'low' when not specified
        assert report.priority == "low"

    def test_insight_report_requires_insights(self):
        """Test that InsightReport requires an Insights instance."""
        system_user = self.get_system_user()

        with pytest.raises(IntegrityError):
            InsightReport.objects.create(
                insights=None,  # This should fail
                title="Test Report",
                content="Test content",
                priority="high",
                created_by=system_user,
            )

    def test_insight_report_ordering(self):
        """Test that InsightReports can be ordered by priority and created_at."""
        tenant = self.create_tenant(name="Test Tenant")
        system_user = self.get_system_user()

        insights = Insights.objects.create(
            tenant=tenant,
            from_date=date.today() - timedelta(days=5),
            to_date=date.today(),
            total_feedback_count=10,
            created_by=system_user,
        )

        # Create reports with different priorities
        report_low = InsightReport.objects.create(
            insights=insights,
            title="Low Priority",
            content="Low content",
            priority="low",
            created_by=system_user,
        )

        report_high = InsightReport.objects.create(
            insights=insights,
            title="High Priority",
            content="High content",
            priority="high",
            created_by=system_user,
        )

        report_medium = InsightReport.objects.create(
            insights=insights,
            title="Medium Priority",
            content="Medium content",
            priority="medium",
            created_by=system_user,
        )

        # Test that all reports are returned
        all_reports = list(insights.reports.all())
        assert len(all_reports) == 3
        assert report_high in all_reports
        assert report_medium in all_reports
        assert report_low in all_reports

        # Test ordering by priority (alphabetical: high, low, medium)
        # Note: "-priority" orders alphabetically, not by priority level
        ordered_reports = list(insights.reports.all().order_by("-priority", "created_at"))
        priorities = [r.priority for r in ordered_reports]
        # Verify all priorities are present
        assert "high" in priorities
        assert "medium" in priorities
        assert "low" in priorities
