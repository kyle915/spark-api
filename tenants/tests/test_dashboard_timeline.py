"""Dashboard Timeline (eventDashboard.recentEvents) lists the events in range.

The Timeline intersected the selected range (7d / 30d / 90d / YTD, all of
which END today) with "start_time or date after now" — so it was "events
later today" and said "No data yet" for tenants with a full calendar of past
demos. Runs through the real client schema with the exact field set the web
app's EventDashboardTimelineQuery selects (outage lesson).
"""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models

TIMELINE = """
query EventDashboardTimelineQuery($filters: EventDashboardFiltersInput) {
  eventDashboard(filters: $filters) {
    recentEvents { id name date location consumers intentRate status }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestDashboardTimeline(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.GS_BUCKET_NAME = "spark-test-bucket"
        cache.clear()
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Timeline Brand")
        self.admin = self.create_user(
            username="tl-admin", email="tl-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.status = self.create_request_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )
        self.request_type = self.create_request_type(name="Retail", tenant=self.tenant)

    def _event(self, name, days_ago, *, request=None):
        when = timezone.now() - timedelta(days=days_ago)
        event = self.create_event(
            name=name, tenant=self.tenant, date=when, start_time=when,
            end_time=when + timedelta(hours=3),
        )
        if request is not None:
            event_models.Event.objects.filter(id=event.id).update(request=request)
        return event

    def _request(self, name):
        return event_models.Request.objects.create(
            name=name, address="1 Main St", tenant=self.tenant,
            status=self.status, request_type=self.request_type,
            created_by=self.system_user,
        )

    async def _timeline(self, start, end):
        result = await self._execute_query_authenticated(
            TIMELINE,
            {"filters": {
                "tenantId": str(self.tenant.id),
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "sections": ["recentEvents"],
            }},
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        return result.data["eventDashboard"]["recentEvents"] or []

    @pytest.mark.asyncio
    async def test_past_events_in_range_show_newest_first(self):
        from asgiref.sync import sync_to_async

        def seed():
            self._event("Older demo", 10)
            newer = self._event("Newer demo", 3)
            recap_models.Recap.objects.create(
                name="r", event=newer, approved=True, submited_at=timezone.now(),
                created_by=self.system_user, updated_by=self.system_user,
            )
            self._event("Outside range", 60)

        await sync_to_async(seed)()
        today = timezone.now().date()
        rows = await self._timeline(today - timedelta(days=30), today)
        assert [r["name"] for r in rows] == ["Newer demo", "Older demo"]
        assert rows[0]["status"] == "Recapped"
        assert rows[1]["status"] == "Recap pending"

    @pytest.mark.asyncio
    async def test_soft_deleted_request_events_are_left_out(self):
        from asgiref.sync import sync_to_async

        def seed():
            live = self._request("Live")
            gone = self._request("Gone")
            event_models.Request.objects.filter(id=gone.id).update(
                deleted_at=timezone.now()
            )
            self._event("Live demo", 2, request=live)
            self._event("Deleted demo", 2, request=gone)

        await sync_to_async(seed)()
        today = timezone.now().date()
        rows = await self._timeline(today - timedelta(days=7), today)
        assert [r["name"] for r in rows] == ["Live demo"]

    @pytest.mark.asyncio
    async def test_future_events_in_range_are_upcoming(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(lambda: self._event("Next week", -5))()
        today = timezone.now().date()
        rows = await self._timeline(today, today + timedelta(days=14))
        assert [(r["name"], r["status"]) for r in rows] == [("Next week", "Upcoming")]
