"""A BA moves their walk-in shift to the store they are really at.

``POST checkin/<code>/change-store`` ("Wrong store? Fix it") moves THIS BA's
own walk-in event in place: address, coordinates, state, store and title.
Same ownership rules as change-program. An address far from the phone's GPS
needs an explicit "I'm sure", which is recorded. A store Spark has never
seen becomes a tenant Retailer flagged for staff review, never twice.
"""
from __future__ import annotations

import json
import uuid

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import AmbassadorEvent, Attendance, CheckinStoreChange
from ambassadors.store_change import far_message, haversine_km, parse_point
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event, Retailer, State

OLD_ADDR = "9703 Barker Cypress Rd, Cypress, TX 77433"
OLD_POINT = (29.9235, -95.6436)
NEW_ADDR = "1201 Avocado Ave, El Cajon, CA 92020"
NEW_POINT = (32.7940, -116.9625)
# ~3 km east of NEW_POINT.
FAR_POINT = (32.7940, -116.9300)


@pytest.mark.django_db(transaction=True)
class TestCheckinChangeStore(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, monkeypatch):
        cache.clear()
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Store Fix {uid}")
        self.tenant.checkin_code = f"SF{uid.upper()}"
        self.retail = self.create_event_type("Retail Sampling", self.tenant)
        self.activation = self.create_event_type("Event Activation", self.tenant)
        self.tenant.checkin_event_type = self.retail
        self.tenant.save(update_fields=["checkin_code", "checkin_event_type"])
        self.tenant.checkin_event_types.set([self.retail, self.activation])
        for code, name in (("TX", "Texas"), ("CA", "California")):
            if not State.objects.filter(code=code).exists():
                State.objects.create(code=code, name=name, created_by=self.system_user)
        from recaps.models import CustomRecapTemplate

        self.tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant, name="Retail recap", event_type=self.retail,
            created_by=self.system_user,
        )
        self.on = dj_tz.localdate()
        self.http = DjangoClient()

        # No network in tests: Photon answers from this table.
        self.geocoded = {NEW_ADDR: list(NEW_POINT), OLD_ADDR: list(OLD_POINT)}
        self.geocode_calls = 0

        def fake_geocode(address, **_kw):
            self.geocode_calls += 1
            return self.geocoded.get(address)

        def fake_reverse(lat, lng, **_kw):
            if haversine_km(lat, lng, *NEW_POINT) < 0.2:
                return {"address": NEW_ADDR, "city": "El Cajon", "state": "California",
                        "postcode": "92020"}
            return None

        monkeypatch.setattr("utils.geocoding.photon_geocode", fake_geocode)
        monkeypatch.setattr("utils.geocoding.photon_reverse", fake_reverse)

    # -- helpers -------------------------------------------------------------

    def _post(self, name, body, code=None):
        return self.http.post(
            reverse(name, kwargs={"code": code or self.tenant.checkin_code}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def _identify(self, etype=None, phone="5055550101", store="Kroger", address=OLD_ADDR):
        body = {
            "firstName": f"Tester{phone[-2:]}",
            "lastName": "Ambassador",
            "phone": phone,
            "eventDate": self.on.isoformat(),
            "address": address,
            "eventTypeId": str((etype or self.retail).id),
            "shiftIsToday": True,
            "planEventId": "none",
            "storeName": store,
        }
        res = self._post("events.public_checkin_identify", body)
        assert res.status_code == 200, res.content
        payload = res.json()
        event = Event.objects.get(uuid=payload["event"]["uuid"])
        amb_event = AmbassadorEvent.objects.get(uuid=payload["ambassadorEventUuid"])
        return payload["sessionToken"], event, amb_event.ambassador

    def _clock_in(self, event, ambassador, point=OLD_POINT):
        amb_event = AmbassadorEvent.objects.get(event=event, ambassador=ambassador)
        checkin_web.record_attendance(
            amb_event=amb_event, kind="clock_in", coordinates=list(point), actor=ambassador.user
        )

    def _move(self, token, **extra):
        body = {
            "session": token,
            "address": NEW_ADDR,
            "latitude": NEW_POINT[0],
            "longitude": NEW_POINT[1],
            "accuracy": 12,
            "addressSource": "gps",
            "requestKey": uuid.uuid4().hex,
        }
        body.update(extra)
        return self._post("events.public_checkin_change_store", body)

    def _started(self, **kw):
        token, event, amb = self._identify(**kw)
        self._clock_in(event, amb)
        return token, event, amb

    # -- allowed -------------------------------------------------------------

    def test_move_updates_the_event_in_place_and_keeps_the_clock(self):
        token, event, amb = self._started()
        booking = AmbassadorEvent.objects.get(event=event, ambassador=amb)
        punches = list(
            Attendance.objects.filter(event=event, ambassador=amb).values_list(
                "id", "clock_time", "coordinates"
            )
        )
        old_retailer = event.retailer_id
        assert event.state.code == "TX"

        res = self._move(token, storeName="Fresh Corner Market", storeNumber="41")
        assert res.status_code == 200, res.content
        body = res.json()

        event.refresh_from_db()
        assert event.address == NEW_ADDR
        assert event.coordinates == list(NEW_POINT)
        assert event.state.code == "CA"
        stamp = f"{self.on.month}/{self.on.day}/{self.on.year}"
        assert event.name == f"{stamp} - {NEW_ADDR} (Fresh Corner Market) · Retail Sampling"
        assert event.retailer is not None and event.retailer.name == "Fresh Corner Market"
        assert event.retailer_id != old_retailer
        # Same event, same booking, same punches.
        assert body["event"]["uuid"] == str(event.uuid)
        assert body["event"]["address"] == NEW_ADDR
        assert AmbassadorEvent.objects.filter(event=event).count() == 1
        assert AmbassadorEvent.objects.get(pk=booking.pk).is_approved == booking.is_approved
        assert (
            list(
                Attendance.objects.filter(event=event, ambassador=amb).values_list(
                    "id", "clock_time", "coordinates"
                )
            )
            == punches
        )
        assert body["storeChange"]["toStore"] == "Fresh Corner Market"
        assert body["storeChange"]["createdStore"] is True

        audit = CheckinStoreChange.objects.get(event=event)
        assert audit.ambassador_id == amb.id
        assert audit.from_address == OLD_ADDR
        assert audit.to_address == NEW_ADDR
        assert audit.from_store_name == "Kroger"
        assert audit.to_store_name == "Fresh Corner Market"
        assert audit.store_number == "41"
        assert audit.from_retailer_id == old_retailer
        assert audit.to_retailer_id == event.retailer_id
        assert audit.created_retailer is True
        assert audit.gps_latitude == NEW_POINT[0]
        assert audit.gps_accuracy_m == 12
        assert audit.distance_km == 0
        assert audit.far_override is False
        assert audit.address_source == "gps"
        assert audit.detail["fromState"] == "TX"
        assert audit.detail["toState"] == "CA"
        assert audit.new_event_name == event.name

    def test_a_known_store_is_reused_not_created(self):
        joes = Retailer.objects.create(
            tenant=self.tenant, name="Joe's Market", address="1201 Avocado Boulevard, El Cajon, CA",
            created_by=self.system_user,
        )
        token, event, _amb = self._started()
        before = Retailer.objects.filter(tenant=self.tenant).count()
        res = self._move(token, storeName="joe's market")
        assert res.status_code == 200, res.content
        event.refresh_from_db()
        assert event.retailer_id == joes.id
        assert Retailer.objects.filter(tenant=self.tenant).count() == before
        assert CheckinStoreChange.objects.get(event=event).created_retailer is False

    def test_a_store_at_the_same_address_fills_a_blank_name(self):
        joes = Retailer.objects.create(
            tenant=self.tenant, name="Joe's Market", address="1201 Avocado Ave, El Cajon, CA 92020",
            created_by=self.system_user,
        )
        token, event, _amb = self._started()
        res = self._move(token)
        assert res.status_code == 200, res.content
        event.refresh_from_db()
        assert event.retailer_id == joes.id
        assert "(Joe's Market)" in event.name

    def test_retry_and_double_tap_never_duplicate(self):
        token, event, _amb = self._started()
        key = uuid.uuid4().hex
        first = self._move(token, storeName="Fresh Corner Market", requestKey=key)
        assert first.status_code == 200, first.content
        again = self._move(token, storeName="Fresh Corner Market", requestKey=key)
        assert again.status_code == 200, again.content
        assert again.json()["storeChange"]["repeated"] is True
        # A second tap with a fresh key is the same place: refused, no new store.
        other = self._move(token, storeName="Fresh Corner Market")
        assert other.status_code == 400
        assert other.json()["error"] == "same_place"
        assert CheckinStoreChange.objects.filter(event=event).count() == 1
        assert Retailer.objects.filter(tenant=self.tenant, name__iexact="Fresh Corner Market").count() == 1

    def test_another_shift_moving_to_the_same_new_store_reuses_it(self):
        token, event, _amb = self._started()
        assert self._move(token, storeName="Fresh Corner Market").status_code == 200
        token2, event2, _amb2 = self._started(phone="5055550102", address="500 Other St, Cypress, TX 77433")
        assert event2.id != event.id
        res = self._move(token2, storeName="FRESH CORNER MARKET")
        assert res.status_code == 200, res.content
        event2.refresh_from_db()
        event.refresh_from_db()
        assert event2.retailer_id == event.retailer_id
        assert Retailer.objects.filter(tenant=self.tenant, name__iexact="Fresh Corner Market").count() == 1
        # The two same-day walk-ins at one store are NOT merged; staff see it.
        audit = CheckinStoreChange.objects.get(event=event2)
        assert audit.detail["sameDayEventUuid"] == str(event.uuid)
        assert audit.created_retailer is False

    def test_far_from_gps_needs_im_sure_and_is_recorded(self):
        token, event, _amb = self._started()
        res = self._move(
            token, storeName="Fresh Corner Market", latitude=FAR_POINT[0],
            longitude=FAR_POINT[1], addressSource="search",
        )
        assert res.status_code == 409
        body = res.json()
        assert body["error"] == "far_from_gps"
        assert body["distanceKm"] > 1
        assert "miles from where your phone says" in body["message"]
        event.refresh_from_db()
        assert event.address == OLD_ADDR
        assert not CheckinStoreChange.objects.exists()
        assert not Retailer.objects.filter(name="Fresh Corner Market").exists()

        res = self._move(
            token, storeName="Fresh Corner Market", latitude=FAR_POINT[0],
            longitude=FAR_POINT[1], addressSource="search", confirmFar=True,
        )
        assert res.status_code == 200, res.content
        audit = CheckinStoreChange.objects.get(event=event)
        assert audit.far_override is True
        assert audit.distance_km > 1
        assert audit.address_source == "search"
        assert res.json()["storeChange"]["farOverride"] is True

    @override_settings(CHECKIN_STORE_FIX_MAX_KM=5)
    def test_threshold_is_configurable(self):
        token, _event, _amb = self._started()
        res = self._move(
            token, storeName="Fresh Corner Market", latitude=FAR_POINT[0],
            longitude=FAR_POINT[1], addressSource="search",
        )
        assert res.status_code == 200, res.content

    def test_no_gps_still_moves_without_a_distance(self):
        token, event, _amb = self._started()
        res = self._move(
            token, storeName="Fresh Corner Market", latitude=None, longitude=None,
            addressSource="typed",
        )
        assert res.status_code == 200, res.content
        audit = CheckinStoreChange.objects.get(event=event)
        assert audit.gps_latitude is None
        assert audit.distance_km is None
        assert audit.place_latitude == NEW_POINT[0]

    def test_retail_needs_a_store_name(self):
        token, event, _amb = self._started()
        res = self._move(token)
        assert res.status_code == 400
        assert res.json()["error"] == "store_name_required"
        event.refresh_from_db()
        assert event.address == OLD_ADDR

    def test_activation_venue_name_is_optional_and_creates_no_store(self):
        token, event, _amb = self._started(etype=self.activation, store="")
        before = Retailer.objects.filter(tenant=self.tenant).count()
        res = self._move(token, storeName="Grand Park Stage")
        assert res.status_code == 200, res.content
        event.refresh_from_db()
        assert "(Grand Park Stage) · Event Activation" in event.name
        assert Retailer.objects.filter(tenant=self.tenant).count() == before
        token2, event2, _a = self._started(etype=self.activation, store="", phone="5055550103",
                                           address="77 Elm St, Cypress, TX 77433")
        assert self._move(token2).status_code == 200

    def test_clock_out_stub_follows_the_store(self):
        from recaps.models import CustomRecap

        token, event, amb = self._started()
        stub = CustomRecap.objects.create(
            name=event.name, event=event, ambassador=amb, tenant=self.tenant,
            retailer=event.retailer, state=event.state, created_by=amb.user,
            custom_recap_template=self.tpl,
        )
        assert self._move(token, storeName="Fresh Corner Market").status_code == 200
        stub.refresh_from_db()
        event.refresh_from_db()
        assert stub.name == event.name
        assert stub.retailer_id == event.retailer_id
        assert stub.state_id == event.state_id

    def test_context_offers_the_fix(self):
        token, event, _amb = self._started()
        res = self.http.get(
            reverse("events.public_checkin_context", kwargs={"code": self.tenant.checkin_code}),
            HTTP_X_CHECKIN_SESSION=token,
        )
        state = res.json()["session"]["storeChange"]
        assert state["allowed"] is True
        assert state["storeNameRequired"] is True
        assert state["addressOnly"] is False
        assert state["storeName"] == "Kroger"
        assert state["maxDistanceKm"] == 1.0

    def test_lookup_reverse_geocodes_and_finds_the_store_nearby(self):
        token, event, _amb = self._started()
        # An earlier event 60 m away names the store.
        Event.objects.create(
            tenant=self.tenant, name="1/2/2026 - 1201 Avocado Ave (Corner Grocer)",
            address="1201 Avocado Ave", coordinates=[NEW_POINT[0] + 0.0005, NEW_POINT[1]],
            created_by=self.system_user, date=dj_tz.now(),
        )
        res = self._post(
            "events.public_checkin_store_lookup",
            {"session": token, "latitude": NEW_POINT[0], "longitude": NEW_POINT[1],
             "includeStores": True},
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["address"] == NEW_ADDR
        assert body["far"] is False
        assert body["store"]["name"] == "Corner Grocer"
        assert body["store"]["distanceM"] < 150
        assert isinstance(body["stores"], list)
        # A typed address far away is measured against GPS.
        res = self._post(
            "events.public_checkin_store_lookup",
            {"session": token, "latitude": FAR_POINT[0], "longitude": FAR_POINT[1],
             "address": NEW_ADDR},
        )
        assert res.json()["far"] is True

    # -- refused -------------------------------------------------------------

    def test_refused_before_clock_in(self):
        token, event, _amb = self._identify()
        res = self._move(token, storeName="Fresh Corner Market")
        assert res.status_code == 409
        assert res.json()["error"] == "not_clocked_in"

    def test_refused_after_the_recap_is_filed(self):
        from recaps.models import CustomRecap

        token, event, amb = self._started()
        CustomRecap.objects.create(
            name="Recap", event=event, ambassador=amb, tenant=self.tenant,
            submitted_at=dj_tz.now(), created_by=amb.user, updated_by=amb.user,
            custom_recap_template=self.tpl,
        )
        res = self._move(token, storeName="Fresh Corner Market")
        assert res.status_code == 409
        assert res.json()["error"] == "recap_filed"
        event.refresh_from_db()
        assert event.address == OLD_ADDR

    def test_refused_on_a_shared_event(self):
        token, event, _amb = self._started()
        token2, event2, amb2 = self._identify(phone="5055550104")
        assert event2.id == event.id  # joined the same walk-in
        res = self._move(token, storeName="Fresh Corner Market")
        assert res.status_code == 409
        assert res.json()["error"] == "shared_event"
        assert "Message your lead" in res.json()["message"]

    def test_refused_on_an_event_someone_else_opened(self):
        token, event, amb = self._started()
        Event.objects.filter(pk=event.pk).update(created_by=self.system_user)
        res = self._move(token, storeName="Fresh Corner Market")
        assert res.status_code == 409
        assert res.json()["error"] == "shared_event"

    def test_bad_session_is_refused(self):
        res = self._move("nope", storeName="Fresh Corner Market")
        assert res.status_code == 401

    # -- time sheet ----------------------------------------------------------

    def test_time_sheet_tenant_moves_the_location_only(self):
        self.tenant.checkin_timesheet_only = True
        self.tenant.save(update_fields=["checkin_timesheet_only"])
        token, event, amb = self._identify(store="")
        self._clock_in(event, amb)
        res = self.http.get(
            reverse("events.public_checkin_context", kwargs={"code": self.tenant.checkin_code}),
            HTTP_X_CHECKIN_SESSION=token,
        )
        state = res.json()["session"]["storeChange"]
        assert state["allowed"] is True
        assert state["addressOnly"] is True
        assert state["storeNameRequired"] is False

        before = Retailer.objects.filter(tenant=self.tenant).count()
        res = self._move(token, storeName="Ignored Store")
        assert res.status_code == 200, res.content
        event.refresh_from_db()
        stamp = f"{self.on.month}/{self.on.day}/{self.on.year}"
        assert event.name == f"{stamp} - {NEW_ADDR}"
        assert event.retailer_id is None
        assert event.state.code == "CA"
        assert Retailer.objects.filter(tenant=self.tenant).count() == before
        audit = CheckinStoreChange.objects.get(event=event)
        assert audit.to_store_name == ""
        assert audit.detail["addressOnly"] is True

    # -- staff ---------------------------------------------------------------

    def test_walkup_row_shows_the_move(self):
        from ambassadors.walkup import _build_walkup_row

        token, event, amb = self._started()
        assert self._move(token, storeName="Fresh Corner Market").status_code == 200
        event.refresh_from_db()
        row = _build_walkup_row(
            AmbassadorEvent.objects.select_related("event__tenant", "ambassador__user").get(
                event=event, ambassador=amb
            )
        )
        assert row.store_changed_from == f"Kroger · {OLD_ADDR}"
        assert row.store_changed_to == f"Fresh Corner Market · {NEW_ADDR}"
        assert row.store_changed_at
        assert row.store_change_created_store is True
        assert row.store_change_retailer_uuid == str(event.retailer.uuid)
        # Distance from venue now reads against the corrected place.
        assert row.distance_from_venue_mi is not None


# -- pure helpers --------------------------------------------------------------


def test_parse_point_rejects_junk():
    assert parse_point("32.1", "-117") == (32.1, -117.0)
    assert parse_point(None, 1) is None
    assert parse_point(0, 0) is None
    assert parse_point(95, 0) is None
    assert parse_point("nan", "1") is None


def test_far_message_reads_in_miles():
    assert "about 1.9 miles" in far_message(3.0)
    assert "about 12 miles" in far_message(20)
