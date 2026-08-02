"""Roaming crews: market-keyed events + sampling stops.

Total Wireless is STATIC — one BA, one store, all day — so the check-in link
keys its event on the store address the BA types. Feel Free ROAMS: a crew works
a market and moves between spots all shift. Keying on a typed address forks a
new event per BA per spelling, which for a roaming crew multiplies into junk
events every day and scatters the hours across them.

Market mode keys the event on the market instead, and the individual spots
become SamplingStops — explicit, timestamped, real GPS, captured in the moment
rather than recalled into the recap at the end.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.models import LocationPing, SamplingStop
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from tenants.models import Tenant

MARKETS = ["Miami, FL", "Ft. Lauderdale, FL", "Austin, TX", "San Antonio, TX"]


@pytest.mark.django_db(transaction=True)
class TestMarketMode(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Feel Free Markets")
        self.actor = self.create_user(
            username="actor-mk@test.com",
            email="actor-mk@test.com",
            role=self.roles["spark_admin"],
        )

    def test_defaults_to_address_mode(self):
        """Every existing brand keeps the store-address behaviour."""
        assert checkin_web.tenant_location_mode(self.tenant) == "address"
        assert checkin_web.tenant_markets(self.tenant) == []

    def test_explicit_market_list_wins(self):
        self.tenant.checkin_location_mode = Tenant.CHECKIN_LOCATION_MARKET
        self.tenant.checkin_markets = MARKETS
        self.tenant.save(
            update_fields=["checkin_location_mode", "checkin_markets"]
        )
        assert checkin_web.tenant_location_mode(self.tenant) == "market"
        assert checkin_web.tenant_markets(self.tenant) == MARKETS

    def test_markets_are_read_off_the_brands_own_recap_template(self):
        """ONE list. A market added to the recap form shows up on the link,
        instead of two lists drifting apart."""
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
        )

        from events.models import EventType

        # CustomRecapTemplate.event_type is NOT NULL.
        etype = EventType.objects.create(
            name="Field Sampling", tenant=self.tenant, created_by=self.actor
        )
        tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="FF Sampling",
            event_type=etype,
            created_by=self.actor,
        )
        ftype, _ = CustomRecapFieldType.objects.get_or_create(
            name="select", defaults={"created_by": self.actor}
        )
        # CustomField.recap_section is NOT NULL too.
        from recaps.models import RecapSection

        section = RecapSection.objects.create(
            tenant=self.tenant, name="Event Information", order=0,
            created_by=self.actor,
        )
        CustomField.objects.create(
            custom_recap_template=tpl,
            recap_section=section,
            name="Event Location:",
            custom_field_type=ftype,
            options=MARKETS,
            created_by=self.actor,
        )
        assert checkin_web.tenant_markets(self.tenant) == MARKETS

    def test_one_event_per_market_per_day_no_matter_who_checks_in(self):
        """The whole point: three BAs working Austin today share ONE event."""
        ids = set()
        for _ in range(3):
            event, _created = checkin_web.find_or_create_walkin_event(
                tenant=self.tenant,
                store_name="",
                address="Austin, TX",
                on_date=timezone.now().date(),
                actor=self.actor,
            )
            ids.add(event.id)
        assert len(ids) == 1

    def test_different_markets_stay_separate(self):
        day = timezone.now().date()
        a, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address="Austin, TX",
            on_date=day, actor=self.actor,
        )
        b, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address="Miami, FL",
            on_date=day, actor=self.actor,
        )
        assert a.id != b.id


@pytest.mark.django_db(transaction=True)
class TestSamplingStops(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Feel Free Stops")
        self.admin = self.create_user(
            username="admin-st@test.com",
            email="admin-st@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-st@test.com",
            email="ba-st@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)
        self.event = self.create_event(
            name="Austin, TX",
            tenant=self.tenant,
            date=timezone.now(),
            start_time=None,
            end_time=None,
        )

    def _log(self, coords=(30.2672, -97.7431), name="", address="1 Congress Ave"):
        with patch(
            "utils.geocoding.photon_reverse", return_value=address
        ) as geo:
            stop, err = checkin_web.log_sampling_stop(
                ambassador=self.ba,
                event=self.event,
                coordinates=list(coords) if coords else None,
                name=name,
            )
        return stop, err, geo

    def test_logs_a_stop_with_a_reverse_geocoded_address(self):
        stop, err, geo = self._log(name="South Congress patio")
        assert err is None
        assert stop["name"] == "South Congress patio"
        assert stop["address"] == "1 Congress Ave"
        assert stop["lat"] == pytest.approx(30.2672)
        assert stop["recordedAt"]
        geo.assert_called_once()

    def test_stops_come_back_in_order(self):
        self._log(name="first")
        self._log(name="second")
        names = [
            s["name"]
            for s in checkin_web.sampling_stops(ambassador=self.ba, event=self.event)
        ]
        assert names == ["first", "second"]

    def test_a_stop_also_plots_on_the_admin_map(self):
        """Mirrored to LocationPing so it appears on the existing map and the
        per-event trail with no new admin UI."""
        before = LocationPing.objects.filter(event=self.event).count()
        self._log()
        assert LocationPing.objects.filter(event=self.event).count() == before + 1

    def test_a_typed_name_alone_is_enough_when_gps_is_denied(self):
        """A BA in a basement with no fix can still say where they were."""
        stop, err, geo = self._log(coords=None, name="Barton Springs")
        assert err is None
        assert stop["name"] == "Barton Springs"
        assert stop["lat"] is None
        assert geo.call_count == 0
        # No coordinates means nothing to plot — but the stop is still recorded.
        assert LocationPing.objects.filter(event=self.event).count() == 0

    def test_neither_gps_nor_a_name_is_refused(self):
        stop, err, _ = self._log(coords=None, name="")
        assert stop is None
        assert "location" in err.lower()

    def test_null_island_is_not_a_fix(self):
        stop, err, _ = self._log(coords=(0.0, 0.0), name="somewhere")
        assert err is None
        assert stop["lat"] is None, "0,0 is 'no fix', not a point off Ghana"

    def test_a_failing_geocoder_never_loses_the_stop(self):
        with patch("utils.geocoding.photon_reverse", side_effect=RuntimeError("down")):
            stop, err = checkin_web.log_sampling_stop(
                ambassador=self.ba,
                event=self.event,
                coordinates=[30.2672, -97.7431],
                name="Zilker",
            )
        assert err is None
        assert stop["address"] == ""
        assert stop["lat"] == pytest.approx(30.2672)
        assert SamplingStop.objects.filter(event=self.event).count() == 1

    def test_stops_ride_along_in_the_public_context(self):
        self._log(name="stop one")
        ctx = checkin_web.build_public_context(self.event, self.ba)
        assert len(ctx["session"]["stops"]) == 1
        assert ctx["session"]["stops"][0]["name"] == "stop one"

    def test_stops_are_scoped_to_the_ba_who_logged_them(self):
        """Several BAs share a market event; each sees only their own trail."""
        other_user = self.create_user(
            username="ba2-st@test.com",
            email="ba2-st@test.com",
            role=self.roles["ambassador"],
        )
        other = self.create_ambassador(other_user)
        self._log(name="mine")

        assert checkin_web.sampling_stops(ambassador=other, event=self.event) == []
        assert len(
            checkin_web.sampling_stops(ambassador=self.ba, event=self.event)
        ) == 1
