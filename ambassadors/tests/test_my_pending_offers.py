"""
Tests for AmbassadorEventQueries.my_pending_offers — the mobile
Shifts-tab query that surfaces unaccepted shift invites.

Coverage:
- Returns is_approved=False rows with start_time in the future
- Excludes accepted (is_approved=True) shifts
- Excludes past-start invites (stale, can't be acted on)
- Returns empty for non-ambassador callers
"""

import pytest
from datetime import datetime, time, timedelta, timezone as _tz
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()

QUERY = """
query Pending {
  myPendingOffers {
    ambassadorEventUuid
    eventUuid
    eventName
    venue
    isApproved
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestMyPendingOffers(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Pending Offers Tenant")
        self.ba_user = self.create_user(
            username="ba-pending-offers",
            email="ba-pending-offers@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.admin = self.create_user(
            username="admin-pending-offers",
            email="adm-pending@test.com",
            role=self.roles["spark_admin"],
        )

    def _future_event(self, *, days_ahead: int = 7, name: str = "Future shift"):
        start = datetime.now(_tz.utc) + timedelta(days=days_ahead)
        return self.create_event(
            name=name,
            tenant=self.tenant,
            date=start,
            start_time=start,
            end_time=start + timedelta(hours=4),
        )

    def _past_event(self, *, days_ago: int = 2, name: str = "Past shift"):
        start = datetime.now(_tz.utc) - timedelta(days=days_ago)
        return self.create_event(
            name=name,
            tenant=self.tenant,
            date=start,
            start_time=start,
            end_time=start + timedelta(hours=4),
        )

    @pytest.mark.asyncio
    async def test_returns_pending_future_invites(self):
        ev_a = await sync_to_async(self._future_event)(
            days_ahead=3, name="Shift A"
        )
        ev_b = await sync_to_async(self._future_event)(
            days_ahead=10, name="Shift B"
        )
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=ev_a,
            tenant=self.tenant,
            is_approved=False,
            created_by=self.admin,
        )
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=ev_b,
            tenant=self.tenant,
            is_approved=False,
            created_by=self.admin,
        )

        result = await self._execute_mutation(
            QUERY, {}, self.endpoint_path, user=self.ba_user
        )
        assert result.errors is None, f"errored: {result.errors}"
        offers = result.data["myPendingOffers"]
        # Sorted by start_time ascending — A (3 days) before B (10 days).
        assert [o["eventName"] for o in offers] == ["Shift A", "Shift B"]
        assert all(o["isApproved"] is False for o in offers)

    @pytest.mark.asyncio
    async def test_excludes_accepted_shifts(self):
        ev = await sync_to_async(self._future_event)(name="Already accepted")
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=ev,
            tenant=self.tenant,
            is_approved=True,  # already accepted
            created_by=self.admin,
        )

        result = await self._execute_mutation(
            QUERY, {}, self.endpoint_path, user=self.ba_user
        )
        assert result.errors is None
        assert result.data["myPendingOffers"] == []

    @pytest.mark.asyncio
    async def test_excludes_past_invites(self):
        ev = await sync_to_async(self._past_event)(name="Stale invite")
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=ev,
            tenant=self.tenant,
            is_approved=False,
            created_by=self.admin,
        )

        result = await self._execute_mutation(
            QUERY, {}, self.endpoint_path, user=self.ba_user
        )
        assert result.errors is None
        assert result.data["myPendingOffers"] == []

    @pytest.mark.asyncio
    async def test_empty_for_non_ambassador(self):
        # The admin is signed in but has no Ambassador profile.
        result = await self._execute_mutation(
            QUERY, {}, self.endpoint_path, user=self.admin
        )
        assert result.errors is None
        assert result.data["myPendingOffers"] == []

    @pytest.mark.asyncio
    async def test_offers_carry_venue_tz_labels(self):
        """Pending offers must expose pre-formatted date/start/end labels so
        the mobile client renders venue-local time verbatim (not device-parsed).
        Regression guard: the resolver previously omitted these, so the
        pending-invite row device-parsed the raw datetimes — wrong time for
        out-of-state shifts and a prior-day date for 00:00Z event.date."""
        ev = await sync_to_async(self._future_event)(name="Labeled shift")
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=ev,
            tenant=self.tenant,
            is_approved=False,
            created_by=self.admin,
        )

        query = """
        query Pending {
          myPendingOffers {
            eventName
            dateLabel
            startLabel
            endLabel
          }
        }
        """
        result = await self._execute_mutation(
            query, {}, self.endpoint_path, user=self.ba_user
        )
        assert result.errors is None, f"errored: {result.errors}"
        offers = result.data["myPendingOffers"]
        assert len(offers) == 1
        o = offers[0]
        # Non-null, human-formatted labels (event has start/end times set).
        assert o["dateLabel"], "dateLabel should be populated"
        assert o["startLabel"] and (
            "AM" in o["startLabel"] or "PM" in o["startLabel"]
        ), o["startLabel"]
        assert o["endLabel"] and (
            "AM" in o["endLabel"] or "PM" in o["endLabel"]
        ), o["endLabel"]
