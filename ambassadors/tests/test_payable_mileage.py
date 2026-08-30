"""Feel Free payable mileage: storage → stops / stops-only branching."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from ambassadors import payable_mileage as pm
from ambassadors.models import PayableMileageClaim
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from tenants.models import Tenant

STORAGE = {
    "market": "Austin, TX",
    "address": "6330 Harold Ct Austin, Texas TX 78721",
    "lat": 30.2672,
    "lng": -97.7431,
}
STOP_A = {
    "name": "Whole Foods",
    "address": "525 N Lamar Blvd, Austin, TX",
    "placeId": "abc",
    "lat": 30.2710,
    "lng": -97.7530,
}
STOP_B = {
    "name": "Target",
    "address": "2300 W Ben White Blvd, Austin, TX",
    "placeId": "def",
    "lat": 30.2300,
    "lng": -97.7800,
}
ROUTED = {
    "miles": 12.4,
    "route": [[30.2672, -97.7431], [30.2710, -97.7530], [30.2300, -97.7800]],
}


@pytest.mark.django_db(transaction=True)
class TestPayableMileage(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Feel Free")
        self.tenant.checkin_storage_units = [STORAGE]
        self.tenant.checkin_location_mode = Tenant.CHECKIN_LOCATION_MARKET
        self.tenant.save(
            update_fields=["checkin_storage_units", "checkin_location_mode"]
        )
        self.admin = self.create_user(
            username="admin-pm@test.com",
            email="admin-pm@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-pm@test.com",
            email="ba-pm@test.com",
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
        self.event.address = "Austin, TX"
        self.event.save(update_fields=["address"])

    def test_yes_branches_include_storage_in_route_points(self):
        pts = pm.build_route_points(
            started_from_storage=True, storage=STORAGE, stops=[STOP_A, STOP_B]
        )
        assert pts[0] == (STORAGE["lat"], STORAGE["lng"])
        assert len(pts) == 3

    def test_no_branches_exclude_storage(self):
        pts = pm.build_route_points(
            started_from_storage=False, storage=STORAGE, stops=[STOP_A, STOP_B]
        )
        assert pts[0] == (STOP_A["lat"], STOP_A["lng"])
        assert len(pts) == 2

    def test_save_yes_computes_miles_from_storage_chain(self):
        with (
            patch("utils.map_matching.google_directions_route_miles", return_value=None),
            patch(
                "utils.map_matching.osrm_route_waypoints", return_value=ROUTED
            ) as m,
        ):
            payload, err = pm.save_payable_mileage_claim(
                ambassador=self.ba,
                event=self.event,
                started_from_storage=True,
                stops=[STOP_A, STOP_B],
            )
        assert err is None
        assert payload["startedFromStorage"] is True
        assert payload["payableMiles"] == 12.4
        assert payload["routeSource"] == "osrm_route"
        assert m.called
        # First waypoint must be storage.
        call_pts = m.call_args[0][0]
        assert call_pts[0] == (STORAGE["lat"], STORAGE["lng"])

    def test_save_prefers_google_directions_when_available(self):
        google = {
            "miles": 14.2,
            "route": [[30.2672, -97.7431], [30.2710, -97.7530]],
        }
        with (
            patch(
                "utils.map_matching.google_directions_route_miles", return_value=google
            ) as g,
            patch("utils.map_matching.osrm_route_waypoints") as o,
        ):
            payload, err = pm.save_payable_mileage_claim(
                ambassador=self.ba,
                event=self.event,
                started_from_storage=True,
                stops=[STOP_A],
            )
        assert err is None
        assert payload["payableMiles"] == 14.2
        assert payload["routeSource"] == "google_route"
        assert g.called
        assert not o.called

    def test_save_no_routes_stops_only(self):
        with (
            patch("utils.map_matching.google_directions_route_miles", return_value=None),
            patch(
                "utils.map_matching.osrm_route_waypoints", return_value=ROUTED
            ) as m,
        ):
            payload, err = pm.save_payable_mileage_claim(
                ambassador=self.ba,
                event=self.event,
                started_from_storage=False,
                stops=[STOP_A, STOP_B],
            )
        assert err is None
        assert payload["startedFromStorage"] is False
        call_pts = m.call_args[0][0]
        assert call_pts[0] == (STOP_A["lat"], STOP_A["lng"])
        assert STORAGE["lat"] not in [p[0] for p in call_pts]

    def test_no_requires_two_stops(self):
        payload, err = pm.save_payable_mileage_claim(
            ambassador=self.ba,
            event=self.event,
            started_from_storage=False,
            stops=[STOP_A],
        )
        assert payload is None
        assert "two" in (err or "").lower()

    def test_yes_requires_at_least_one_stop(self):
        payload, err = pm.save_payable_mileage_claim(
            ambassador=self.ba,
            event=self.event,
            started_from_storage=True,
            stops=[],
        )
        assert payload is None
        assert err

    def test_inject_mileage_prefers_ba_detour_bump(self):
        from events.models import EventType
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )

        etype = EventType.objects.create(
            name="Field Sampling", tenant=self.tenant, created_by=self.admin
        )
        tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="FF Sampling",
            event_type=etype,
            created_by=self.admin,
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Sampling Details", order=1, created_by=self.admin
        )
        ftype, _ = CustomRecapFieldType.objects.get_or_create(
            name="number", defaults={"created_by": self.admin}
        )
        field = CustomField.objects.create(
            custom_recap_template=tpl,
            recap_section=section,
            name="Mileage",
            custom_field_type=ftype,
            required=True,
            options=[],
            order=1,
            created_by=self.admin,
        )
        out = pm.inject_mileage_into_field_values(
            template=tpl,
            field_values=[{"customFieldId": str(field.id), "value": "999"}],
            payable_miles=Decimal("12.40"),
        )
        assert len(out) == 1
        # BA-typed detour bump wins over the itinerary claim.
        assert out[0]["value"] == "999"

        filled = pm.inject_mileage_into_field_values(
            template=tpl,
            field_values=[],
            payable_miles=Decimal("12.40"),
        )
        assert filled[0]["value"] == "12.40"

        insert_named = CustomField.objects.create(
            custom_recap_template=tpl,
            recap_section=section,
            name="Insert your mileage",
            custom_field_type=ftype,
            required=True,
            options=[],
            order=2,
            created_by=self.admin,
        )
        # Prefer the first mileage-named field (order=1 Mileage) — still matches
        # the "Insert your mileage" naming pattern via is_mileage_custom_field.
        assert pm.is_mileage_custom_field("Insert your mileage")
        assert pm.find_mileage_custom_field(tpl).id == field.id
        _ = insert_named  # created to prove name matching in is_mileage_custom_field

    def test_is_mileage_field_matches_variants(self):
        assert pm.is_mileage_custom_field("Mileage")
        assert pm.is_mileage_custom_field("Insert your mileage")
        assert pm.is_mileage_custom_field("Miles driven")
        assert pm.is_mileage_custom_field("miles")
        assert not pm.is_mileage_custom_field("Sampling Timeframe?")

    def test_resolve_storage_by_market(self):
        matched = pm.resolve_storage_unit(self.tenant, "Austin, TX")
        assert matched is not None
        assert matched["address"] == STORAGE["address"]

    def test_resolve_tampa_st_pete_market_to_tampa_storage(self):
        """Feel Free check-in market label ≠ storage market label."""
        self.tenant.checkin_storage_units = [
            STORAGE,
            {
                "market": "Tampa, FL",
                "address": "10700 US Highway 19 N Pinellas Park, Florida FL 33782",
                "lat": 27.84,
                "lng": -82.70,
            },
        ]
        self.tenant.save(update_fields=["checkin_storage_units"])
        matched = pm.resolve_storage_unit(self.tenant, "Tampa / St. Pete, FL")
        assert matched is not None
        assert matched["market"] == "Tampa, FL"

    def test_upsert_claim_row(self):
        with (
            patch("utils.map_matching.google_directions_route_miles", return_value=None),
            patch("utils.map_matching.osrm_route_waypoints", return_value=ROUTED),
        ):
            pm.save_payable_mileage_claim(
                ambassador=self.ba,
                event=self.event,
                started_from_storage=True,
                stops=[STOP_A],
            )
            pm.save_payable_mileage_claim(
                ambassador=self.ba,
                event=self.event,
                started_from_storage=True,
                stops=[STOP_A, STOP_B],
            )
        assert PayableMileageClaim.objects.filter(
            ambassador=self.ba, event=self.event
        ).count() == 1
