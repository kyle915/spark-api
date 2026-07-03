"""
Tests for dashboard goals RQ tasks.

Covers create_goals_for_tenant and create_goals_for_all_tenants.
Includes scale tests (20+ users, 20+ tenants) for performance assurance.
"""
from unittest.mock import MagicMock, patch

import pytest

# Minimum scale for performance/scale tests
SCALE_TEST_MIN_USERS = 20
SCALE_TEST_MIN_TENANTS = 20

from tenants.dashboard.tasks import (
    create_goals_for_all_tenants,
    create_goals_for_tenant,
)
from tenants.dashboard.tests.base import DashboardGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestCreateGoalsForTenant(DashboardGraphQLTestCase):
    """Tests for create_goals_for_tenant task."""

    @patch("tenants.dashboard.tasks.ensure_goals_for_tenant_users")
    def test_create_goals_for_tenant_success_returns_created_count(
        self, mock_ensure
    ):
        """Task calls service and returns the number of goals created."""
        mock_ensure.return_value = 5
        result = create_goals_for_tenant(self.tenant.id, 2025)
        mock_ensure.assert_called_once_with(self.tenant.id, 2025)
        assert result == 5

    @patch("tenants.dashboard.tasks.ensure_goals_for_tenant_users")
    def test_create_goals_for_tenant_zero_created(self, mock_ensure):
        """Task returns 0 when service creates no new goals (idempotent)."""
        mock_ensure.return_value = 0
        result = create_goals_for_tenant(self.tenant.id, 2025)
        assert result == 0

    @patch("tenants.dashboard.tasks.ensure_goals_for_tenant_users")
    def test_create_goals_for_tenant_logs_on_success(self, mock_ensure):
        """Task logs info when goals are created."""
        mock_ensure.return_value = 3
        with patch("tenants.dashboard.tasks.logger") as mock_logger:
            create_goals_for_tenant(self.tenant.id, 2026)
            mock_logger.info.assert_called_once()
            call_msg = mock_logger.info.call_args[0][0]
            assert "3" in call_msg
            assert str(self.tenant.id) in call_msg
            assert "2026" in call_msg

    @patch("tenants.dashboard.tasks.ensure_goals_for_tenant_users")
    def test_create_goals_for_tenant_on_exception_logs_and_reraises(
        self, mock_ensure
    ):
        """Task logs error and re-raises when service raises."""
        mock_ensure.side_effect = ValueError("Service error")
        with patch("tenants.dashboard.tasks.logger") as mock_logger:
            with pytest.raises(ValueError, match="Service error"):
                create_goals_for_tenant(self.tenant.id, 2025)
            mock_logger.error.assert_called_once()
            call_msg = mock_logger.error.call_args[0][0]
            assert "Error creating goals" in call_msg
            assert str(self.tenant.id) in call_msg

    def test_create_goals_for_tenant_integration_creates_goals(self):
        """Task actually creates goals when called with real service (no mock)."""
        result = create_goals_for_tenant(self.tenant.id, 2025)
        assert result >= 0
        from tenants.models import Goal

        assert Goal.objects.filter(
            tenant_id=self.tenant.id, year=2025
        ).count() >= result

    def test_create_goals_for_tenant_performance_20_users(self):
        """Task creates goals for a tenant with 20+ users (bulk path, no mocks)."""
        from tenants.models import Goal

        large_tenant = self.create_tenant(name="Large Tenant")
        # Tenant post_save auto-links existing spark admins (prod behavior);
        # the exact-count assertions below must reflect only the users this
        # test creates, so drop any pre-linked memberships.
        large_tenant.tenanted_users.all().delete()
        role = self.roles["client"]
        num_users = SCALE_TEST_MIN_USERS
        for i in range(num_users):
            user = self.create_user(
                username=f"scaleuser{i}@test.com",
                email=f"scaleuser{i}@test.com",
                role=role,
                password="testpass123",
            )
            self.create_tenanted_user(user=user, tenant=large_tenant, is_active=True)

        result = create_goals_for_tenant(large_tenant.id, 2025)

        assert result == num_users
        assert (
            Goal.objects.filter(tenant_id=large_tenant.id, year=2025).count()
            == num_users
        )


