"""Master Tracker market = the row's own address, never the shared retailer.

Regression: every Total Wine request showed "Tucson, AZ" because the banner
retailer account (shared by all stores) carried the first store's city and
the tracker read ``retailer.location`` first.
"""

from io import StringIO

import pytest
from django.core import mail
from django.core.management import call_command

from events import models as em
from events.market import market_for, parse_address_geo
from events.mutations import _resolve_request_location
from events.tests.base import EventsGraphQLTestCase

TRACKER_ROWS_Q = """
query Rows($filters: RequestFiltersInput) {
  requests(first: 50, filters: $filters) {
    totalCount
    edges { node { name market marketStateCode } }
  }
}
"""

TRACKER_COUNTS_Q = """
query Counts($filters: RequestFiltersInput) {
  trackerStatusCounts(filters: $filters) { total marketCodes }
}
"""


@pytest.mark.parametrize(
    "address,expected",
    [
        ("8740 Rio San Diego Dr, San Diego, CA 92108", ("San Diego", "CA", "92108")),
        (
            "1900 E Rio Salado Pkwy Ste 120, Tempe, AZ 85288",
            ("Tempe", "AZ", "85288"),
        ),
        ("1505 Hawthorne Blvd, Redondo Beach, CA 90278, USA", ("Redondo Beach", "CA", "90278")),
        ("EDMOND, OK, 73034", ("Edmond", "OK", "73034")),
        ("405 East Nifong Blvd, Columbia, Missouri", ("Columbia", "MO", None)),
        ("OREGON CITY\tOR\t97045", (None, "OR", "97045")),
        ("1357 N Elston Ave, Chicago IL", ("Chicago", "IL", None)),
        ("Breakaway Music Festival, Worcester Palladium, worcester ma", ("Worcester", "MA", None)),
        ("2714 W Southern Ave Tempe AZ 85282", (None, "AZ", "85282")),
        ("64 W 9400 S Sandy, UT  84070 United States", (None, "UT", "84070")),
        ("2712 E colonial dr Orlando Florida 32803", (None, "FL", "32803")),
        ("11650 s 73rd st papillion, ne 68046", (None, "NE", "68046")),
        ("3954A Peachtree Rd NE", (None, None, None)),
        ("3954 A Peachtree Rd Ne", (None, None, None)),
        ("13657 Washington Street", (None, None, None)),
        ("60 Washington square park south", (None, None, None)),
        ("3101 Texas Sage", (None, None, None)),
        ("Ohio state university", (None, None, None)),
        ("Center Parc Stadium - Atlanta, GA", ("Atlanta", "GA", None)),
        ("Sloan Park Festival Grounds - Phoenix, AZ 85201", ("Phoenix", "AZ", "85201")),
        ("Kissimmee Event, West Irlo Bronson Memorial Highway, FL", (None, "FL", None)),
        ("King Soopers 3600 Mesa Dr Boulder, CO", (None, "CO", None)),
        ("1 Main St, Miller Place, NY", ("Miller Place", "NY", None)),
        ("3954A PEACHTREE ROAD NE, , BROOKHAVEN, GA30319", ("Brookhaven", "GA", "30319")),
        ("2955 COBB PKWY NW STE 308, , ATLANTA, GA30339-1234", ("Atlanta", "GA", "30339")),
        ("4 Pennsylvania Plaza, New York, New York", ("New York", "NY", None)),
        ("1648 NW Chipman Road, LEE'S SUMMIT, MO 64081", ("Lee's Summit", "MO", "64081")),
        ("Tampa / St. Pete, FL", ("Tampa / St. Pete", "FL", None)),
        ("123 Main St, Indiana, PA 15701", ("Indiana", "PA", "15701")),
        ("Total Wine", (None, None, None)),
        ("", (None, None, None)),
    ],
)
def test_parse_address_geo(address, expected):
    geo = parse_address_geo(address)
    assert (geo.city, geo.state_code, geo.zip) == expected


