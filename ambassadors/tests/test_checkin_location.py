"""Tests for GPS on the web check-in: reverse geocoding + LocationPing.

Two separate concerns, and the distinction matters:

* ``photon_reverse`` turns the phone's coordinates into a street address so the
  BA doesn't have to type it. It talks to the network, so it is stubbed here —
  what's under test is the guards and the formatting, not Komoot's data.
* ``record_location_ping`` is what makes a web BA visible on the admin map. The
  clock coordinates were ALREADY landing on ``Attendance.coordinates``, but the
  "Today, on the ground" map and the per-event trail read ``LocationPing``, so
  browser check-ins were invisible on both.

Both paths must be unkillable: a BA in a stockroom with one bar has to be able
to clock in even when geolocation is denied, garbage, or the geocoder is down.
"""
import uuid

import pytest

from ambassadors import checkin_web
from ambassadors.models import LocationPing
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event
from utils import geocoding


@pytest.mark.django_db(transaction=True)
class TestCheckinLocation(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Geo Test {uid}")
        self.event = Event.objects.create(
            tenant=self.tenant, name="Geo event", address="1 Test Way",
            created_by=self.system_user,
        )
        ba_user = self.create_user(
            username=f"ba-{uid}",
            email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(ba_user)

    # -- reverse geocode formatting ----------------------------------------

    def test_formats_a_us_address_from_photon_parts(self):
        out = geocoding._format_photon_address({
            "housenumber": "1155", "street": "E State St",
            "city": "Trenton", "state": "NJ", "postcode": "08609",
        })
        assert out == "1155 E State St, Trenton, NJ 08609"

    def test_missing_pieces_do_not_leave_stray_punctuation(self):
        """Photon often has no house number. The result must still read
        cleanly rather than ", Trenton, NJ"."""
        out = geocoding._format_photon_address({
            "street": "E State St", "city": "Trenton", "state": "NJ",
        })
        assert out == "E State St, Trenton, NJ"
        assert not out.startswith(",")
        assert ", ," not in out

    def test_falls_back_to_feature_name_when_there_is_no_street(self):
        out = geocoding._format_photon_address({
            "name": "Liberty State Park", "city": "Jersey City", "state": "NJ",
        })
        assert out.startswith("Liberty State Park")

    # -- reverse geocode guards --------------------------------------------

    @pytest.mark.parametrize("lat,lng", [(0, 0), (999, 999), (None, None), ("x", "y")])
    def test_reverse_rejects_unusable_coordinates_without_a_network_call(
        self, lat, lng, monkeypatch
    ):
        """Null island / out-of-range / junk must short-circuit BEFORE the HTTP
        call — otherwise every denied-permission tap burns a Photon request."""
        def explode(*a, **k):  # pragma: no cover — must never run
            raise AssertionError("should not have hit the network")

        monkeypatch.setattr(geocoding.httpx, "get", explode)
        assert geocoding.photon_reverse(lat, lng) is None

    def test_reverse_returns_none_when_the_geocoder_is_down(self, monkeypatch):
        def boom(*a, **k):
            raise geocoding.httpx.ConnectError("down")

        monkeypatch.setattr(geocoding.httpx, "get", boom)
        # None, not an exception — the BA types the address instead.
        assert geocoding.photon_reverse(40.2, -74.7) is None

    # -- location pings -----------------------------------------------------

    def test_clock_coordinates_become_a_plottable_ping(self):
        ping = checkin_web.record_location_ping(
            ambassador=self.ambassador, event=self.event,
            coordinates=[40.2171, -74.7429], source="clock_in",
        )
        assert ping is not None
        assert LocationPing.objects.filter(event=self.event).count() == 1
        assert ping.source == "clock_in"
        assert ping.lat == pytest.approx(40.2171)

    @pytest.mark.parametrize(
        "coords",
        [None, [], [0, 0], ["a", "b"], [999, 0], [0, 999], [40.2]],
    )
    def test_bad_coordinates_are_dropped_not_stored(self, coords):
        assert checkin_web.record_location_ping(
            ambassador=self.ambassador, event=self.event,
            coordinates=coords, source="clock_in",
        ) is None
        assert LocationPing.objects.filter(event=self.event).count() == 0

    def test_unknown_source_falls_back_rather_than_raising(self):
        """`source` is a choices field; an unexpected value must not 500 a
        clock-in."""
        ping = checkin_web.record_location_ping(
            ambassador=self.ambassador, event=self.event,
            coordinates=[40.2, -74.7], source="not-a-real-source",
        )
        assert ping is not None
        assert ping.source == "foreground"
