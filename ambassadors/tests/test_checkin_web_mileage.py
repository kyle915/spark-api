"""Web mileage — the browser odometer on the public check-in link.

Deliberately NOT the app's breadcrumb tracker. Mobile Safari suspends JS and
geolocation whenever the screen locks, which is most of a drive, so a browser
trail would be full of holes and would UNDER-report mileage while looking
precise. The web flow records one fix at Start, one at Stop, and asks OSRM to
route between them over real roads.

These tests pin the behaviours that decide whether a BA gets paid correctly:
the per-gig toggle, double-tap safety, the rate snapshot, and — most
importantly — that a leg ALWAYS closes even when OSRM can't help, because a
stuck timer means a BA can't start their next leg.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.models import MileageSession
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

# Two real Austin points ~2.4 road miles apart.
START = [30.2672, -97.7431]
END = [30.2500, -97.7500]
ROUTED = {"miles": 2.4, "route": [[30.2672, -97.7431], [30.26, -97.746], END]}


@pytest.mark.django_db(transaction=True)
class TestWebMileage(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Feel Free")
        self.admin = self.create_user(
            username="admin-mi@test.com",
            email="admin-mi@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-mi@test.com",
            email="ba-mi@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)
        self.event = self.create_event(
            name="South Congress",
            tenant=self.tenant,
            date=timezone.now(),
            start_time=None,
            end_time=None,
        )
        self.event.track_mileage = True
        self.event.mileage_rate = Decimal("0.725")
        self.event.save(update_fields=["track_mileage", "mileage_rate"])

    def _start(self, coords=START):
        return checkin_web.start_mileage_leg(
            ambassador=self.ba, event=self.event, coordinates=coords
        )

    def _stop(self, coords=END, routed=ROUTED):
        with patch("utils.map_matching.osrm_route", return_value=routed) as m:
            state, msg = checkin_web.stop_mileage_leg(
                ambassador=self.ba, event=self.event, coordinates=coords
            )
        return state, msg, m

    # ---- the per-gig gate ------------------------------------------------

    def test_disabled_when_the_gig_does_not_track_mileage(self):
        self.event.track_mileage = False
        self.event.save(update_fields=["track_mileage"])

        assert checkin_web.mileage_state(ambassador=self.ba, event=self.event) == {
            "enabled": False, "active": None, "legs": [],
            "totalMiles": 0.0, "totalAmount": 0.0,
        }
        state, msg = self._start()
        assert state is None and "isn't being tracked" in msg

    # ---- the happy path --------------------------------------------------

    def test_start_then_stop_records_road_miles_and_dollars(self):
        state, msg = self._start()
        assert msg is None
        assert state["enabled"] is True
        assert state["active"] is not None
        assert state["legs"] == []

        state, msg, mock_route = self._stop()
        assert msg is None
        assert state["active"] is None
        assert len(state["legs"]) == 1
        # 2.4 mi * $0.725 = $1.74
        assert state["totalMiles"] == 2.4
        assert state["totalAmount"] == 1.74
        mock_route.assert_called_once()

    def test_rate_is_snapshotted_so_later_edits_do_not_rewrite_history(self):
        self._start()
        self._stop()
        session = MileageSession.objects.get(ambassador=self.ba, event=self.event)
        assert session.rate_per_mile == Decimal("0.725")

        self.event.mileage_rate = Decimal("1.000")
        self.event.save(update_fields=["mileage_rate"])
        session.refresh_from_db()
        assert session.rate_per_mile == Decimal("0.725")
        assert session.reimbursement_amount == Decimal("1.74")

    def test_multiple_legs_sum(self):
        self._start(); self._stop()
        self._start(); self._stop()
        state = checkin_web.mileage_state(ambassador=self.ba, event=self.event)
        assert len(state["legs"]) == 2
        assert state["totalMiles"] == 4.8
        assert state["totalAmount"] == 3.48

    def test_the_leg_is_marked_as_a_routed_odometer_not_a_matched_trail(self):
        """An admin reading the row later must be able to tell the two apart."""
        self._start()
        self._stop()
        session = MileageSession.objects.get(ambassador=self.ba, event=self.event)
        assert session.route_source == checkin_web.WEB_MILEAGE_SOURCE == "osrm_route"
        assert session.route  # geometry stored for the map

    # ---- the ways it can go wrong ---------------------------------------

    def test_double_tap_start_does_not_open_two_legs(self):
        self._start()
        state, msg = self._start()
        assert msg is None
        assert MileageSession.objects.filter(
            ambassador=self.ba, event=self.event,
            status=MileageSession.STATUS_ACTIVE,
        ).count() == 1

    def test_stop_without_start_is_a_clear_message(self):
        state, msg, _ = self._stop()
        assert state is None
        assert "No drive is running" in msg

    def test_start_without_a_location_fix_is_refused(self):
        state, msg = self._start(coords=None)
        assert state is None and "location" in msg.lower()

    def test_null_island_is_not_a_fix(self):
        state, msg = self._start(coords=[0.0, 0.0])
        assert state is None

    def test_leg_still_closes_when_osrm_cannot_route(self):
        """The important one. A BA must never be left with a stuck timer that
        blocks their next leg — but we say plainly that there's no distance
        rather than recording 0 miles as if they drove nowhere."""
        self._start()
        state, msg, _ = self._stop(routed=None)

        assert state["active"] is None, "leg must close even with no distance"
        assert "couldn't work out the distance" in msg
        session = MileageSession.objects.get(ambassador=self.ba, event=self.event)
        assert session.status == MileageSession.STATUS_COMPLETED
        assert session.total_miles is None, "no distance is null, never 0.00"
        assert session.reimbursement_amount is None
        assert session.route_source == ""

    def test_leg_closes_when_the_stop_has_no_fix(self):
        self._start()
        state, msg, mock_route = self._stop(coords=None)
        assert state["active"] is None
        assert mock_route.call_count == 0, "no end point — don't call OSRM"
        assert "couldn't work out the distance" in msg

    def test_a_forgotten_stop_is_auto_closed_and_unblocks_the_next_leg(self):
        self._start()
        stale = MileageSession.objects.get(ambassador=self.ba, event=self.event)
        stale.started_at = timezone.now() - timedelta(
            hours=checkin_web.MILEAGE_MAX_OPEN_HOURS + 1
        )
        stale.save(update_fields=["started_at"])

        assert (
            checkin_web.active_mileage_session(ambassador=self.ba, event=self.event)
            is None
        )
        stale.refresh_from_db()
        assert stale.status == MileageSession.STATUS_CANCELED
        # ...and a fresh leg can now start.
        state, msg = self._start()
        assert msg is None and state["active"] is not None

    def test_canceled_legs_do_not_count_toward_totals(self):
        self._start(); self._stop()
        MileageSession.objects.create(
            tenant=self.tenant, ambassador=self.ba, event=self.event,
            status=MileageSession.STATUS_CANCELED, total_miles=Decimal("99.00"),
        )
        state = checkin_web.mileage_state(ambassador=self.ba, event=self.event)
        assert state["totalMiles"] == 2.4

    def test_mileage_rides_along_in_the_public_context(self):
        """The page reads state from the same payload it already fetches."""
        self._start()
        ctx = checkin_web.build_public_context(self.event, self.ba)
        assert ctx["session"]["mileage"]["enabled"] is True
        assert ctx["session"]["mileage"]["active"] is not None