@pytest.mark.django_db(transaction=True)
class TestTrackerMarket(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Total Wine Market Tenant")
        self.tenant.slug = "total-wine-market-tenant"
        self.tenant.save(update_fields=["slug"])
        self.admin = self.create_user(
            username="market-admin",
            email="admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.sys = self.get_system_user()
        self.az = em.State.objects.create(name="Arizona", code="AZ", created_by=self.sys)
        self.ca = em.State.objects.create(name="California", code="CA", created_by=self.sys)
        self.tucson = em.Location.objects.create(
            name="Tucson", code="TUS", zip="85701", state=self.az, created_by=self.sys
        )
        self.tempe = em.Location.objects.create(
            name="Tempe", code="TMP", zip="85284", state=self.az, created_by=self.sys
        )
        self.phoenix = em.Location.objects.create(
            name="Phoenix", code="PHX", zip="85004", state=self.az, created_by=self.sys
        )
        self.total_wine = em.Retailer.objects.create(
            name="Total Wine", tenant=self.tenant, location=self.tucson, created_by=self.sys
        )
        self.rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.status = self.create_request_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )

    def _request(self, name: str, address: str, **kw) -> em.Request:
        return em.Request.objects.create(
            name=name,
            address=address,
            request_type=self.rt,
            tenant=self.tenant,
            status=self.status,
            retailer=self.total_wine,
            created_by=self.sys,
            **kw,
        )

    def _stale(self, name: str, address: str, **fks) -> em.Request:
        """A row as prod has it: FKs written before the save-time sync."""
        req = self._request(name, address)
        em.Request.objects.filter(pk=req.pk).update(**fks)
        return em.Request.objects.get(pk=req.pk)

    def test_market_ignores_shared_retailer_location(self):
        req = self._stale(
            "San Diego",
            "8740 Rio San Diego Dr, San Diego, CA 92108",
            state=None,
            location=None,
        )
        assert market_for(req).label == "San Diego, CA"

    def test_market_falls_back_to_own_location_then_state(self):
        req = self._stale("No addr", "Total Wine", location=self.tempe, state=self.az)
        assert market_for(req).label == "Tempe, AZ"
        req = self._stale("State only", "Total Wine", location=None, state=self.ca)
        assert market_for(req).label == "CA"

    def test_save_derives_state_and_city_from_address(self):
        req = self._request(
            "Tempe", "8544 S Emerald Dr, Tempe, AZ 85284", location=self.tucson
        )
        req.refresh_from_db()
        assert req.state_id == self.az.id
        assert req.location_id == self.tempe.id

    def test_save_clears_location_in_another_state(self):
        req = self._request(
            "San Diego", "8740 Rio San Diego Dr, San Diego, CA 92108", location=self.tucson
        )
        req.refresh_from_db()
        assert req.state_id == self.ca.id
        assert req.location_id is None

    def test_save_keeps_same_state_location_without_catalog_city(self):
        req = self._request(
            "Scottsdale", "1 Main St, Scottsdale, AZ 85251", location=self.phoenix
        )
        req.refresh_from_db()
        assert req.state_id == self.az.id
        assert req.location_id == self.phoenix.id

    def test_save_finds_catalog_city_at_end_of_street_segment(self):
        req = self._request(
            "Tempe no comma", "8544 S EMERALD DR TEMPE AZ 85284", location=self.tucson
        )
        req.refresh_from_db()
        assert (req.state_id, req.location_id) == (self.az.id, self.tempe.id)
        assert market_for(req).label == "Tempe, AZ"

        venue = self._request(
            "Venue", "McCormick Place // 2301 S Dr Phoenix, AZ 85004", location=None
        )
        venue.refresh_from_db()
        assert venue.location_id == self.phoenix.id
        assert market_for(venue).label == "Phoenix, AZ"

    def test_street_name_is_never_the_catalog_city(self):
        req = self._request("Street", "100 Tempe St, AZ 85281", location=None)
        req.refresh_from_db()
        assert (req.state_id, req.location_id) == (self.az.id, None)
        assert market_for(req).label == "AZ"

    def test_unrelated_save_does_not_touch_geo(self):
        req = self._stale(
            "Stale", "8740 Rio San Diego Dr, San Diego, CA 92108", state=self.az
        )
        req.notes = "status-only edit"
        req.save(update_fields=["notes"])
        req.refresh_from_db()
        assert req.state_id == self.az.id

    @pytest.mark.asyncio
    async def test_tracker_rows_and_market_chips_use_address(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._stale)(
            "San Diego",
            "8740 Rio San Diego Dr, San Diego, CA 92108",
            state=None,
            location=None,
        )
        await sync_to_async(self._request)(
            "Tempe", "1900 E Rio Salado Pkwy Ste 120, Tempe, AZ 85288"
        )
        res = await self._execute_mutation(
            TRACKER_ROWS_Q,
            {"filters": {"tenantId": str(self.tenant.id)}},
            user=self.admin,
        )
        assert res.errors is None, res.errors
        markets = {
            e["node"]["name"]: (e["node"]["market"], e["node"]["marketStateCode"])
            for e in res.data["requests"]["edges"]
        }
        assert markets == {
            "San Diego": ("San Diego, CA", "CA"),
            "Tempe": ("Tempe, AZ", "AZ"),
        }

        az_only = await self._execute_mutation(
            TRACKER_ROWS_Q,
            {"filters": {"tenantId": str(self.tenant.id), "stateCode": "AZ"}},
            user=self.admin,
        )
        assert az_only.errors is None, az_only.errors
        assert [e["node"]["name"] for e in az_only.data["requests"]["edges"]] == ["Tempe"]

        ca_only = await self._execute_mutation(
            TRACKER_ROWS_Q,
            {"filters": {"tenantId": str(self.tenant.id), "stateCode": "CA"}},
            user=self.admin,
        )
        assert [e["node"]["name"] for e in ca_only.data["requests"]["edges"]] == [
            "San Diego"
        ]

    @pytest.mark.asyncio
    async def test_market_chip_codes_follow_request_state(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._request)(
            "San Diego", "8740 Rio San Diego Dr, San Diego, CA 92108"
        )
        res = await self._execute_mutation(
            TRACKER_COUNTS_Q,
            {"filters": {"tenantId": str(self.tenant.id)}},
            user=self.admin,
        )
        assert res.errors is None, res.errors
        assert res.data["trackerStatusCounts"]["marketCodes"] == ["CA"]

    @pytest.mark.asyncio
    async def test_notification_location_prefers_request_location(self):
        from asgiref.sync import sync_to_async

        req = await sync_to_async(self._request)(
            "Tempe", "8544 S Emerald Dr, Tempe, AZ 85284"
        )
        loc = await _resolve_request_location(req)
        assert loc.id == self.tempe.id

    def test_fix_command_dry_run_then_apply(self):
        inherited = self._stale(
            "Tempe inherited",
            "1 Mill Ave, Scottsdale, AZ 85251",
            location=self.tucson,
            state=self.az,
        )
        relink = self._stale(
            "Tempe relink",
            "8544 S Emerald Dr, Tempe, AZ 85284",
            location=self.tucson,
            state=self.az,
        )
        wrong_state = self._stale(
            "San Diego",
            "8740 Rio San Diego Dr, San Diego, CA 92108",
            location=self.tucson,
            state=self.az,
        )
        hand_set = self._stale(
            "Metro", "2 Main St, Scottsdale, AZ 85251", location=self.phoenix, state=self.az
        )
        no_addr = self._stale("No address", "Total Wine", location=self.tucson, state=self.az)

        out = StringIO()
        call_command("fix_tracker_markets", "--tenant", self.tenant.slug, stdout=out)
        log = out.getvalue()
        assert "apply=False" in log
        assert "tracker market: Tucson, AZ → San Diego, CA" in log
        assert "kept location 'Phoenix'" in log
        assert "kept location 'Tucson'" in log
        assert "address has no US state" in log
        assert "DISPLAY-ONLY FIXES" in log
        wrong_state.refresh_from_db()
        assert wrong_state.state_id == self.az.id

        call_command(
            "fix_tracker_markets", "--tenant", self.tenant.slug, "--apply", stdout=StringIO()
        )
        for r in (inherited, relink, wrong_state, hand_set, no_addr):
            r.refresh_from_db()
        assert (inherited.state_id, inherited.location_id) == (self.az.id, self.tucson.id)
        assert (relink.state_id, relink.location_id) == (self.az.id, self.tempe.id)
        assert (wrong_state.state_id, wrong_state.location_id) == (self.ca.id, None)
        assert hand_set.location_id == self.phoenix.id
        assert (no_addr.state_id, no_addr.location_id) == (self.az.id, self.tucson.id)
        assert wrong_state.status_id == self.status.id
        assert len(mail.outbox) == 0

        again = StringIO()
        call_command(
            "fix_tracker_markets", "--tenant", self.tenant.slug, "--apply", stdout=again
        )
        assert "CHANGES (0)" in again.getvalue()
