"""
Event positions for the maps.

An event nobody has clocked into yet used to have no pin, because the Today
board only sent each BA's last phone location. Now the board sends the
EVENT's position too: its own coordinates, or one looked up from its address
and stored in ``EventMapPoint``.

Covered here:
  * ``events.geocoding`` — what counts as a usable Google result, and that the
    API key never leaks into a status line. ``requests.get`` is mocked; no
    test makes a network call.
  * ``geocode_events`` — the backfill command and its cron endpoint.
  * ``liveShiftBoard`` / ``staffingBoard`` through the real schema, with the
    FULL field set the front end sends (a past outage came from testing only
    the new fields).

``Event.coordinates`` must never be written by any of this: clock-in distance
checks, nearby-gig pushes and recap roll-ups read it.
"""

from __future__ import annotations

from datetime import datetime, time as _time, timedelta, timezone as _tz
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import geocoding
from events import models as event_models
from events.live_board import _ops_today
from events.map_points import (
    address_hash,
    event_map_point,
    event_map_points,
    is_lookup_worthy,
    normalise_address,
)
from events.models import Event, EventMapPoint
from events.tests.base import EventsGraphQLTestCase

FAKE_KEY = "AIzaFAKE-key-for-tests-0123456789"
KEY_PATH = "utils.map_matching._google_maps_api_key"
GET_PATH = "events.geocoding.requests.get"

# Where the command reaches the geocoder (patched so no test hits Google).
LOOKUP_PATH = "events.management.commands.geocode_events.geocoding.lookup_address"
STATUS_PATH = "events.management.commands.geocode_events.geocoding.geocoding_status"

ADDRESS = "1608 Broadway St, New York, NY 10019"


def _ops_noon() -> datetime:
    return datetime.combine(_ops_today(), _time(12, 0), tzinfo=_tz.utc)


