"""
Tests for the goals service.

Covers: get_goals, get_or_create_goal, extract_goal_updates, upsert_goals,
build_goals_progress, get_current_values_for_user, ensure_goals_for_tenant_users.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from tenants.dashboard.goals_service import (
    GOAL_FIELD_DEFAULTS,
    GOAL_PROGRESS_SPECS,
    GOAL_TARGET_FIELDS,
    build_goals_progress,
    ensure_goals_for_tenant_users,
    extract_goal_updates,
    get_current_values_for_user,
    get_goals,
    get_or_create_goal,
    upsert_goals,
)
from tenants.models import Goal
from tenants.dashboard.tests.base import DashboardGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestGoalsServiceConstants:
    """Test that goal constants are consistent and complete."""

    def test_goal_target_fields_derived_from_defaults(self):
        assert set(GOAL_TARGET_FIELDS) == set(GOAL_FIELD_DEFAULTS.keys())

    def test_goal_progress_specs_use_valid_goal_attrs(self):
        for spec in GOAL_PROGRESS_SPECS:
            assert spec.goal_attr in GOAL_FIELD_DEFAULTS


@pytest.mark.django_db(transaction=True)
class TestGetGoals(DashboardGraphQLTestCase):
    """Tests for get_goals."""

    def test_get_goals_returns_none_when_no_goal_exists(self):
        goal = get_goals(self.tenant.id, self.rmm_user.id, 2025)
        assert goal is None

    def test_get_goals_returns_goal_when_exists(self):
        created, _ = get_or_create_goal(self.tenant.id, self.rmm_user.id, 2025)
        found = get_goals(self.tenant.id, self.rmm_user.id, 2025)
        assert found is not None
        assert found.id == created.id
        assert found.year == 2025

    def test_get_goals_filters_by_tenant_user_year(self):
        get_or_create_goal(self.tenant.id, self.rmm_user.id, 2025)
        other_tenant = self.create_tenant(name="Other Tenant")
        assert get_goals(other_tenant.id, self.rmm_user.id, 2025) is None
        assert get_goals(self.tenant.id, self.client_user.id, 2025) is None
        assert get_goals(self.tenant.id, self.rmm_user.id, 2024) is None


@pytest.mark.django_db(transaction=True)
class TestGetOrCreateGoal(DashboardGraphQLTestCase):
    """Tests for get_or_create_goal."""

    def test_get_or_create_goal_creates_with_defaults(self):
        goal, created = get_or_create_goal(self.tenant.id, self.rmm_user.id, 2025)
        assert created is True
        assert goal.tenant_id == self.tenant.id
        assert goal.user_id == self.rmm_user.id
        assert goal.year == 2025
        for field in GOAL_TARGET_FIELDS:
            assert getattr(goal, field) is None

    def test_get_or_create_goal_returns_existing(self):
        goal1, created1 = get_or_create_goal(self.tenant.id, self.rmm_user.id, 2025)
        goal2, created2 = get_or_create_goal(self.tenant.id, self.rmm_user.id, 2025)
        assert created1 is True
        assert created2 is False
        assert goal1.id == goal2.id


@pytest.mark.django_db(transaction=True)
class TestExtractGoalUpdates(DashboardGraphQLTestCase):
    """Tests for extract_goal_updates."""

    def test_extract_goal_updates_includes_only_non_none(self):
        obj = MagicMock()
        obj.event_target_goal = 10
        obj.consumer_sampling_goal = None
        obj.brand_awareness_goal = 50.0
        obj.purchase_intent_goal = None
        obj.female_participation_goal = None
        obj.first_time_buyers_goal = 100
        result = extract_goal_updates(obj)
        assert result == {
            "event_target_goal": 10,
            "brand_awareness_goal": 50.0,
            "first_time_buyers_goal": 100,
        }

    def test_extract_goal_updates_empty_when_all_none(self):
        obj = MagicMock()
        for field in GOAL_TARGET_FIELDS:
            setattr(obj, field, None)
        assert extract_goal_updates(obj) == {}

    def test_extract_goal_updates_ignores_unknown_attrs(self):
        obj = MagicMock()
        for field in GOAL_TARGET_FIELDS:
            setattr(obj, field, None)
        obj.unknown_attr = 999
        result = extract_goal_updates(obj)
        assert "unknown_attr" not in result


@pytest.mark.django_db(transaction=True)
class TestUpsertGoals(DashboardGraphQLTestCase):
    """Tests for upsert_goals."""

    def test_upsert_goals_creates_and_sets_provided_fields(self):
        goal = upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 20, "consumer_sampling_goal": 500},
        )
        assert goal.event_target_goal == 20
        assert goal.consumer_sampling_goal == 500
        assert goal.brand_awareness_goal is None

    def test_upsert_goals_updates_existing(self):
        upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 10},
        )
        goal = upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 25, "brand_awareness_goal": 80.0},
        )
        assert goal.event_target_goal == 25
        assert goal.brand_awareness_goal == 80.0

    def test_upsert_goals_ignores_unknown_keys(self):
        """Unknown keys in goal_updates are filtered out and do not raise."""
        goal = upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 5, "invalid_key": 99},
        )
        assert goal.event_target_goal == 5

    def test_upsert_goals_ignores_none_values(self):
        upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 10},
        )
        goal = upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": None, "consumer_sampling_goal": 100},
        )
        assert goal.event_target_goal == 10
        assert goal.consumer_sampling_goal == 100

    def test_upsert_goals_empty_dict_does_not_overwrite(self):
        upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 15},
        )
        goal = upsert_goals(self.tenant.id, self.rmm_user.id, 2025, None)
        assert goal.event_target_goal == 15


@pytest.mark.django_db(transaction=True)
class TestBuildGoalsProgress(DashboardGraphQLTestCase):
    """Tests for build_goals_progress."""

    def test_build_goals_progress_empty_when_no_targets_set(self):
        goal, _ = get_or_create_goal(self.tenant.id, self.rmm_user.id, 2025)
        current = {
            "current_events_count": 5,
            "current_consumer_sampling": 100,
        }
        result = build_goals_progress(goal, current)
        assert result == []

    def test_build_goals_progress_includes_only_set_targets(self):
        goal = upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 10, "consumer_sampling_goal": 200},
        )
        current = {
            "current_events_count": 5,
            "current_consumer_sampling": 100,
            "current_brand_awareness": 0,
            "current_purchase_intent": 0,
            "current_first_time_buyers": 0,
        }
        result = build_goals_progress(goal, current)
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert "Events Target" in names
        assert "Consumer Sampling" in names
        assert result[0]["target"] in (10.0, 200.0)
        assert result[0]["current"] in (5.0, 100.0)
        assert 0 <= result[0]["percentage_complete"] <= 100

    def test_build_goals_progress_skips_zero_target(self):
        goal = upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 0},
        )
        current = {"current_events_count": 5}
        result = build_goals_progress(goal, current)
        assert len(result) == 0

    def test_build_goals_progress_caps_percentage_at_100(self):
        goal = upsert_goals(
            self.tenant.id,
            self.rmm_user.id,
            2025,
            {"event_target_goal": 5},
        )
        current = {"current_events_count": 20}
        result = build_goals_progress(goal, current)
        assert len(result) == 1
        assert result[0]["percentage_complete"] == 100.0


@pytest.mark.django_db(transaction=True)
class TestGetCurrentValuesForUser(DashboardGraphQLTestCase):
    """Tests for get_current_values_for_user.

    Uses dashboard base data: event1, event2 (with recaps and consumer engagements),
    rmm_user assigned to those events. Request dates are today and today-1.
    """

    def test_get_current_values_returns_structure(self):
        today = timezone.now().date()
        start = today - timedelta(days=30)
        end = today + timedelta(days=1)
        result = get_current_values_for_user(
            self.tenant.id,
            self.rmm_user.id,
            start,
            end,
        )
        assert "current_events_count" in result
        assert "current_consumer_sampling" in result
        assert "current_brand_awareness" in result
        assert "current_purchase_intent" in result
        assert "current_first_time_buyers" in result
        assert "current_female_participation" in result
        assert result["current_female_participation"] is None

    def test_get_current_values_includes_events_in_range(self):
        today = timezone.now().date()
        start = today - timedelta(days=30)
        end = today + timedelta(days=1)
        result = get_current_values_for_user(
            self.tenant.id,
            self.rmm_user.id,
            start,
            end,
        )
        assert result["current_events_count"] >= 2
        assert result["current_consumer_sampling"] >= 0

    def test_get_current_values_brand_awareness_and_purchase_intent_clamped(self):
        today = timezone.now().date()
        start = today - timedelta(days=30)
        end = today + timedelta(days=1)
        result = get_current_values_for_user(
            self.tenant.id,
            self.rmm_user.id,
            start,
            end,
        )
        assert 0 <= result["current_brand_awareness"] <= 100
        assert 0 <= result["current_purchase_intent"] <= 100

    def test_get_current_values_empty_for_user_with_no_events(self):
        today = timezone.now().date()
        start = today - timedelta(days=365)
        end = today
        result = get_current_values_for_user(
            self.tenant.id,
            self.client_user.id,
            start,
            end,
        )
        assert result["current_events_count"] == 0
        assert result["current_consumer_sampling"] == 0
        assert result["current_brand_awareness"] == 0.0
        assert result["current_purchase_intent"] == 0.0
        assert result["current_first_time_buyers"] == 0

    def test_get_current_values_empty_for_wrong_tenant(self):
        other_tenant = self.create_tenant(name="Other")
        today = timezone.now().date()
        result = get_current_values_for_user(
            other_tenant.id,
            self.rmm_user.id,
            today - timedelta(days=30),
            today + timedelta(days=1),
        )
        assert result["current_events_count"] == 0


@pytest.mark.django_db(transaction=True)
class TestEnsureGoalsForTenantUsers(DashboardGraphQLTestCase):
    """Tests for ensure_goals_for_tenant_users (bulk goal creation per tenant)."""

    def test_ensure_goals_creates_for_each_active_user(self):
        count = ensure_goals_for_tenant_users(self.tenant.id, 2025)
        assert count >= 2
        assert Goal.objects.filter(tenant_id=self.tenant.id, year=2025).count() >= 2

    def test_ensure_goals_exact_count_matches_active_users(self):
        """Count returned equals number of active tenanted users when no goals exist."""
        active_count = self.tenant.tenanted_users.filter(is_active=True).count()
        count = ensure_goals_for_tenant_users(self.tenant.id, 2025)
        assert count == active_count
        assert Goal.objects.filter(tenant_id=self.tenant.id, year=2025).count() == active_count

    def test_ensure_goals_idempotent_second_call_creates_zero(self):
        ensure_goals_for_tenant_users(self.tenant.id, 2025)
        count = ensure_goals_for_tenant_users(self.tenant.id, 2025)
        assert count == 0

    def test_ensure_goals_returns_zero_for_nonexistent_tenant(self):
        count = ensure_goals_for_tenant_users(999999, 2025)
        assert count == 0

    def test_ensure_goals_returns_zero_when_no_active_users(self):
        """Tenant with only inactive tenanted users gets no goals created."""
        empty_tenant = self.create_tenant(name="Empty Tenant")
        # Tenant post_save auto-links existing spark admins (prod behavior);
        # this test asserts an exact membership set, so start from a clean
        # slate — a leaked admin user from an earlier module would otherwise
        # arrive pre-linked and active.
        empty_tenant.tenanted_users.all().delete()
        self.create_tenanted_user(user=self.client_user, tenant=empty_tenant, is_active=False)
        count = ensure_goals_for_tenant_users(empty_tenant.id, 2025)
        assert count == 0
        assert Goal.objects.filter(tenant_id=empty_tenant.id, year=2025).count() == 0

    def test_ensure_goals_creates_only_for_missing_users(self):
        """When some users already have goals, only missing users get new goals."""
        # Give one user a goal already
        get_or_create_goal(self.tenant.id, self.rmm_user.id, 2025)
        active_count = self.tenant.tenanted_users.filter(is_active=True).count()
        count = ensure_goals_for_tenant_users(self.tenant.id, 2025)
        assert count == active_count - 1
        assert Goal.objects.filter(tenant_id=self.tenant.id, year=2025).count() == active_count

    def test_ensure_goals_excludes_inactive_tenanted_users(self):
        """Inactive tenanted users do not get goals."""
        extra_user = self.create_user(
            username="inactive@test.com",
            email="inactive@test.com",
            role=self.roles["client"],
            password="testpass123",
        )
        self.create_tenanted_user(user=extra_user, tenant=self.tenant, is_active=False)
        active_count = self.tenant.tenanted_users.filter(is_active=True).count()
        count = ensure_goals_for_tenant_users(self.tenant.id, 2025)
        assert count == active_count
        assert Goal.objects.filter(tenant_id=self.tenant.id, year=2025, user_id=extra_user.id).count() == 0

    @patch("tenants.dashboard.goals_service.BULK_GOAL_CREATE_BATCH_SIZE", 2)
    def test_ensure_goals_bulk_batching_respected(self):
        """When missing users exceed batch size, all are still created (batched bulk_create)."""
        big_tenant = self.create_tenant(name="Big Tenant")
        # Drop auto-linked admin memberships (see
        # test_ensure_goals_returns_zero_when_no_active_users) — the count
        # below must reflect exactly the users this test creates.
        big_tenant.tenanted_users.all().delete()
        role = self.roles["client"]
        num_users = 5  # More than patched batch size of 2
        for i in range(num_users):
            user = self.create_user(
                username=f"bulkuser{i}@test.com",
                email=f"bulkuser{i}@test.com",
                role=role,
                password="testpass123",
            )
            self.create_tenanted_user(user=user, tenant=big_tenant, is_active=True)
        count = ensure_goals_for_tenant_users(big_tenant.id, 2025)
        assert count == num_users
        assert Goal.objects.filter(tenant_id=big_tenant.id, year=2025).count() == num_users
