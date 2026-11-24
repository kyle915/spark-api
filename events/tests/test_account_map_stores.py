"""Account Map: one pin per physical store, drawn wherever a position exists.

* A store is a place (street address / spot on the ground), not a Retailer
  row: a banner's locations hanging off one Retailer are separate stores with
  their own visit counts, and two Retailer rows at one address are one store.
* A request with no coordinates of its own is drawn from its event's position
  (the event's coordinates, else the looked-up ``EventMapPoint``).
* Soft-deleted requests are not pins and their recaps count for no store.

The GraphQL test asks for exactly the fields spark-front-client's
``AccountMapQuery`` asks for.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from events import models as em
from events.account_stores import build_store_index
from events.admin_payloads import (
    count_account_map_pins,
    list_account_map_pins,
    scoped_requests,
)
from events.map_points import address_hash
from events.tests.base import EventsGraphQLTestCase
from recaps import models as rm

# Field for field what src/api/queries/AccountMapQuery.ts sends.
FE_MAP_QUERY = """
query AccountMapQuery($tenantId: ID, $first: Int, $offset: Int) {
  accountMapPinTotal(tenantId: $tenantId)
  accountMapPins(tenantId: $tenantId, first: $first, offset: $offset) {
    id
    name
    address
    lat
    lng
    statusSlug
    date
    retailerName
    locationName
    stateCode
    coverageClass
    visitCount
    planned
    storeKey
    locationSource
    lastVisitDate
  }
  accountMapCoverage(tenantId: $tenantId) {
    rosterTotal
    sampled
    repeat
    neverVisited
    planned
  }
}
"""

TEMPE = ("1730 E Warner Rd, Tempe, AZ 85284", [33.3340, -111.9120])
PHOENIX = ("1670 W Bethany Home Rd, Phoenix, AZ 85015", [33.5240, -112.0940])
TUCSON = ("4370 N Oracle Rd, Tucson, AZ 85705", [32.2880, -110.9780])


@pytest.mark.django_db(transaction=True)
class TestAccountMapStores(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Stores Tenant")
        self.admin = self.create_user(
            username="stores-admin",
            email="stores-admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.approved = em.RequestStatus.objects.create(
            name="Approved", tenant=self.tenant, created_by=self.sys
        )
        # One Retailer row for the whole banner, the way bulk import leaves it.
        self.banner = em.Retailer.objects.create(
            name="Total Wine",
            address="",
            store_contact="Mgr",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.now = timezone.now()

    def _request(self, place, *, when=None, retailer="banner", tenant=None, coords="place", name="visit"):
        address, point = place
        return em.Request.objects.create(
            name=name,
            address=address,
            request_type=self.rt,
            tenant=tenant or self.tenant,
            created_by=self.sys,
            status=self.approved,
            retailer=self.banner if retailer == "banner" else retailer,
            date=when or self.now - timedelta(days=5),
            coordinates=point if coords == "place" else coords,
        )

    def _filed_event(self, request, *, address=None, coordinates=None, when=None):
        when = when or self.now - timedelta(days=5)
        ev = self.create_event(
            name="shift",
            tenant=request.tenant,
            request=request,
            date=when,
            address=address if address is not None else request.address,
        )
        em.Event.objects.filter(pk=ev.pk).update(
            retailer=request.retailer, coordinates=coordinates
        )
        rm.Recap.objects.create(
            name="filed",
            event_id=ev.pk,
            created_by=self.sys,
            updated_by=self.sys,
            submited_at=when,
            total_engagements=10,
        )
        return ev

    def _pins(self):
        return list_account_map_pins(
            scoped_requests(self.tenant.id), tenant_id=self.tenant.id
        )

    # ── one pin per physical store ────────────────────────────────────

    def test_a_banner_on_one_retailer_row_is_one_store_per_address(self):
        tempe = self._request(TEMPE)
        tempe_again = self._request(TEMPE, when=self.now - timedelta(days=40))
        self._request(PHOENIX)
        self._request(TUCSON)
        self._filed_event(tempe)
        self._filed_event(tempe_again, when=self.now - timedelta(days=40))

        pins = self._pins()
        assert len(pins) == 4
        by_address = {}
        for p in pins:
            by_address.setdefault(p.address, set()).add(p.store_key)
        # Three stores, and the two Tempe requests are the same one.
        assert all(len(keys) == 1 for keys in by_address.values())
        assert len({next(iter(k)) for k in by_address.values()}) == 3

        tempe_pin = next(p for p in pins if p.address == TEMPE[0])
        phoenix_pin = next(p for p in pins if p.address == PHOENIX[0])
        # The banner's two filed visits were both in Tempe. Counting by
        # Retailer row used to give every Total Wine "Repeat, 2 visits".
        assert (tempe_pin.visit_count, tempe_pin.coverage_class) == (2, "repeat")
        assert (phoenix_pin.visit_count, phoenix_pin.coverage_class) == (
            0,
            "never_visited",
        )

    def test_two_retailer_rows_at_one_address_are_one_store(self):
        twin = em.Retailer.objects.create(
            name="Total Wine & More",
            address=TEMPE[0],
            store_contact="Mgr",
            tenant=self.tenant,
            created_by=self.sys,
        )
        a = self._request(TEMPE)
        b = self._request(
            ("1730 E. Warner Rd,  Tempe AZ 85284", TEMPE[1]), retailer=twin
        )
        self._filed_event(a)
        self._filed_event(b)
        pins = self._pins()
        assert len({p.store_key for p in pins}) == 1
        assert {p.visit_count for p in pins} == {2}

    def test_the_same_address_text_in_two_towns_is_two_stores(self):
        self._request(("100 Main St", [33.4484, -112.0740]))
        self._request(("100 Main St", [32.2226, -110.9747]))
        assert len({p.store_key for p in self._pins()}) == 2

    def test_stores_do_not_join_across_brands(self):
        other = self.create_tenant(name="Another Brand")
        other_rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=other, created_by=self.sys
        )
        mine = self._request(TEMPE)
        theirs = em.Request.objects.create(
            name="theirs",
            address=TEMPE[0],
            request_type=other_rt,
            tenant=other,
            created_by=self.sys,
            date=self.now,
            coordinates=TEMPE[1],
        )
        index = build_store_index(None)
        assert index.store_for_request(mine.pk) != index.store_for_request(theirs.pk)

    def test_planned_is_per_store_not_per_banner(self):
        self._request(TEMPE)
        self._request(PHOENIX, when=self.now + timedelta(days=7))
        pins = self._pins()
        assert next(p for p in pins if p.address == PHOENIX[0]).planned is True
        assert next(p for p in pins if p.address == TEMPE[0]).planned is False

    def test_a_walk_up_recap_with_no_request_counts_at_its_address(self):
        """A standing check-in makes an event with no request. Its recap
        belongs to the store at that address, not to the whole banner."""
        self._request(TEMPE)
        self._request(PHOENIX)
        when = self.now - timedelta(days=2)
        walk_up = self.create_event(
            name="walk-up", tenant=self.tenant, date=when, address=PHOENIX[0]
        )
        em.Event.objects.filter(pk=walk_up.pk).update(retailer=self.banner)
        rm.Recap.objects.create(
            name="filed",
            event_id=walk_up.pk,
            created_by=self.sys,
            updated_by=self.sys,
            submited_at=when,
            total_engagements=4,
        )
        pins = self._pins()
        assert next(p for p in pins if p.address == PHOENIX[0]).visit_count == 1
        assert next(p for p in pins if p.address == TEMPE[0]).visit_count == 0

    def test_a_recap_that_cannot_be_placed_is_counted_nowhere(self):
        self._request(TEMPE)
        self._request(PHOENIX)
        when = self.now - timedelta(days=2)
        vague = self.create_event(
            name="somewhere", tenant=self.tenant, date=when, address="Arizona"
        )
        em.Event.objects.filter(pk=vague.pk).update(retailer=self.banner)
        rm.Recap.objects.create(
            name="filed",
            event_id=vague.pk,
            created_by=self.sys,
            updated_by=self.sys,
            submited_at=when,
            total_engagements=4,
        )
        assert {p.visit_count for p in self._pins()} == {0}

    # ── positions from the event / the geocoder ───────────────────────

    def test_a_request_without_coordinates_uses_its_events_looked_up_point(self):
        req = self._request(TEMPE, coords=[])
        ev = self.create_event(
            name="shift", tenant=self.tenant, request=req, date=self.now, address=TEMPE[0]
        )
        qs = scoped_requests(self.tenant.id)
        # No position anywhere yet: not a pin, and not counted as one.
        assert self._pins() == []
        assert count_account_map_pins(qs) == 0

        em.EventMapPoint.objects.create(
            event=ev,
            lat=TEMPE[1][0],
            lng=TEMPE[1][1],
            address_hash=address_hash(TEMPE[0]),
            status=em.EventMapPoint.STATUS_OK,
        )
        pins = self._pins()
        assert count_account_map_pins(qs) == 1
        assert [(p.lat, p.lng, p.location_source) for p in pins] == [
            (TEMPE[1][0], TEMPE[1][1], "geocoded")
        ]

    def test_an_events_own_coordinates_beat_a_looked_up_point(self):
        req = self._request(TEMPE, coords=[])
        looked_up = self.create_event(
            name="first", tenant=self.tenant, request=req, date=self.now, address=TEMPE[0]
        )
        em.EventMapPoint.objects.create(
            event=looked_up,
            lat=33.0,
            lng=-111.0,
            address_hash=address_hash(TEMPE[0]),
            status=em.EventMapPoint.STATUS_OK,
        )
        surveyed = self.create_event(
            name="second", tenant=self.tenant, request=req, date=self.now, address=TEMPE[0]
        )
        em.Event.objects.filter(pk=surveyed.pk).update(coordinates=TEMPE[1])
        pin = self._pins()[0]
        assert (pin.lat, pin.lng, pin.location_source) == (
            TEMPE[1][0],
            TEMPE[1][1],
            "event",
        )

    def test_a_point_looked_up_for_an_old_address_is_not_used(self):
        req = self._request(TEMPE, coords=[])
        ev = self.create_event(
            name="shift", tenant=self.tenant, request=req, date=self.now, address=TEMPE[0]
        )
        em.EventMapPoint.objects.create(
            event=ev,
            lat=33.0,
            lng=-111.0,
            address_hash=address_hash("somewhere else entirely 123"),
            status=em.EventMapPoint.STATUS_OK,
        )
        assert self._pins() == []

    def test_zero_zero_and_not_found_points_are_not_pins(self):
        req = self._request(TEMPE, coords=[0, 0])
        ev = self.create_event(
            name="shift", tenant=self.tenant, request=req, date=self.now, address=TEMPE[0]
        )
        em.EventMapPoint.objects.create(
            event=ev,
            lat=None,
            lng=None,
            address_hash=address_hash(TEMPE[0]),
            status=em.EventMapPoint.STATUS_NOT_FOUND,
        )
        assert self._pins() == []

    # ── soft-deleted requests ─────────────────────────────────────────

    def test_a_deleted_request_is_no_pin_and_its_recap_counts_for_no_store(self):
        kept = self._request(TEMPE)
        gone = self._request(TEMPE, when=self.now - timedelta(days=9))
        self._filed_event(gone, when=self.now - timedelta(days=9))
        assert {p.visit_count for p in self._pins()} == {1}

        em.Request.objects.filter(pk=gone.pk).update(deleted_at=self.now)
        pins = self._pins()
        assert [p.address for p in pins] == [kept.address]
        assert pins[0].visit_count == 0
        assert pins[0].coverage_class == "never_visited"

    def test_a_deleted_requests_event_does_not_lend_a_position_or_a_plan(self):
        gone = self._request(TEMPE, coords=[], when=self.now + timedelta(days=3))
        ev = self.create_event(
            name="shift",
            tenant=self.tenant,
            request=gone,
            date=self.now + timedelta(days=3),
            start_time=self.now + timedelta(days=3),
            address=TEMPE[0],
        )
        em.Event.objects.filter(pk=ev.pk).update(coordinates=TEMPE[1])
        kept = self._request(TEMPE)
        em.Request.objects.filter(pk=gone.pk).update(deleted_at=self.now)
        pins = self._pins()
        assert len(pins) == 1 and pins[0].address == kept.address
        assert pins[0].planned is False

    # ── through the real schema, with the front end's field set ───────

    @pytest.mark.asyncio
    async def test_graphql_with_the_front_ends_exact_fields(self):
        from asgiref.sync import sync_to_async

        def seed():
            tempe = self._request(TEMPE)
            self._request(PHOENIX)
            self._filed_event(tempe)
            bare = self._request(TUCSON, coords=[])
            ev = self.create_event(
                name="shift", tenant=self.tenant, request=bare, date=self.now, address=TUCSON[0]
            )
            em.EventMapPoint.objects.create(
                event=ev,
                lat=TUCSON[1][0],
                lng=TUCSON[1][1],
                address_hash=address_hash(TUCSON[0]),
                status=em.EventMapPoint.STATUS_OK,
            )

        await sync_to_async(seed)()
        result = await self._execute_mutation(
            FE_MAP_QUERY,
            {"tenantId": str(self.tenant.id), "first": 2000, "offset": 0},
            user=self.admin,
        )
        assert result.errors is None, result.errors
        pins = result.data["accountMapPins"]
        assert result.data["accountMapPinTotal"] == 3
        assert len(pins) == 3
        assert len({p["storeKey"] for p in pins}) == 3
        by_address = {p["address"]: p for p in pins}
        assert by_address[TEMPE[0]]["visitCount"] == 1
        assert by_address[TEMPE[0]]["coverageClass"] == "sampled"
        assert by_address[TEMPE[0]]["locationSource"] == "request"
        assert by_address[PHOENIX[0]]["visitCount"] == 0
        assert by_address[TUCSON[0]]["locationSource"] == "geocoded"
        assert by_address[TUCSON[0]]["lat"] == TUCSON[1][0]
        assert result.data["accountMapCoverage"]["rosterTotal"] == 1
        assert by_address[TEMPE[0]]["lastVisitDate"] is not None
        assert by_address[PHOENIX[0]]["lastVisitDate"] is None

    # ── a request placed from its own looked-up address ───────────────

    def test_a_request_with_no_event_is_drawn_from_its_own_looked_up_address(self):
        req = self._request(TEMPE, coords=[])
        qs = scoped_requests(self.tenant.id)
        assert self._pins() == []
        em.RequestMapPoint.objects.create(
            request=req,
            lat=TEMPE[1][0],
            lng=TEMPE[1][1],
            address_hash=address_hash(TEMPE[0]),
            status=em.RequestMapPoint.STATUS_OK,
        )
        assert count_account_map_pins(qs) == 1
        pin = self._pins()[0]
        assert (pin.lat, pin.lng, pin.location_source) == (
            TEMPE[1][0],
            TEMPE[1][1],
            "geocoded",
        )

    def test_a_request_point_for_an_old_address_or_not_found_is_not_drawn(self):
        stale = self._request(TEMPE, coords=[])
        em.RequestMapPoint.objects.create(
            request=stale,
            lat=33.0,
            lng=-111.0,
            address_hash=address_hash("12 somewhere else entirely"),
            status=em.RequestMapPoint.STATUS_OK,
        )
        missing = self._request(PHOENIX, coords=[])
        em.RequestMapPoint.objects.create(
            request=missing,
            address_hash=address_hash(PHOENIX[0]),
            status=em.RequestMapPoint.STATUS_NOT_FOUND,
        )
        assert self._pins() == []

    def test_an_events_position_beats_the_requests_looked_up_address(self):
        req = self._request(TEMPE, coords=[])
        ev = self.create_event(
            name="shift", tenant=self.tenant, request=req, date=self.now, address=TEMPE[0]
        )
        em.Event.objects.filter(pk=ev.pk).update(coordinates=TEMPE[1])
        em.RequestMapPoint.objects.create(
            request=req,
            lat=33.0,
            lng=-111.0,
            address_hash=address_hash(TEMPE[0]),
            status=em.RequestMapPoint.STATUS_OK,
        )
        pin = self._pins()[0]
        assert (pin.lat, pin.location_source) == (TEMPE[1][0], "event")

    # ── store names without the visit's date ──────────────────────────

    def test_a_request_named_store_and_date_is_labelled_by_the_store(self):
        self._request(TEMPE, retailer=None, name="BP · 2027-01-11")
        self._request(PHOENIX, retailer=None, name="05/17/2026")
        by_address = {p.address: p for p in self._pins()}
        assert by_address[TEMPE[0]].name == "BP"
        assert by_address[TEMPE[0]].retailer_name == ""
        # Nothing but a date: named by its street instead.
        assert by_address[PHOENIX[0]].name == "1670 W Bethany Home Rd"

    def test_last_visit_is_the_newest_filed_visit_at_the_store(self):
        older = self.now - timedelta(days=40)
        newer = self.now - timedelta(days=3)
        req = self._request(TEMPE)
        self._filed_event(req, when=older)
        self._filed_event(req, when=newer)
        pin = self._pins()[0]
        assert pin.visit_count == 2
        assert pin.last_visit_date is not None
        assert pin.last_visit_date.startswith(newer.date().isoformat())
