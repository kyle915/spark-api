"""
Tests for the missing-recap surface — query + nudge + reassign.

Covers:
- `missingRecapEvents` query returns events whose end_time is in the
  past and have no recap; excludes events that have a recap; respects
  the lookbackDays window; populates assigned ambassadors per row.
- `nudgeAmbassadorForRecap` mutation fires the push (mocked) on a
  valid AmbassadorEvent; refuses to push when the event already has
  a recap (idempotency guard).
- `reassignRecapEvent` mutation moves a recap to a new event in the
  same tenant; rejects cross-tenant moves.

The tests stub `send_push_to_user` so they don't try to hit the live
Expo relay during a test run.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import AsyncMock, patch
from asgiref.sync import sync_to_async

from ambassadors.models import AmbassadorEvent
from recaps.models import Recap
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


MISSING_QUERY = """
query Missing($lookbackDays: Int!) {
  missingRecapEvents(lookbackDays: $lookbackDays) {
    eventUuid
    eventName
    hoursOverdue
    assignedAmbassadors {
      ambassadorEventUuid
      name
      isApproved
    }
  }
}
"""

NUDGE_MUTATION = """
mutation Nudge($input: NudgeAmbassadorForRecapInput!) {
  nudgeAmbassadorForRecap(input: $input) {
    success
    message
    devicesNotified
  }
}
"""

REASSIGN_MUTATION = """
mutation Reassign($input: ReassignRecapEventInput!) {
  reassignRecapEvent(input: $input) {
    success
    message
    recap {
      id
      event {
        id
      }
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestMissingRecapEvents(AmbassadorsGraphQLTestCase):
    """Coverage for the admin-facing `missingRecapEvents` query."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.tenant = self.create_tenant(name="Missing Recap Tenant")
        self.admin = self.create_user(
            username="admin-missing",
            email="admin-missing@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-missing",
            email="ba-missing@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)

    def _past_event(self, *, hours_ago: int = 4, name: str = "Past shift"):
        end = datetime.now(_tz.utc) - timedelta(hours=hours_ago)
        # Event spans [end-4h, end] — typical 4-hour sampling slot.
        return self.create_event(
            name=name,
            tenant=self.tenant,
            date=end,
            start_time=end - timedelta(hours=4),
            end_time=end,
        )

    def _ba_event(self, event, *, is_approved: bool = True):
        return AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            is_approved=is_approved,
            created_by=self.admin,
        )

    @pytest.mark.asyncio
    async def test_returns_past_events_without_a_recap(self):
        ev = await sync_to_async(self._past_event)(hours_ago=2, name="Solo")
        await sync_to_async(self._ba_event)(ev)

        result = await self._execute_mutation(
            MISSING_QUERY,
            {"lookbackDays": 30},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None, f"errored: {result.errors}"
        rows = result.data["missingRecapEvents"]
        assert len(rows) == 1
        assert rows[0]["eventName"] == "Solo"
        assert rows[0]["hoursOverdue"] is not None
        assert rows[0]["hoursOverdue"] >= 2
        ba_rows = rows[0]["assignedAmbassadors"]
        assert len(ba_rows) == 1
        assert ba_rows[0]["isApproved"] is True

    @pytest.mark.asyncio
    async def test_excludes_events_with_a_recap(self):
        ev = await sync_to_async(self._past_event)(name="Already filed")
        await sync_to_async(self._ba_event)(ev)
        # Attach a recap → event should drop out of the result.
        await sync_to_async(Recap.objects.create)(
            name="filed",
            event=ev,
            ambassador=self.ambassador,
            created_by=self.admin,
        )

        result = await self._execute_mutation(
            MISSING_QUERY,
            {"lookbackDays": 30},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None
        assert result.data["missingRecapEvents"] == []

    @pytest.mark.asyncio
    async def test_respects_lookback_days_window(self):
        # 40 days in the past — outside the 30-day window.
        long_ago_end = datetime.now(_tz.utc) - timedelta(days=40)
        old_ev = await sync_to_async(self.create_event)(
            name="Way old",
            tenant=self.tenant,
            date=long_ago_end,
            start_time=long_ago_end - timedelta(hours=4),
            end_time=long_ago_end,
        )
        await sync_to_async(self._ba_event)(old_ev)
        # And a recent one — should be the only row.
        recent = await sync_to_async(self._past_event)(name="Recent")
        await sync_to_async(self._ba_event)(recent)

        result = await self._execute_mutation(
            MISSING_QUERY,
            {"lookbackDays": 30},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None
        names = [r["eventName"] for r in result.data["missingRecapEvents"]]
        assert "Recent" in names
        assert "Way old" not in names

    @pytest.mark.asyncio
    async def test_excludes_future_events(self):
        # Sanity check: a future event is "not yet ended" so it
        # doesn't belong in the missing list at all.
        future_start = datetime.now(_tz.utc) + timedelta(days=3)
        future_ev = await sync_to_async(self.create_event)(
            name="Future",
            tenant=self.tenant,
            date=future_start,
            start_time=future_start,
            end_time=future_start + timedelta(hours=4),
        )
        await sync_to_async(self._ba_event)(future_ev)

        result = await self._execute_mutation(
            MISSING_QUERY,
            {"lookbackDays": 30},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None
        assert result.data["missingRecapEvents"] == []


@pytest.mark.django_db(transaction=True)
class TestNudgeAmbassadorForRecap(AmbassadorsGraphQLTestCase):
    """Coverage for the per-row `nudgeAmbassadorForRecap` mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.tenant = self.create_tenant(name="Nudge Tenant")
        self.admin = self.create_user(
            username="admin-nudge",
            email="admin-nudge@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-nudge",
            email="ba-nudge@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        end = datetime.now(_tz.utc) - timedelta(hours=4)
        self.event = self.create_event(
            name="Nudge target",
            tenant=self.tenant,
            date=end,
            start_time=end - timedelta(hours=4),
            end_time=end,
        )
        self.ambassador_event = AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.admin,
        )

    @pytest.mark.asyncio
    async def test_fires_push_on_valid_assignment(self):
        # send_push_to_user is awaitable — patch with an AsyncMock
        # that returns 2 (devices-notified) so the success path
        # reports a real number. The mutation imports it function-locally
        # (`from ambassadors.push import send_push_to_user`), so the patch
        # must target ambassadors.push — the module the name is read from
        # at call time.
        with patch(
            "ambassadors.push.send_push_to_user",
            new=AsyncMock(return_value=2),
        ) as mock_push:
            result = await self._execute_mutation(
                NUDGE_MUTATION,
                {
                    "input": {
                        "ambassadorEventUuid": str(self.ambassador_event.uuid),
                    }
                },
                self.endpoint_path,
                user=self.admin,
            )

        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["nudgeAmbassadorForRecap"]
        assert payload["success"] is True
        assert payload["devicesNotified"] == 2
        mock_push.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refuses_push_when_recap_already_filed(self):
        # Idempotency guard: if a recap already exists for the event
        # we shouldn't fire the "you owe a recap" push.
        await sync_to_async(Recap.objects.create)(
            name="prior",
            event=self.event,
            ambassador=self.ambassador,
            created_by=self.admin,
        )

        with patch(
            "ambassadors.push.send_push_to_user",
            new=AsyncMock(return_value=99),
        ) as mock_push:
            result = await self._execute_mutation(
                NUDGE_MUTATION,
                {
                    "input": {
                        "ambassadorEventUuid": str(self.ambassador_event.uuid),
                    }
                },
                self.endpoint_path,
                user=self.admin,
            )

        assert result.errors is None
        payload = result.data["nudgeAmbassadorForRecap"]
        assert payload["success"] is False
        assert "already filed" in payload["message"].lower()
        mock_push.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
class TestReassignRecapEvent(AmbassadorsGraphQLTestCase):
    """Coverage for `reassignRecapEvent` — moving a recap between events."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.tenant = self.create_tenant(name="Reassign Tenant")
        self.other_tenant = self.create_tenant(name="Other Tenant")
        self.admin = self.create_user(
            username="admin-reassign",
            email="admin-reassign@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-reassign",
            email="ba-reassign@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        now = datetime.now(_tz.utc)
        self.event_a = self.create_event(
            name="Event A",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event_b = self.create_event(
            name="Event B",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.recap = Recap.objects.create(
            name="Mis-linked recap",
            event=self.event_a,
            ambassador=self.ambassador,
            created_by=self.admin,
        )

    @pytest.mark.asyncio
    async def test_moves_recap_to_same_tenant_event(self):
        # `resolve_id_to_int` (the helper the mutation uses internally)
        # accepts either a relay-encoded globalId OR a digit string,
        # so passing the raw PK as a string works here.
        result = await self._execute_mutation(
            REASSIGN_MUTATION,
            {
                "input": {
                    "recapId": str(self.recap.id),
                    "eventId": str(self.event_b.id),
                }
            },
            self.endpoint_path,
            user=self.admin,
        )

        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["reassignRecapEvent"]
        assert payload["success"] is True
        # Reload from DB to verify the change persisted.
        refreshed = await sync_to_async(
            Recap.objects.select_related("event").get
        )(pk=self.recap.id)
        assert refreshed.event_id == self.event_b.id

    @pytest.mark.asyncio
    async def test_rejects_cross_tenant_move(self):
        now = datetime.now(_tz.utc)
        cross_event = await sync_to_async(self.create_event)(
            name="Cross-tenant",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )

        result = await self._execute_mutation(
            REASSIGN_MUTATION,
            {
                "input": {
                    "recapId": str(self.recap.id),
                    "eventId": str(cross_event.id),
                }
            },
            self.endpoint_path,
            user=self.admin,
        )

        assert result.errors is None
        payload = result.data["reassignRecapEvent"]
        assert payload["success"] is False
        assert "tenant" in payload["message"].lower()
        # And the original recap should still point at event_a.
        refreshed = await sync_to_async(Recap.objects.get)(pk=self.recap.id)
        assert refreshed.event_id == self.event_a.id
