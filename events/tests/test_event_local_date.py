"""Event.localDate: the day a shift is on at the venue.

Today, Upcoming and the Calendar group events by day. They used to read the
day by putting the event's date through the viewer's browser clock, so the day
depended on where the viewer sat. ``localDate`` is the venue's day, decided on
the server. Runs the web app's real EventsQuery (full field set) through the
clients schema, the endpoint the app uses.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase

# spark-front-client src/api/queries/EventsQuery.ts, field for field.
EVENTS_QUERY = """
query EventsQuery($filters: EventFiltersInput, $first: Int, $after: String) {
  events(filters: $filters, first: $first, after: $after) {
    totalCount
    pageInfo { hasNextPage hasPreviousPage startCursor endCursor }
    edges {
      cursor
      node {
        __typename id uuid name date localDate
        timezone { __typename id uuid name code offset }
        startTime endTime address coordinates isNational notes
        request { __typename id uuid name date retailer { __typename id uuid name } }
        tenantId
        eventType { __typename id uuid name }
        location { id }
        state { id }
        status { __typename id uuid name slug }
      }
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestEventLocalDate(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Local Date Brand")
        self.admin = self.create_user(
            username="admin-localdate@test.com",
            email="admin-localdate@test.com",
            role=self.roles["spark_admin"],
        )

    def _seed(self):
        from events.models import Event, TimeZone

        pacific = TimeZone.objects.create(name="Pacific", code="PST", offset=-480)
        eastern = TimeZone.objects.create(name="Eastern", code="EST", offset=-300)
        la = ZoneInfo("America/Los_Angeles")
        ny = ZoneInfo("America/New_York")
        rows = {
            # 7 PM Pacific Nov 16 = Nov 17 03:00 UTC.
            "evening west": (pacific, datetime(2026, 11, 16, 19, 0, tzinfo=la)),
            # 9 AM Eastern Nov 17.
            "morning east": (eastern, datetime(2026, 11, 17, 9, 0, tzinfo=ny)),
            # No zone: read in Pacific. 02:00 UTC Nov 17 = 6 PM Pacific Nov 16.
            "no zone": (None, datetime(2026, 11, 17, 2, 0, tzinfo=ZoneInfo("UTC"))),
        }
        for name, (tz, start) in rows.items():
            ev = self.create_event(name=name, tenant=self.tenant, date=start)
            Event.objects.filter(pk=ev.pk).update(
                timezone=tz, start_time=start, end_time=start + timedelta(hours=3)
            )

    async def _run(self, **filters):
        result = await self._execute_query_authenticated(
            EVENTS_QUERY,
            {"first": 100, "filters": {"tenantId": str(self.tenant.id), **filters}},
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        return {
            e["node"]["name"]: e["node"] for e in result.data["events"]["edges"]
        }

    @pytest.mark.asyncio
    async def test_local_date_is_the_venues_day(self):
        await sync_to_async(self._seed)()
        nodes = await self._run()
        assert nodes["evening west"]["localDate"] == "2026-11-16"
        assert nodes["evening west"]["date"].startswith("2026-11-16T19:00")
        assert nodes["morning east"]["localDate"] == "2026-11-17"
        assert nodes["no zone"]["localDate"] == "2026-11-16"

    @pytest.mark.asyncio
    async def test_a_window_one_day_wider_catches_the_venue_day(self):
        """The date filter is by UTC day. The web app asks for one extra day
        each side and keeps rows whose localDate is in range; this pins that
        the evening-west shift is in the UTC-day window ending the next day."""
        await sync_to_async(self._seed)()
        exact = await self._run(startDate="2026-11-16", endDate="2026-11-16")
        assert "evening west" not in exact  # the UTC-day trap
        wider = await self._run(startDate="2026-11-15", endDate="2026-11-17")
        assert wider["evening west"]["localDate"] == "2026-11-16"