def _reply(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload if payload is not None else {}
    return resp


def _result(lat, lng, location_type, types):
    return {
        "geometry": {"location": {"lat": lat, "lng": lng}, "location_type": location_type},
        "types": types,
    }


def _ok(*results):
    return _reply(200, {"status": "OK", "results": list(results)})


# ─────────────────────────── the geocoder ───────────────────────────


class TestGeocodeAddress:
    def _geocode(self, reply, address=ADDRESS):
        with patch(KEY_PATH, return_value=FAKE_KEY), patch(
            GET_PATH, return_value=reply
        ) as get:
            return geocoding.geocode_address(address), get

    def test_accepts_a_rooftop_result(self):
        point, get = self._geocode(
            _ok(_result(40.7614, -73.9836, "ROOFTOP", ["street_address"]))
        )
        assert point == (40.7614, -73.9836)
        # One call, to Google's geocoder, with a timeout.
        get.assert_called_once()
        args, kwargs = get.call_args
        assert args[0] == "https://maps.googleapis.com/maps/api/geocode/json"
        assert kwargs["params"] == {"address": ADDRESS, "key": FAKE_KEY, "region": "us"}
        assert kwargs["timeout"] and kwargs["timeout"] <= 10

    def test_accepts_range_interpolated(self):
        point, _ = self._geocode(
            _ok(_result(30.2672, -97.7431, "RANGE_INTERPOLATED", ["street_address"]))
        )
        assert point == (30.2672, -97.7431)

    def test_accepts_the_centre_of_a_building_or_business_only(self):
        store, _ = self._geocode(
            _ok(_result(33.75, -84.39, "GEOMETRIC_CENTER", ["establishment", "store"]))
        )
        assert store == (33.75, -84.39)
        street, _ = self._geocode(
            _ok(_result(33.75, -84.39, "GEOMETRIC_CENTER", ["route"]))
        )
        assert street is None

    def test_rejects_a_city_level_approximate_result(self):
        point, _ = self._geocode(
            _ok(_result(30.2672, -97.7431, "APPROXIMATE", ["locality", "political"]))
        )
        assert point is None

    def test_skips_a_vague_first_result_for_a_precise_second(self):
        point, _ = self._geocode(
            _ok(
                _result(30.0, -97.0, "APPROXIMATE", ["locality"]),
                _result(30.2672, -97.7431, "ROOFTOP", ["premise"]),
            )
        )
        assert point == (30.2672, -97.7431)

    def test_rejects_zero_results(self):
        point, _ = self._geocode(_reply(200, {"status": "ZERO_RESULTS", "results": []}))
        assert point is None

    def test_rejects_a_non_200(self):
        point, _ = self._geocode(_reply(500, {}))
        assert point is None

    def test_rejects_zero_zero(self):
        point, _ = self._geocode(_ok(_result(0, 0, "ROOFTOP", ["street_address"])))
        assert point is None

    def test_rejects_out_of_range(self):
        point, _ = self._geocode(_ok(_result(140.0, -73.9, "ROOFTOP", ["premise"])))
        assert point is None

    def test_no_key_means_no_call(self):
        with patch(KEY_PATH, return_value=""), patch(GET_PATH) as get:
            assert geocoding.geocode_address(ADDRESS) is None
            get.assert_not_called()

    def test_never_raises(self):
        with patch(KEY_PATH, return_value=FAKE_KEY), patch(
            GET_PATH, side_effect=RuntimeError(f"boom ?key={FAKE_KEY}")
        ):
            assert geocoding.geocode_address(ADDRESS) is None
        bad_json = _reply(200)
        bad_json.json.side_effect = ValueError("not json")
        assert self._geocode(bad_json)[0] is None
        assert self._geocode(_reply(200, ["not", "a", "dict"]))[0] is None

    def test_outcomes_tell_a_bad_address_from_a_broken_service(self):
        """Only a real "Google has no spot for this" may be remembered."""
        with patch(KEY_PATH, return_value=FAKE_KEY):
            with patch(GET_PATH, return_value=_reply(200, {"status": "ZERO_RESULTS"})):
                assert geocoding.lookup_address(ADDRESS)[0] == geocoding.NOT_FOUND
            with patch(
                GET_PATH, return_value=_reply(200, {"status": "OVER_QUERY_LIMIT"})
            ):
                assert geocoding.lookup_address(ADDRESS)[0] == geocoding.ERROR
            with patch(GET_PATH, return_value=_reply(403, {})):
                assert geocoding.lookup_address(ADDRESS)[0] == geocoding.ERROR
            with patch(GET_PATH, side_effect=TimeoutError()):
                assert geocoding.lookup_address(ADDRESS)[0] == geocoding.ERROR
        with patch(KEY_PATH, return_value=""):
            assert geocoding.lookup_address(ADDRESS)[0] == geocoding.NO_KEY


class TestGeocodingStatus:
    def test_working(self):
        with patch(KEY_PATH, return_value=FAKE_KEY), patch(
            GET_PATH,
            return_value=_ok(_result(37.422, -122.084, "ROOFTOP", ["street_address"])),
        ):
            assert geocoding.geocoding_status() == "Google geocoding: working."

    def test_off_without_a_key(self):
        with patch(KEY_PATH, return_value=""), patch(GET_PATH) as get:
            assert (
                geocoding.geocoding_status()
                == "Google geocoding: off (no GOOGLE_MAPS_API_KEY set)."
            )
            get.assert_not_called()

    def test_refused_key_never_appears(self):
        refused = _reply(
            403,
            {
                "status": "REQUEST_DENIED",
                "error_message": (
                    f"The provided API key {FAKE_KEY} is invalid. "
                    f"See https://maps.googleapis.com/x?key={FAKE_KEY}&v=1"
                ),
            },
        )
        with patch(KEY_PATH, return_value=FAKE_KEY), patch(GET_PATH, return_value=refused):
            line = geocoding.geocoding_status()
        assert line.startswith("Google geocoding: not available (HTTP 403). ")
        assert "is invalid" in line  # Google's own explanation survives
        assert FAKE_KEY not in line

    def test_refused_with_a_200_never_shows_the_key_either(self):
        """Google usually refuses a key with HTTP 200 + REQUEST_DENIED."""
        refused = _reply(
            200,
            {"status": "REQUEST_DENIED", "error_message": f"Bad key {FAKE_KEY}."},
        )
        with patch(KEY_PATH, return_value=FAKE_KEY), patch(GET_PATH, return_value=refused):
            line = geocoding.geocoding_status()
        assert line.startswith("Google geocoding: not available (REQUEST_DENIED). ")
        assert FAKE_KEY not in line

    def test_network_failure_never_shows_the_key(self):
        with patch(KEY_PATH, return_value=FAKE_KEY), patch(
            GET_PATH, side_effect=ConnectionError(f"GET /json?key={FAKE_KEY} failed")
        ):
            line = geocoding.geocoding_status()
        assert "not available" in line
        assert FAKE_KEY not in line


class TestAddressHelpers:
    def test_same_address_typed_two_ways_is_one_hash(self):
        assert normalise_address(" 123 Main St.,  Austin, TX ") == "123 main st austin tx"
        assert address_hash("123 Main St., Austin, TX") == address_hash(
            "123 main st austin tx"
        )
        assert address_hash("123 Main St") != address_hash("125 Main St")

    def test_only_street_addresses_are_worth_a_lookup(self):
        assert is_lookup_worthy(ADDRESS)
        assert not is_lookup_worthy("Austin, Texas")  # no digit
        assert not is_lookup_worthy("Bldg 4")  # too short
        assert not is_lookup_worthy("")
        assert not is_lookup_worthy(None)


# ─────────────────────── the backfill command ───────────────────────


@pytest.mark.django_db
class TestGeocodeEventsCommand(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Total Wireless", slug="total-wireless")
        self.other_tenant = self.create_tenant(name="Feel Free", slug="feel-free")
        self.noon = _ops_noon()

    def _event(self, *, address=ADDRESS, coordinates=None, tenant=None, date=None,
               request=None):
        return Event.objects.create(
            name="Store demo",
            tenant=tenant or self.tenant,
            address=address,
            coordinates=coordinates,
            date=date or self.noon,
            request=request,
            created_by=self.system_user,
        )

    def _request(self, *, deleted: bool):
        status = event_models.RequestStatus.objects.create(
            name="Approved", slug="approved", tenant=self.tenant,
            created_by=self.system_user,
        )
        return event_models.Request.objects.create(
            name="Vons",
            address=ADDRESS,
            tenant=self.tenant,
            status=status,
            request_type=self.create_request_type(name="Sampling", tenant=self.tenant),
            coordinates=[],
            deleted_at=timezone.now() if deleted else None,
            created_by=self.system_user,
        )

    def _run(self, *, lookup=None, **opts):
        """Run the command with Google stubbed out. Returns (log, lookup mock)."""
        out = StringIO()
        if lookup is None:
            lookup = (geocoding.OK, (40.7614, -73.9836), "found")
        with patch(STATUS_PATH, return_value="Google geocoding: working."), patch(
            LOOKUP_PATH, return_value=lookup
        ) as mock:
            call_command("geocode_events", stdout=out, **opts)
        return out.getvalue(), mock

    # ---------- storing ----------

    def test_stores_a_point_and_leaves_event_coordinates_alone(self):
        ev = self._event()
        log, mock = self._run()

        mock.assert_called_once_with(ADDRESS)
        point = EventMapPoint.objects.get(event=ev)
        assert (point.lat, point.lng) == (40.7614, -73.9836)
        assert point.status == "ok" and point.source == "geocoded"
        assert point.address_hash == address_hash(ADDRESS)
        # The surveyed column is NOT touched — distance checks read it.
        ev.refresh_from_db()
        assert not ev.coordinates
        assert log.startswith("Google geocoding: working.")
        assert f"event {ev.id} tenant {self.tenant.id}: found" in log
        assert "Done: 1 found" in log
        assert event_map_point(ev) == (40.7614, -73.9836, "geocoded")

    def test_skips_an_event_that_already_has_coordinates(self):
        ev = self._event(coordinates=[33.75, -84.39])
        _log, mock = self._run()

        mock.assert_not_called()
        assert not EventMapPoint.objects.exists()
        assert event_map_point(ev) == (33.75, -84.39, "event")

    def test_zero_zero_coordinates_count_as_missing(self):
        ev = self._event(coordinates=[0, 0])
        _log, mock = self._run()

        mock.assert_called_once()
        assert EventMapPoint.objects.filter(event=ev, status="ok").exists()

    def test_dry_run_stores_nothing_and_looks_nothing_up(self):
        ev = self._event()
        log, mock = self._run(dry_run=True)

        mock.assert_not_called()
        assert not EventMapPoint.objects.exists()
        assert f"event {ev.id} tenant {self.tenant.id}: would look up" in log
        assert "Dry run: 1 event(s) would be looked up" in log

    # ---------- remembering failures ----------

    def test_not_found_is_remembered_until_the_address_changes(self):
        ev = self._event(address="99999 Nowhere Rd, Nowhere, ZZ")
        miss = (geocoding.NOT_FOUND, None, "no results")

        log, mock = self._run(lookup=miss)
        mock.assert_called_once()
        row = EventMapPoint.objects.get(event=ev)
        assert row.status == "not_found" and row.lat is None
        assert f"event {ev.id} tenant {self.tenant.id}: not found" in log
        assert event_map_point(ev) is None

        # Next run: same address, so Google is not asked again.
        _log, mock = self._run(lookup=miss)
        mock.assert_not_called()

        # Someone fixes the address — now it is looked up again.
        Event.objects.filter(id=ev.id).update(address=ADDRESS)
        _log, mock = self._run()
        mock.assert_called_once_with(ADDRESS)
        row.refresh_from_db()
        assert row.status == "ok" and (row.lat, row.lng) == (40.7614, -73.9836)
        assert row.address_hash == address_hash(ADDRESS)

    def test_a_broken_service_is_not_remembered_as_a_bad_address(self):
        """A refused key or an outage must not poison every address."""
        ev = self._event()
        log, _mock = self._run(lookup=(geocoding.ERROR, None, "HTTP 403"))

        assert not EventMapPoint.objects.exists()
        assert f"event {ev.id} tenant {self.tenant.id}: lookup failed" in log

        _log, mock = self._run()  # service back: it is tried again
        mock.assert_called_once()
        assert EventMapPoint.objects.filter(event=ev, status="ok").exists()

    def test_stops_asking_after_repeated_failures(self):
        for n in range(6):
            self._event(address=f"{100 + n} Main St, Austin, TX 78701")
        _log, mock = self._run(lookup=(geocoding.ERROR, None, "HTTP 500"))

        assert mock.call_count == 3
        assert not EventMapPoint.objects.exists()

    def test_a_stale_point_is_not_used_after_the_address_changes(self):
        ev = self._event()
        self._run()
        ev.address = "500 Other Ave, Dallas, TX 75201"
        ev.save(update_fields=["address"])

        assert event_map_point(ev) is None  # old pin would be the wrong store

    # ---------- who is in scope ----------

    def test_skips_a_soft_deleted_request(self):
        gone = self._event(request=self._request(deleted=True))
        _log, mock = self._run()

        mock.assert_not_called()
        assert not EventMapPoint.objects.filter(event=gone).exists()

    def test_a_live_request_is_looked_up(self):
        kept = self._event(request=self._request(deleted=False))
        self._run()

        assert EventMapPoint.objects.filter(event=kept, status="ok").exists()

    def test_skips_addresses_that_are_not_street_addresses(self):
        vague = self._event(address="Austin, Texas")
        blank = self._event(address="")
        log, mock = self._run()

        mock.assert_not_called()
        assert not EventMapPoint.objects.exists()
        assert f"event {vague.id} tenant {self.tenant.id}: skipped (no street address)" in log
        assert f"event {blank.id} " not in log

    def test_date_window(self):
        past = self._event(date=self.noon - timedelta(days=10))
        far = self._event(date=self.noon + timedelta(days=40))
        soon = self._event(date=self.noon + timedelta(days=7))
        self._run()

        done = set(EventMapPoint.objects.values_list("event_id", flat=True))
        assert done == {soon.id}
        assert past.id not in done and far.id not in done

    def test_tenant_scope(self):
        mine = self._event()
        theirs = self._event(tenant=self.other_tenant)
        self._run(tenant=self.tenant.id)

        assert EventMapPoint.objects.filter(event=mine).exists()
        assert not EventMapPoint.objects.filter(event=theirs).exists()

    def test_limit_leaves_the_rest_for_next_run(self):
        for n in range(3):
            self._event(address=f"{200 + n} Elm St, Austin, TX 78701")
        log, mock = self._run(limit=2)

        assert mock.call_count == 2
        assert EventMapPoint.objects.count() == 2
        assert "1 left for the next run (limit 2)" in log

    def test_two_events_at_one_address_cost_one_lookup(self):
        first = self._event()
        second = self._event(address="1608 broadway st., new york, ny 10019")
        _log, mock = self._run()

        mock.assert_called_once()
        assert EventMapPoint.objects.filter(status="ok").count() == 2
        assert event_map_point(first)[:2] == event_map_point(second)[:2]

    def test_log_has_ids_only(self):
        self._event()
        log, _mock = self._run()

        assert "Store demo" not in log
        assert "Broadway" not in log

    # ---------- reading ----------

    def test_positions_for_a_whole_board_take_one_query(self):
        events = [
            self._event(address=f"{300 + n} Oak St, Austin, TX 78701") for n in range(5)
        ]
        events.append(self._event(coordinates=[33.75, -84.39]))
        self._run()

        with CaptureQueriesContext(connection) as queries:
            points = event_map_points(events)
        assert len(queries) == 1
        assert len(points) == 6
        assert {src for _lat, _lng, src in points.values()} == {"event", "geocoded"}

        with CaptureQueriesContext(connection) as queries:
            event_map_points(events[-1:])  # own coordinates only: no query
        assert len(queries) == 0


# ───────────────────────── the cron endpoint ─────────────────────────


# transaction=True, like the other cron endpoint tests: after a request
# through the test client the DB connection is closed, and a savepoint-wrapped
# test cannot reopen it ("the connection is closed").
@pytest.mark.django_db(transaction=True)
class TestGeocodeEventsCron(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Total Wireless", slug="total-wireless")
        self.other_tenant = self.create_tenant(name="Feel Free", slug="feel-free")

    def _event(self, tenant):
        return Event.objects.create(
            name="Store demo",
            tenant=tenant,
            address=ADDRESS,
            date=_ops_noon(),
            created_by=self.system_user,
        )

    @override_settings(INTERNAL_CRON_SECRET="test-cron-secret-value-only-for-tests")
    def test_cron_endpoint_needs_the_secret_and_passes_params_through(self):
        secret = "test-cron-secret-value-only-for-tests"
        url = "/internal/cron/geocode-events"
        mine = self._event(self.tenant)
        self._event(self.other_tenant)
        http = Client()

        assert http.post(url).status_code in (401, 403)

        with patch(STATUS_PATH, return_value="Google geocoding: working."), patch(
            LOOKUP_PATH, return_value=(geocoding.OK, (40.7614, -73.9836), "found")
        ) as mock:
            dry = http.post(
                f"{url}?dry_run=1&tenant={self.tenant.id}", HTTP_X_CRON_SECRET=secret
            )
            assert dry.status_code == 200
            body = dry.json()
            assert body["ok"] is True and body["dry_run"] is True
            assert "would look up" in body["log"]
            mock.assert_not_called()
            assert not EventMapPoint.objects.exists()

            real = http.post(
                f"{url}?tenant={self.tenant.id}&limit=5", HTTP_X_CRON_SECRET=secret
            )
            assert real.status_code == 200
            body = real.json()
            assert body["ok"] is True and body["dry_run"] is False
            assert "Done: 1 found" in body["log"]
            assert list(EventMapPoint.objects.values_list("event_id", flat=True)) == [
                mine.id
            ]

            bad = http.post(f"{url}?limit=lots", HTTP_X_CRON_SECRET=secret)
            assert bad.status_code == 400

    def test_cron_endpoint_is_registered(self):
        from digest.cron_views import GeocodeEventsView, _registered_views

        assert _registered_views()["geocode-events"] is GeocodeEventsView


# ───────────────────────── through the schema ─────────────────────────

# The front end's query, field for field, plus the three new event-level
# fields. Do not trim this to "just the new ones".
BOARD_QUERY = """
    query LiveShiftBoard($date: String, $tenantId: ID) {
      liveShiftBoard(date: $date, tenantId: $tenantId) {
        eventUuid eventName brandName date startTime endTime startLocal endLocal
        lastClockInTapAt address store requestUuid onClock noShows pendingApproval
        lat lng locationSource
        assigned {
          ambassadorUuid name status clockInAt clockOutAt workedHours lat lng
          locationAt locationSource pendingApproval registeredAt bookingSource
        }
      }
    }
"""

STAFFING_QUERY = """
    query StaffingBoard($start: String!, $end: String!, $tenantId: ID) {
      staffingBoard(startDate: $start, endDate: $end, tenantId: $tenantId) {
        eventUuid eventId eventName brandName date startTime endTime startLocal
        endLocal address store requestUuid tenantId isStaffed
        lat lng locationSource
        assigned { ambassadorUuid ambassadorId name isApproved }
      }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestEventPositionOnTheBoards(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Total Wireless")
        self.admin = self.create_user(
            username="admin-map@test.com",
            email="admin-map@test.com",
            role=self.roles["spark_admin"],
        )
        self.noon = _ops_noon()

    def _shift(self, name, *, address=ADDRESS, coordinates=None):
        """An event today with one approved BA (so it is on the Today board)
        who has NOT clocked in — the case that used to have no pin."""
        event = self.create_event(
            name=name,
            tenant=self.tenant,
            address=address,
            date=self.noon,
            start_time=None,
            end_time=None,
            coordinates=coordinates,
        )
        slug = name.lower().replace(" ", "-")
        user = self.create_user(
            username=f"{slug}@test.com",
            email=f"{slug}@test.com",
            role=self.roles["ambassador"],
        )
        AmbassadorEvent.objects.create(
            ambassador=self.create_ambassador(user),
            event=event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.admin,
        )
        return event

    def _seed_three(self):
        own = self._shift("Own coords", coordinates=[33.749, -84.388])
        looked_up = self._shift("Looked up", address="77 Peachtree St, Atlanta, GA")
        EventMapPoint.objects.create(
            event=looked_up,
            lat=33.7537,
            lng=-84.3901,
            address_hash=address_hash(looked_up.address),
        )
        unknown = self._shift("Nowhere", address="12 Unknown Way, Atlanta, GA")
        EventMapPoint.objects.create(
            event=unknown,
            address_hash=address_hash(unknown.address),
            status="not_found",
        )
        self._shift("No address", address="")

    @pytest.mark.asyncio
    async def test_live_board_sends_each_events_position(self):
        await sync_to_async(self._seed_three)()

        result = await self._execute_query_authenticated(
            BOARD_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        shifts = {s["eventName"]: s for s in result.data["liveShiftBoard"]}
        assert set(shifts) == {"Own coords", "Looked up", "Nowhere", "No address"}

        own = shifts["Own coords"]
        assert (own["lat"], own["lng"]) == (33.749, -84.388)
        assert own["locationSource"] == "event"

        looked_up = shifts["Looked up"]
        assert (looked_up["lat"], looked_up["lng"]) == (33.7537, -84.3901)
        assert looked_up["locationSource"] == "geocoded"

        for name in ("Nowhere", "No address"):
            row = shifts[name]
            assert row["lat"] is None and row["lng"] is None
            assert row["locationSource"] is None

        # The per-BA position is a separate thing and is untouched: nobody
        # has clocked in, so no BA has a pin, yet the events do.
        for row in shifts.values():
            assert len(row["assigned"]) == 1
            ba = row["assigned"][0]
            assert ba["lat"] is None and ba["lng"] is None
            assert ba["locationSource"] is None
            assert ba["status"] in {"upcoming", "awaiting", "no_show", "missed"}
            assert row["onClock"] == 0

    @pytest.mark.asyncio
    async def test_staffing_board_sends_each_events_position(self):
        await sync_to_async(self._seed_three)()
        day = _ops_today().isoformat()

        result = await self._execute_query_authenticated(
            STAFFING_QUERY,
            {"start": day, "end": day, "tenantId": str(self.tenant.id)},
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        shifts = {s["eventName"]: s for s in result.data["staffingBoard"]}

        assert (shifts["Own coords"]["lat"], shifts["Own coords"]["lng"]) == (
            33.749,
            -84.388,
        )
        assert shifts["Own coords"]["locationSource"] == "event"
        assert shifts["Looked up"]["locationSource"] == "geocoded"
        assert shifts["Looked up"]["lat"] == 33.7537
        assert shifts["Nowhere"]["lat"] is None
        assert shifts["Nowhere"]["locationSource"] is None
        assert shifts["No address"]["lng"] is None
        assert shifts["Own coords"]["isStaffed"] is True


class TestServiceArea:
    """A point outside North America is a wrong match, never a store."""

    def test_us_canada_hawaii_and_puerto_rico_are_in(self):
        from events.geocoding import in_service_area

        assert in_service_area(42.3539, -71.1337)  # Allston, MA
        assert in_service_area(21.3069, -157.8583)  # Honolulu
        assert in_service_area(61.2181, -149.9003)  # Anchorage
        assert in_service_area(18.4655, -66.1057)  # San Juan
        assert in_service_area(43.6532, -79.3832)  # Toronto

    def test_europe_and_the_null_island_are_out(self):
        from events.geocoding import in_service_area

        assert not in_service_area(52.2053, 0.1218)  # Cambridge, England
        assert not in_service_area(0, 0)
        assert not in_service_area(None, None)

    def test_a_stored_point_in_europe_is_not_used(self):
        from types import SimpleNamespace

        from events.map_points import own_point

        assert own_point(SimpleNamespace(coordinates=[52.2053, 0.1218])) is None
        assert own_point(SimpleNamespace(coordinates=[42.3539, -71.1337])) == (
            42.3539,
            -71.1337,
        )
