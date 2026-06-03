"""
Tests for AmbassadorEventQueries.my_earnings_stats — the mobile
Earnings tab's shift count + hour estimate.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestMyEarningsStats(AmbassadorsGraphQLTestCase):
    """Coverage for the lightweight per-BA earnings preview query."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Earnings Tenant")

    async def create_user_async(self, **kwargs):
        return await sync_to_async(self.create_user)(**kwargs)

    @staticmethod
    def _shift_at(days_ago: int, start_hour: int, end_hour: int):
        """Helper: (date, start_time, end_time) for a shift `days_ago`.

        Event.start_time / Event.end_time are DateTimeField (not TimeField),
        so they must be tz-aware datetimes, not bare ``time`` objects. We
        anchor start/end to the shift's calendar day; the earnings resolver
        only reads their clock components (.hour/.minute/.second) for the
        hour estimate, so the date portion is incidental.
        """
        d = datetime.now(_tz.utc) - timedelta(days=days_ago)
        return (
            d,
            d.replace(hour=start_hour, minute=0, second=0, microsecond=0),
            d.replace(hour=end_hour, minute=0, second=0, microsecond=0),
        )

    QUERY = """
    query Stats($withinDays: Int) {
      myEarningsStats(withinDays: $withinDays) {
        shiftsCount
        hoursEstimate
        withinDays
      }
    }
    """

    @pytest.mark.asyncio
    async def test_empty_window_returns_zero_and_null_hours(self):
        ba_user = await self.create_user_async(
            username="ba-no-shifts",
            email="ba-no@test.com",
            role=self.roles["ambassador"],
        )
        await sync_to_async(self.create_ambassador)(ba_user)

        result = await self._execute_mutation(
            self.QUERY,
            {"withinDays": 30},
            self.endpoint_path,
            user=ba_user,
        )

        assert result.errors is None, f"query errored: {result.errors}"
        stats = result.data["myEarningsStats"]
        assert stats["shiftsCount"] == 0
        assert stats["hoursEstimate"] is None
        assert stats["withinDays"] == 30

    @pytest.mark.asyncio
    async def test_counts_approved_shifts_within_window(self):
        ba_user = await self.create_user_async(
            username="ba-3-shifts",
            email="ba-3@test.com",
            role=self.roles["ambassador"],
        )
        ambassador = await sync_to_async(self.create_ambassador)(ba_user)

        # Two 4-hour shifts within window
        for days_ago in (1, 10):
            d, s, e = self._shift_at(days_ago, 10, 14)
            ev = await sync_to_async(self.create_event)(
                name=f"Shift {days_ago}d ago",
                tenant=self.tenant,
                date=d,
                start_time=s,
                end_time=e,
            )
            await sync_to_async(AmbassadorEvent.objects.create)(
                ambassador=ambassador,
                event=ev,
                tenant=self.tenant,
                is_approved=True,
                created_by=ba_user,
            )

        # One shift outside the 30-day window (90 days ago)
        d, s, e = self._shift_at(90, 9, 13)
        ev_old = await sync_to_async(self.create_event)(
            name="Old shift",
            tenant=self.tenant,
            date=d,
            start_time=s,
            end_time=e,
        )
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=ambassador,
            event=ev_old,
            tenant=self.tenant,
            is_approved=True,
            created_by=ba_user,
        )

        result = await self._execute_mutation(
            self.QUERY,
            {"withinDays": 30},
            self.endpoint_path,
            user=ba_user,
        )

        assert result.errors is None, f"query errored: {result.errors}"
        stats = result.data["myEarningsStats"]
        assert stats["shiftsCount"] == 2
        # 2 shifts × 4 hours = 8.0
        assert stats["hoursEstimate"] == 8.0

    @pytest.mark.asyncio
    async def test_ignores_unapproved_shifts(self):
        ba_user = await self.create_user_async(
            username="ba-pending",
            email="ba-pending@test.com",
            role=self.roles["ambassador"],
        )
        ambassador = await sync_to_async(self.create_ambassador)(ba_user)

        d, s, e = self._shift_at(5, 12, 16)
        ev = await sync_to_async(self.create_event)(
            name="Pending shift",
            tenant=self.tenant,
            date=d,
            start_time=s,
            end_time=e,
        )
        # Unapproved — should not count
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=ambassador,
            event=ev,
            tenant=self.tenant,
            is_approved=False,
            created_by=ba_user,
        )

        result = await self._execute_mutation(
            self.QUERY,
            {"withinDays": 30},
            self.endpoint_path,
            user=ba_user,
        )

        assert result.errors is None, f"query errored: {result.errors}"
        stats = result.data["myEarningsStats"]
        assert stats["shiftsCount"] == 0
        assert stats["hoursEstimate"] is None

    @pytest.mark.asyncio
    async def test_within_days_is_clamped(self):
        ba_user = await self.create_user_async(
            username="ba-clamp",
            email="ba-clamp@test.com",
            role=self.roles["ambassador"],
        )
        await sync_to_async(self.create_ambassador)(ba_user)

        # 9999 should clamp to 365
        result = await self._execute_mutation(
            self.QUERY,
            {"withinDays": 9999},
            self.endpoint_path,
            user=ba_user,
        )
        assert result.errors is None
        assert result.data["myEarningsStats"]["withinDays"] == 365

        # 0 should clamp to 1
        result = await self._execute_mutation(
            self.QUERY,
            {"withinDays": 0},
            self.endpoint_path,
            user=ba_user,
        )
        assert result.errors is None
        assert result.data["myEarningsStats"]["withinDays"] == 1

    @pytest.mark.asyncio
    async def test_handles_midnight_rollover(self):
        ba_user = await self.create_user_async(
            username="ba-midnight",
            email="ba-mid@test.com",
            role=self.roles["ambassador"],
        )
        ambassador = await sync_to_async(self.create_ambassador)(ba_user)

        # 10 PM → 2 AM = 4 hours across midnight. start_time/end_time are
        # DateTimeField, so build tz-aware datetimes (the resolver reads only
        # their clock components and rolls a negative delta over midnight).
        d = datetime.now(_tz.utc) - timedelta(days=2)
        ev = await sync_to_async(self.create_event)(
            name="Late shift",
            tenant=self.tenant,
            date=d,
            start_time=d.replace(hour=22, minute=0, second=0, microsecond=0),
            end_time=d.replace(hour=2, minute=0, second=0, microsecond=0),
        )
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=ambassador,
            event=ev,
            tenant=self.tenant,
            is_approved=True,
            created_by=ba_user,
        )

        result = await self._execute_mutation(
            self.QUERY,
            {"withinDays": 30},
            self.endpoint_path,
            user=ba_user,
        )
        assert result.errors is None
        assert result.data["myEarningsStats"]["shiftsCount"] == 1
        # Should be 4h (not -20h)
        assert result.data["myEarningsStats"]["hoursEstimate"] == 4.0