@pytest.mark.django_db(transaction=True)
class TestCreateGoalsForAllTenants(DashboardGraphQLTestCase):
    """Tests for create_goals_for_all_tenants task."""

    @patch("tenants.dashboard.tasks.Queues")
    def test_create_goals_for_all_tenants_enqueues_one_job_per_tenant(
        self, mock_queues_class
    ):
        """Task enqueues create_goals_for_tenant once per tenant."""
        mock_queue = MagicMock()
        mock_queues_class.return_value.default = mock_queue

        result = create_goals_for_all_tenants(2025)

        # At least our fixture tenant exists
        assert result >= 1
        assert mock_queue.add.call_count == result
        for call in mock_queue.add.call_args_list:
            args = call[0]
            assert args[0] is create_goals_for_tenant
            assert isinstance(args[1], int)
            assert args[2] == 2025

    @patch("tenants.dashboard.tasks.Queues")
    def test_create_goals_for_all_tenants_returns_enqueued_count(
        self, mock_queues_class
    ):
        """Task return value equals number of enqueued jobs."""
        mock_queue = MagicMock()
        mock_queues_class.return_value.default = mock_queue

        result = create_goals_for_all_tenants(2026)
        assert result == mock_queue.add.call_count

    @patch("tenants.dashboard.tasks.Queues")
    @patch("tenants.dashboard.tasks.Tenant")
    def test_create_goals_for_all_tenants_zero_tenants(
        self, mock_tenant_model, mock_queues_class
    ):
        """Task enqueues nothing and returns 0 when there are no tenants."""
        mock_qs = MagicMock()
        mock_qs.iterator.return_value = iter([])
        mock_tenant_model.objects.values_list.return_value = mock_qs

        mock_queue = MagicMock()
        mock_queues_class.return_value.default = mock_queue

        result = create_goals_for_all_tenants(2025)

        assert result == 0
        mock_queue.add.assert_not_called()

    @patch("tenants.dashboard.tasks.Queues")
    def test_create_goals_for_all_tenants_logs_enqueued_count(
        self, mock_queues_class
    ):
        """Task logs the number of jobs enqueued."""
        mock_queue = MagicMock()
        mock_queues_class.return_value.default = mock_queue

        with patch("tenants.dashboard.tasks.logger") as mock_logger:
            create_goals_for_all_tenants(2025)
            mock_logger.info.assert_called_once()
            call_msg = mock_logger.info.call_args[0][0]
            assert "Enqueued" in call_msg
            assert "2025" in call_msg

    @patch("tenants.dashboard.tasks.Queues")
    def test_create_goals_for_all_tenants_performance_20_tenants(
        self, mock_queues_class
    ):
        """Task enqueues one job per tenant for 20+ tenants (scale test)."""
        mock_queue = MagicMock()
        mock_queues_class.return_value.default = mock_queue

        # Fixture already has self.tenant; create more so we have at least 20
        extra_tenants_needed = SCALE_TEST_MIN_TENANTS - 1
        our_tenant_ids = [self.tenant.id]
        for i in range(extra_tenants_needed):
            t = self.create_tenant(name=f"Scale Tenant {i}")
            our_tenant_ids.append(t.id)

        result = create_goals_for_all_tenants(2027)

        assert result >= SCALE_TEST_MIN_TENANTS
        assert mock_queue.add.call_count == result
        enqueued_tenant_ids = [call[0][1] for call in mock_queue.add.call_args_list]
        assert set(our_tenant_ids).issubset(set(enqueued_tenant_ids))
        for call in mock_queue.add.call_args_list:
            args = call[0]
            assert args[0] is create_goals_for_tenant
            assert args[2] == 2027
