"""
Coverage for the `backfill_event_coordinates` management command.

It populates ``Event.coordinates`` for events with missing coordinates
(null/empty/[0,0]) by COPYING from the parent ``Request.coordinates`` when
valid (free, no network) and otherwise GEOCODING ``Event.address`` via the
keyless Photon API. Behaviour contract mirrors repair_event_dates:
    * DRY-RUN by default (writes nothing); --execute opts in to writes.
    * idempotent (a second --execute run updates 0 rows).
    * --tenant scopes to one tenant.
    * saves ONLY the `coordinates` field.

The Photon HTTP call is STUBBED in every test (no real network).
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from events import models as event_models
from events.tests.base import EventsGraphQLTestCase

# Where the command looks up photon_geocode (patch at the use site).
GEOCODE_PATH = (
    "events.management.commands.backfill_event_coordinates.photon_geocode"
)
# Skip the politeness sleep so tests don't wait on the network spacing delay.
SLEEP_PATH = "events.management.commands.backfill_event_coordinates.time.sleep"


@pytest.mark.django_db
class TestBackfillEventCoordinates(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death", slug="liquid-death")
        self.other_tenant = self.create_tenant(
            name="Total Wireless", slug="total-wireless"
        )
        self.ev_status = self.create_event_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )
        self.ev_status_other = self.create_event_status(
            name="Approved", tenant=self.other_tenant, slug="approved"
        )
        self.request_type = self.create_request_type(
            name="Sampling", tenant=self.tenant
        )
        self.request_type_other = self.create_request_type(
            name="Sampling", tenant=self.other_tenant
        )

    # ─── factories ───────────────────────────────────────────────────

    def _make_event(
        self,
        *,
        tenant=None,
        status=None,
        coordinates=None,
        address="1608 Broadway St, New York, NY",
        request_coordinates=None,
        with_request=True,
    ):
        tenant = tenant or self.tenant
        status = status or self.ev_status
        request_type = (
            self.request_type if tenant == self.tenant else self.request_type_other
        )
        request = None
        if with_request:
            req_status = event_models.RequestStatus.objects.create(
                name="Approved",
                slug="approved",
                tenant=tenant,
                created_by=self.system_user,
            )
            request = event_models.Request.objects.create(
                name="Vons",
                address="1608 Broadway St",
                tenant=tenant,
                status=req_status,
                request_type=request_type,
                coordinates=request_coordinates if request_coordinates is not None else [],
                created_by=self.system_user,
            )
        return event_models.Event.objects.create(
            name="Vons activation",
            tenant=tenant,
            request=request,
            status=status,
            address=address,
            coordinates=coordinates if coordinates is not None else [],
            created_by=self.system_user,
        )

    # ─── copy-from-request path (NO network) ─────────────────────────

    def test_execute_copies_coords_from_request_no_network(self):
        ev = self._make_event(coordinates=[], request_coordinates=[40.758, -73.985])
        out = StringIO()
        # photon must NOT be called when the request has usable coords.
        with patch(GEOCODE_PATH) as mock_geo:
            call_command("backfill_event_coordinates", execute=True, stdout=out)
            mock_geo.assert_not_called()
        ev.refresh_from_db()
        assert ev.coordinates == [40.758, -73.985]
        report = out.getvalue()
        assert "Updated: 1 event(s)" in report
        assert "from request=1" in report

    def test_request_zero_island_coords_are_not_copied_falls_back_to_geocode(self):
        # [0,0] on the request is the null-island sentinel — NOT usable, so the
        # command must fall through to geocoding the event address.
        ev = self._make_event(coordinates=[], request_coordinates=[0, 0])
        out = StringIO()
        with patch(GEOCODE_PATH, return_value=[34.05, -118.24]) as mock_geo, \
                patch(SLEEP_PATH):
            call_command("backfill_event_coordinates", execute=True, stdout=out)
            mock_geo.assert_called_once()
        ev.refresh_from_db()
        assert ev.coordinates == [34.05, -118.24]
        assert "geocoded=1" in out.getvalue()

    # ─── geocode path (stubbed Photon) ───────────────────────────────

    def test_execute_geocodes_when_no_request_coords(self):
        ev = self._make_event(coordinates=[], request_coordinates=[])
        out = StringIO()
        with patch(GEOCODE_PATH, return_value=[34.05, -118.24]) as mock_geo, \
                patch(SLEEP_PATH):
            call_command("backfill_event_coordinates", execute=True, stdout=out)
            mock_geo.assert_called_once()
        ev.refresh_from_db()
        assert ev.coordinates == [34.05, -118.24]
        report = out.getvalue()
        assert "Updated: 1 event(s)" in report
        assert "geocoded=1" in report

    def test_geocode_miss_skips_row(self):
        ev = self._make_event(coordinates=[], request_coordinates=[])
        out = StringIO()
        with patch(GEOCODE_PATH, return_value=None), patch(SLEEP_PATH):
            call_command("backfill_event_coordinates", execute=True, stdout=out)
        ev.refresh_from_db()
        assert ev.coordinates == []  # untouched
        report = out.getvalue()
        assert "Updated: 0 event(s)" in report
        assert "Skipped/failed" in report

    # ─── idempotency ─────────────────────────────────────────────────

    def test_second_execute_run_updates_zero(self):
        self._make_event(coordinates=[], request_coordinates=[40.758, -73.985])
        first = StringIO()
        call_command("backfill_event_coordinates", execute=True, stdout=first)
        assert "Updated: 1 event(s)" in first.getvalue()

        second = StringIO()
        with patch(GEOCODE_PATH) as mock_geo:
            call_command("backfill_event_coordinates", execute=True, stdout=second)
            mock_geo.assert_not_called()
        assert "Updated: 0 event(s)" in second.getvalue()

    def test_event_with_valid_coords_is_not_a_candidate(self):
        ev = self._make_event(coordinates=[12.34, 56.78], request_coordinates=[1, 2])
        out = StringIO()
        with patch(GEOCODE_PATH) as mock_geo:
            call_command("backfill_event_coordinates", execute=True, stdout=out)
            mock_geo.assert_not_called()
        ev.refresh_from_db()
        assert ev.coordinates == [12.34, 56.78]  # unchanged
        assert "Updated: 0 event(s)" in out.getvalue()

    # ─── dry-run writes nothing ──────────────────────────────────────

    def test_dry_run_is_default_and_writes_nothing(self):
        ev = self._make_event(coordinates=[], request_coordinates=[40.758, -73.985])
        out = StringIO()
        # No execute kwarg → dry-run default. Geocode shouldn't even be needed
        # here (request coords present), but assert no writes regardless.
        call_command("backfill_event_coordinates", stdout=out)
        ev.refresh_from_db()
        assert ev.coordinates == []  # untouched
        report = out.getvalue()
        assert "DRY RUN" in report
        assert "Would update: 1 event(s)" in report

    def test_dry_run_does_not_sleep_even_on_geocode_path(self):
        self._make_event(coordinates=[], request_coordinates=[])
        out = StringIO()
        with patch(GEOCODE_PATH, return_value=[1.0, 2.0]), patch(SLEEP_PATH) as mock_sleep:
            call_command("backfill_event_coordinates", stdout=out)  # dry-run
            mock_sleep.assert_not_called()
        assert "Would update: 1 event(s)" in out.getvalue()

    # ─── --tenant scoping ────────────────────────────────────────────

    def test_tenant_scope_only_touches_that_tenant(self):
        mine = self._make_event(
            tenant=self.tenant, status=self.ev_status,
            coordinates=[], request_coordinates=[40.758, -73.985],
        )
        theirs = self._make_event(
            tenant=self.other_tenant, status=self.ev_status_other,
            coordinates=[], request_coordinates=[41.0, -74.0],
        )
        out = StringIO()
        call_command(
            "backfill_event_coordinates", execute=True,
            tenant="liquid-death", stdout=out,
        )
        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.coordinates == [40.758, -73.985]  # repaired
        assert theirs.coordinates == []  # other tenant untouched
        assert "Updated: 1 event(s)" in out.getvalue()

    # ─── only saves the coordinates field ────────────────────────────

    def test_only_coordinates_field_is_saved(self):
        ev = self._make_event(coordinates=[], request_coordinates=[40.758, -73.985])
        # Mutate name out-of-band; the command uses update_fields=["coordinates"]
        # so it must NOT clobber name.
        event_models.Event.objects.filter(id=ev.id).update(name="UNTOUCHED")
        call_command("backfill_event_coordinates", execute=True, stdout=StringIO())
        ev.refresh_from_db()
        assert ev.coordinates == [40.758, -73.985]
        assert ev.name == "UNTOUCHED"


class TestPhotonGeocodeHelper:
    """Unit coverage for utils.geocoding.photon_geocode — the single network
    seam. httpx is stubbed; no real network. Asserts the GeoJSON [lng, lat] →
    stored [lat, lng] swap and the best-effort "return None, never raise"
    contract."""

    def test_maps_geojson_lng_lat_to_lat_lng(self):
        from utils import geocoding

        fake = MagicMockResponse(
            {"features": [{"geometry": {"coordinates": [-73.985, 40.758]}}]}
        )
        with patch("utils.geocoding.httpx.get", return_value=fake):
            assert geocoding.photon_geocode("1 Times Sq, NY") == [40.758, -73.985]

    def test_empty_address_returns_none_without_calling_network(self):
        from utils import geocoding

        with patch("utils.geocoding.httpx.get") as mock_get:
            assert geocoding.photon_geocode("  ") is None
            mock_get.assert_not_called()

    def test_no_features_returns_none(self):
        from utils import geocoding

        fake = MagicMockResponse({"features": []})
        with patch("utils.geocoding.httpx.get", return_value=fake):
            assert geocoding.photon_geocode("nowhere") is None

    def test_transport_error_returns_none(self):
        import httpx

        from utils import geocoding

        with patch(
            "utils.geocoding.httpx.get", side_effect=httpx.ConnectError("down")
        ):
            assert geocoding.photon_geocode("anywhere") is None

    def test_has_valid_coordinates_rejects_empty_and_null_island(self):
        from utils.geocoding import has_valid_coordinates

        assert has_valid_coordinates([40.7, -73.9]) is True
        assert has_valid_coordinates([]) is False
        assert has_valid_coordinates(None) is False
        assert has_valid_coordinates([0, 0]) is False
        assert has_valid_coordinates([1.0]) is False  # too short


class MagicMockResponse:
    """Minimal stand-in for an httpx.Response: .raise_for_status() no-ops and
    .json() returns the payload handed to the constructor."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload
