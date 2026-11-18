"""Recap location normalizer: retailer, store number, city and state per recap.

The model is never called: ``utils.ai_text._post_json`` is patched, and the
request body it would have sent is checked for people data.
"""

from __future__ import annotations

import json
from datetime import datetime
from io import StringIO
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.test import Client as DjangoClient
from django.test import override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events import models as event_models
from recaps import models as recap_models
from recaps.recap_location import chain_in_text, run, scrub, state_code_of
from recaps.us_state_geo import state_at

# ─── Pure text rules ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("HEB #614 - Austin", ("H-E-B", "614")),
        ("H-E-B 540", ("H-E-B", "540")),
        ("heb", ("H-E-B", "")),
        ("Kroger Store 412 Houston", ("Kroger", "412")),
        ("Whole Foods Los Angeles · 2026-06-21", ("Whole Foods Market", "")),
        ("Whole Foods 06/21", ("Whole Foods Market", "")),
        ("HEB 10 - 2pm", ("H-E-B", "")),
        ("HEB 1234 Main St, Austin TX", ("H-E-B", "")),
        ("HEB - Shift #2", ("H-E-B", "")),
        ("Total Wine & More #1203", ("Total Wine & More", "1203")),
        ("Demo at the farmers market", ("", "")),
    ],
)
def test_chain_and_store_number_in_text(text, expected):
    assert chain_in_text(text) == expected


def test_state_code_of():
    assert state_code_of("tx") == "TX"
    assert state_code_of("Texas") == "TX"
    assert state_code_of(" new york ") == "NY"
    assert state_code_of("Tex") == ""
    assert state_code_of("XX") == ""


def test_scrub_blanks_people_data():
    out = scrub("Claire Thornhill 555-123-4567 claire@x.com at HEB", ["Claire Thornhill"])
    assert "Claire" not in out and "Thornhill" not in out
    assert "555" not in out and "@" not in out
    assert "HEB" in out


# ─── Point → state, offline ─────────────────────────────────────────


@pytest.mark.parametrize(
    "lat, lng, code",
    [
        (30.2672, -97.7431, "TX"),  # Austin
        (34.0522, -118.2437, "CA"),  # Los Angeles
        (47.6062, -122.3321, "WA"),  # Seattle
        (25.7617, -80.1918, "FL"),  # Miami
        (40.7128, -74.0060, "NY"),  # Manhattan
        (21.3069, -157.8583, "HI"),  # Honolulu
        (61.2181, -149.9003, "AK"),  # Anchorage
        (38.8977, -77.0365, "DC"),
        (41.8917, -87.6086, "IL"),  # Navy Pier: past the simplified shore
    ],
)
def test_state_at_known_points(lat, lng, code):
    hit = state_at(lat, lng)
    assert hit is not None and hit.code == code


def test_state_at_border_points_report_the_distance_to_the_line():
    # Country Club Plaza, Kansas City MO: ~1.5 km from State Line Rd.
    plaza = state_at(39.0425, -94.5900)
    assert plaza.code == "MO" and 1.0 < plaza.border_km < 5.0
    kck = state_at(39.1142, -94.6275)
    assert kck.code == "KS" and kck.border_km < 5.0
    # Bristol TN on State St, which IS the TN/VA line.
    assert state_at(36.5940, -82.1850).border_km < 1.0
    # Far inland: nowhere near another state.
    assert state_at(30.2672, -97.7431).border_km >= 20


@pytest.mark.parametrize(
    "lat, lng",
    [(43.65, -79.38), (32.5149, -117.0382), (18.4655, -66.1057), (0, 0), (None, None)],
)
def test_state_at_outside_the_states(lat, lng):
    # Toronto, Tijuana, San Juan PR (a territory), null island, garbage.
    assert state_at(lat, lng) is None


# ─── Through the database ───────────────────────────────────────────

BREAKDOWN_Q = """
query ConsumerBreakdownQuery($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantConsumerBreakdown(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    total
    recaps
    storeCount
    stateCount
    byWeek { weekStart consumers recaps }
    byStore { label kind consumers recaps }
    byState { label kind consumers recaps }
  }
}
"""

MARKET_Q = """
query TenantMarketPerformanceQuery($tenantId: ID!, $year: Int, $startDate: String, $endDate: String) {
  tenantMarketPerformance(tenantId: $tenantId, year: $year, startDate: $startDate, endDate: $endDate) {
    state
    eventCount
    recapCount
    consumersReached
    samplesDistributed
    productsSold
    totalEngagements
  }
}
"""


def _reply(obj: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(obj)}}]}


@pytest.mark.django_db(transaction=True)
class TestRecapLocation(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Location Brand", slug="location-brand")
        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="loc-retail", tenant=self.tenant, created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Loc Retail", event_type=self.et, tenant=self.tenant, created_by=self.sys
        )
        self.text = recap_models.CustomRecapFieldType.objects.create(name="Text", created_by=self.sys)
        self.section = recap_models.RecapSection.objects.create(
            name="Details", tenant=self.tenant, created_by=self.sys
        )
        self.fields = {}
        for name in (
            "Total number of consumers sampled",
            "Store",
            "Store #",
            "City",
            "State",
            "BA phone",
            "Store manager name",
        ):
            self.fields[name] = recap_models.CustomField.objects.create(
                name=name, custom_recap_template=self.template, custom_field_type=self.text,
                recap_section=self.section, created_by=self.sys,
            )
        self.tx = event_models.State.objects.create(name="Texas", code="TX", created_by=self.sys)
        self.ca = event_models.State.objects.create(name="California", code="CA", created_by=self.sys)
        self.request_type = event_models.RequestType.objects.create(
            name="Retail", tenant=self.tenant, created_by=self.sys
        )
        ba_user = self.create_user(
            username="claire@ba.com", email="claire@ba.com", role=self.roles["ambassador"]
        )
        ba_user.first_name, ba_user.last_name = "Claire", "Thornhill"
        ba_user.save(update_fields=["first_name", "last_name"])
        self.ba = self.create_ambassador(ba_user)

    # helpers

    def _event(self, *, day=6, address="", name="Demo", **kwargs):
        return event_models.Event.objects.create(
            name=name, tenant=self.tenant, address=address,
            date=timezone.make_aware(datetime(2026, 10, day, 18)), event_type=self.et,
            created_by=self.sys, updated_by=self.sys, **kwargs,
        )

    def _recap(self, event, answers: dict | None = None, **kwargs):
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=True, submitted_at=event.date, event=event, tenant=self.tenant,
            custom_recap_template=self.template, created_by=self.sys, ambassador=self.ba, **kwargs,
        )
        for field_name, value in (answers or {}).items():
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=self.fields[field_name], value=value,
                created_by=self.sys,
            )
        return recap

    def _request(self, **kwargs):
        kwargs.setdefault("address", "")
        return event_models.Request.objects.create(
            name="Gig", request_type=self.request_type, tenant=self.tenant,
            created_by=self.sys, date=timezone.make_aware(datetime(2026, 10, 6, 18)), **kwargs,
        )

    def _run(self, **kwargs):
        kwargs.setdefault("use_ai", False)
        return run(self.tenant, **kwargs)

    def _row(self, report, recap):
        return next((r for r in report["rows"] if r["recap_id"] == recap.id), None)

    # deterministic

    def test_address_and_store_answer_fill_everything(self):
        recap = self._recap(
            self._event(request=self._request(address="1 Congress Ave, Austin, TX 78701")),
            {"Store": "heb #614", "Total number of consumers sampled": "40"},
        )
        report = self._run()
        row = self._row(report, recap)
        assert row["changes"] == {
            "retailer": "H-E-B", "store_number": "614", "city": "Austin", "state_code": "TX",
        }
        assert row["sources"]["state_code"] == "request_address"
        assert row["sources"]["retailer"] == "store_field"
        assert row["confidence"]["retailer"] == "high"

    def test_dry_run_writes_nothing(self):
        recap = self._recap(self._event(request=self._request(address="1 Congress Ave, Austin, TX 78701")), {"Store": "heb"})
        before_locations = event_models.Location.objects.count()
        report = self._run()
        assert report["applied"] is False and report["recaps_with_changes"] == 1
        recap.refresh_from_db()
        assert recap.retailer_id is None and recap.state_id is None
        assert recap.location_id is None and recap.store_number == ""
        assert recap.location_fill == {}
        assert not event_models.Retailer.objects.filter(tenant=self.tenant).exists()
        assert event_models.Location.objects.count() == before_locations

    def test_apply_writes_and_a_second_run_is_a_no_op(self):
        recap = self._recap(
            self._event(request=self._request(address="1 Congress Ave, Austin, TX 78701")), {"Store": "H-E-B 540"}
        )
        first = self._run(apply=True)
        assert first["recaps_written"] == 1
        recap.refresh_from_db()
        assert recap.retailer.name == "H-E-B"
        assert recap.store_number == "540"
        assert recap.state.code == "TX"
        assert recap.location.name == "Austin" and recap.location.state_id == self.tx.id
        assert recap.location_fill["fields"]["retailer"]["source"] == "store_field"
        assert first["created_retailers"] == ["H-E-B"]

        second = self._run(apply=True)
        assert second["recaps_with_changes"] == 0 and second["recaps_written"] == 0
        assert event_models.Retailer.objects.filter(tenant=self.tenant).count() == 1

    def test_unnormalized_retailer_is_replaced_but_a_hand_set_one_is_kept(self):
        heb = event_models.Retailer.objects.create(name="H-E-B", tenant=self.tenant, created_by=self.sys)
        lower = event_models.Retailer.objects.create(name="heb", tenant=self.tenant, created_by=self.sys)
        typed = self._recap(self._event(request=self._request(retailer_name="heb")))
        on_event = self._recap(self._event(retailer=lower))
        by_hand = self._recap(self._event(), retailer=lower)
        report = self._run(apply=True)

        typed.refresh_from_db()
        on_event.refresh_from_db()
        by_hand.refresh_from_db()
        assert typed.retailer_id == heb.id
        assert on_event.retailer_id == heb.id
        assert on_event.event.retailer_id == lower.id  # the shared event is untouched
        assert by_hand.retailer_id == lower.id
        notes = self._row(report, by_hand)["notes"]
        assert any("set on the recap by hand" in n for n in notes)

    def test_store_number_answer(self):
        recap = self._recap(self._event(), {"Store": "Kroger", "Store #": "#0412"})
        row = self._row(self._run(), recap)
        assert row["changes"] == {"retailer": "Kroger", "store_number": "412"}

    def test_answers_fill_city_and_state(self):
        recap = self._recap(self._event(), {"City": "austin", "State": "Texas"})
        row = self._row(self._run(), recap)
        assert row["changes"] == {"city": "Austin", "state_code": "TX"}

    def test_sources_that_disagree_leave_the_state_blank(self):
        recap = self._recap(self._event(request=self._request(address="1 Congress Ave, Austin, TX 78701")), {"State": "CA"})
        row = self._row(self._run(), recap)
        assert "state_code" not in row["changes"] and "city" not in row["changes"]
        assert any("disagree" in n for n in row["notes"])

    def test_event_address_state_is_already_filled(self):
        # Events take their State FK from their address on save, so the
        # state is there already; only the city is new.
        event = self._event(address="1 Congress Ave, Austin, TX 78701")
        assert event.state_id == self.tx.id
        recap = self._recap(event)
        row = self._row(self._run(), recap)
        assert row["before"]["state_code"] == "TX"
        assert row["changes"] == {"city": "Austin"}

    def test_existing_values_are_never_overwritten(self):
        loc = event_models.Location.objects.create(
            name="Dallas", code="TX", zip="75201", state=self.tx, created_by=self.sys
        )
        event = self._event(address="1 Main St, Los Angeles, CA 90001")
        # Set by hand, disagreeing with the address: kept as it is.
        event_models.Event.objects.filter(pk=event.pk).update(state=self.tx, location=loc)
        recap = self._recap(event)
        row = self._row(self._run(), recap)
        assert row is None or not row["changes"]

    def test_unknown_store_is_reported_not_invented(self):
        recap = self._recap(self._event(), {"Store": "Joe's Corner Shop"})
        row = self._row(self._run(), recap)
        assert "retailer" not in row["changes"]
        assert any("not a known chain" in n for n in row["notes"])

    # AI

    def _ai_recap(self):
        return self._recap(
            self._event(name="Demo with Claire Thornhill at the big grocery on Lamar, Austin TX"),
            {
                "Store": "the h e b plus off lamar store 77",
                "BA phone": "512-555-0199",
                "Store manager name": "Bob Mgr bob@store.com",
            },
        )

    @override_settings(OPENAI_API_KEY="test-key")
    def test_ai_fills_only_what_the_text_supports_and_sees_no_people_data(self):
        recap = self._recap(
            self._event(name="Demo with Claire Thornhill at the big grocery on Lamar, Austin TX"),
            {"Store": "big grocery off lamar", "BA phone": "512-555-0199",
             "Store manager name": "Bob Mgr bob@store.com"},
        )
        wrong = self._recap(self._event(name="Sampling at the corner market"), {"Store": "corner market"})
        calls = []

        def _post(url, payload, headers, *, timeout):
            calls.append(payload)
            return _reply({"recaps": [
                {"id": recap.id, "retailer": "HEB", "store_number": None, "city": "Austin",
                 "state_code": "TX", "confidence": "high"},
                # Nothing in this recap's text names a state or a city: dropped.
                {"id": wrong.id, "retailer": "Kroger", "store_number": "99", "city": "Portland",
                 "state_code": "OR", "confidence": "medium"},
            ]})

        with patch("utils.ai_text._post_json", side_effect=_post):
            report = self._run(use_ai=True)

        assert report["ai"]["calls"] == 1
        row = self._row(report, recap)
        assert row["changes"] == {"retailer": "H-E-B", "city": "Austin", "state_code": "TX"}
        assert row["sources"] == {"retailer": "ai", "city": "ai", "state_code": "ai"}
        assert set(row["confidence"].values()) == {"medium"}
        wrong_row = self._row(report, wrong)
        assert "state_code" not in wrong_row["changes"]
        assert "city" not in wrong_row["changes"]
        assert "store_number" not in wrong_row["changes"]

        body = json.dumps(calls[0])
        for secret in ("Claire", "Thornhill", "512-555", "0199", "bob@store.com", "Bob Mgr", "claire@ba.com"):
            assert secret not in body, secret
        assert "lamar" in body.lower()

    @override_settings(OPENAI_API_KEY="")
    def test_ai_unavailable_still_runs_the_deterministic_rules(self):
        recap = self._recap(self._event(request=self._request(address="1 Congress Ave, Austin, TX 78701")), {"Store": "big grocery"})
        with patch("utils.ai_text._post_json") as post:
            report = self._run(use_ai=True)
        post.assert_not_called()
        assert report["ai"]["status"].startswith("unavailable")
        row = self._row(report, recap)
        assert row["changes"] == {"city": "Austin", "state_code": "TX"}

    @override_settings(OPENAI_API_KEY="test-key")
    def test_ai_low_confidence_and_garbage_are_ignored(self):
        recap = self._recap(self._event(name="Sampling, Austin TX"), {"Store": "a store"})

        def _post(url, payload, headers, *, timeout):
            return _reply({"recaps": [
                {"id": recap.id, "retailer": "HEB", "store_number": None, "city": "Austin",
                 "state_code": "TX", "confidence": "low"},
            ]})

        with patch("utils.ai_text._post_json", side_effect=_post):
            report = self._run(use_ai=True)
        row = self._row(report, recap)
        assert row is None or not row["changes"]

    # Clock-in GPS

    AUSTIN = (30.2672, -97.7431)
    LOS_ANGELES = (34.0522, -118.2437)

    def _clock_in(self, event, point, *, ambassador=None, accuracy=None, ping=False):
        from ambassadors.models import Attendance, LocationPing, Source

        amb = ambassador or self.ba
        if ping:
            return LocationPing.objects.create(
                ambassador=amb, event=event, lat=point[0], lng=point[1],
                accuracy_meters=accuracy, recorded_at=event.date, source="clock_in",
            )
        source, _ = Source.objects.get_or_create(name="clock_in")
        return Attendance.objects.create(
            clock_time=event.date, coordinates=list(point), ambassador=amb, event=event, source=source,
        )

    def test_clockin_gps_fills_a_blank_state(self):
        event = self._event(name="Walk-up")
        self._clock_in(event, self.AUSTIN)
        recap = self._recap(event)
        report = self._run(apply=True)
        row = self._row(report, recap)
        assert row["changes"] == {"state_code": "TX"}
        assert row["sources"]["state_code"] == "clockin_gps"
        assert row["confidence"]["state_code"] == "high"
        assert report["by_source"] == {"clockin_gps": 1}
        recap.refresh_from_db()
        assert recap.state_id == self.tx.id
        assert recap.location_fill["fields"]["state_code"]["source"] == "clockin_gps"
        assert recap.event.state_id is None  # the shared event is untouched

    def test_clockin_gps_places_the_recaps_own_city_at_medium(self):
        event = self._event(name="Walk-up")
        self._clock_in(event, self.AUSTIN)
        recap = self._recap(event, {"City": "austin"})
        row = self._row(self._run(), recap)
        assert row["changes"] == {"city": "Austin", "state_code": "TX"}
        assert row["sources"] == {"city": "city_field", "state_code": "clockin_gps"}
        assert row["confidence"]["city"] == "medium"

    def test_clockin_gps_city_listed_only_in_another_state_is_left_blank(self):
        event_models.Location.objects.create(
            name="Portland", code="OR", zip="97201", state=event_models.State.objects.create(
                name="Oregon", code="OR", created_by=self.sys), created_by=self.sys,
        )
        event = self._event(name="Walk-up")
        self._clock_in(event, self.AUSTIN)
        recap = self._recap(event, {"City": "Portland"})
        row = self._row(self._run(), recap)
        assert row["changes"] == {"state_code": "TX"}
        assert any("listed only in OR" in n for n in row["notes"])

    def test_clockin_gps_agreeing_with_an_address_keeps_the_address_source(self):
        event = self._event(request=self._request(address="1 Congress Ave, Austin, TX 78701"))
        self._clock_in(event, self.AUSTIN)
        recap = self._recap(event)
        row = self._row(self._run(), recap)
        assert row["changes"]["state_code"] == "TX"
        assert row["sources"]["state_code"] == "request_address"

    def test_clockin_gps_disagreeing_with_an_address_blanks_the_state(self):
        event = self._event(request=self._request(address="1 Main St, Los Angeles, CA 90001"))
        self._clock_in(event, self.AUSTIN)
        recap = self._recap(event)
        report = self._run(apply=True)
        row = self._row(report, recap)
        assert "state_code" not in row["changes"] and "city" not in row["changes"]
        assert any("disagree" in n and "clockin_gps" in n for n in row["notes"])
        recap.refresh_from_db()
        assert recap.state_id is None and recap.location_id is None

    def test_no_clockin_gps_leaves_the_recap_unchanged(self):
        event = self._event(name="Walk-up")
        from ambassadors.models import Attendance, Source

        Attendance.objects.create(  # a clock-in with no fix
            clock_time=event.date, coordinates=None, ambassador=self.ba, event=event,
            source=Source.objects.get_or_create(name="clock_in")[0],
        )
        recap = self._recap(event)
        row = self._row(self._run(apply=True), recap)
        assert row is None or not row["changes"]
        recap.refresh_from_db()
        assert recap.state_id is None and recap.location_fill == {}

    def test_another_bas_clockin_and_a_recap_with_a_state_are_ignored(self):
        other_user = self.create_user(username="o@ba.com", email="o@ba.com", role=self.roles["ambassador"])
        other = self.create_ambassador(other_user)
        event = self._event(name="Walk-up")
        self._clock_in(event, self.AUSTIN, ambassador=other)
        recap = self._recap(event)
        row = self._row(self._run(), recap)
        assert row is None or not row["changes"]

        has_state = self._recap(self._event(name="Walk-up 2"), state=self.ca)
        self._clock_in(has_state.event, self.AUSTIN)
        row = self._row(self._run(), has_state)
        assert row is None or not row["changes"]

    def test_poor_accuracy_far_from_event_and_border_fixes_are_not_used(self):
        poor_event = self._event(name="Walk-up")
        self._clock_in(poor_event, self.AUSTIN, ping=True, accuracy=5000)
        self._clock_in(poor_event, self.AUSTIN)  # the same fix, on the Attendance row
        poor = self._recap(poor_event)

        far_event = self._event(name="Walk-up", coordinates=list(self.LOS_ANGELES))
        self._clock_in(far_event, self.AUSTIN)
        far = self._recap(far_event)

        border_event = self._event(name="Walk-up")
        self._clock_in(border_event, (36.5940, -82.1850))  # Bristol, on the TN/VA line
        border = self._recap(border_event)

        good_ping_event = self._event(name="Walk-up")
        self._clock_in(good_ping_event, self.LOS_ANGELES, ping=True, accuracy=20)
        good_ping = self._recap(good_ping_event)

        report = self._run()
        for recap, phrase in ((poor, "accuracy"), (far, "from the event"), (border, "state line")):
            row = self._row(report, recap)
            assert "state_code" not in row["changes"], phrase
            assert any(phrase in n for n in row["notes"]), (phrase, row["notes"])
        assert self._row(report, good_ping)["changes"] == {"state_code": "CA"}

    def test_clockin_gps_near_a_border_is_medium(self):
        event = self._event(name="Walk-up")
        self._clock_in(event, (39.0425, -94.5900))  # Kansas City MO, ~1.5 km from KS
        recap = self._recap(event)
        row = self._row(self._run(), recap)
        assert row["changes"] == {"state_code": "MO"}
        assert row["confidence"]["state_code"] == "medium"

    @override_settings(OPENAI_API_KEY="test-key")
    def test_clockin_gps_state_is_settled_before_the_model(self):
        event = self._event(name="Sampling at the corner market")
        self._clock_in(event, self.AUSTIN)
        recap = self._recap(event, {"Store": "corner market"})
        calls = []

        def _post(url, payload, headers, *, timeout):
            calls.append(payload)
            return _reply({"recaps": [
                {"id": recap.id, "retailer": None, "store_number": None, "city": None,
                 "state_code": "OR", "confidence": "high"},
            ]})

        with patch("utils.ai_text._post_json", side_effect=_post):
            report = self._run(use_ai=True)
        row = self._row(report, recap)
        assert row["changes"]["state_code"] == "TX"
        assert row["sources"]["state_code"] == "clockin_gps"

    # Reports read what was written

    def _client(self):
        user = self.create_user(
            username="c@loc.com", email="c@loc.com", role=self.roles["client"], password="password123"
        )
        self.create_tenanted_user(user=user, tenant=self.tenant)
        return user

    def _seed_for_reports(self):
        # Before: no store and no state anywhere a report reads.
        self._recap(
            self._event(day=6, name="Sampling"),
            {"Store": "HEB #614", "City": "Austin", "State": "TX",
             "Total number of consumers sampled": "40"},
        )
        return self._client()

    @pytest.mark.asyncio
    async def test_filled_values_flow_into_the_store_and_state_reports(self):
        me = await sync_to_async(self._seed_for_reports)()
        variables = {"tenantId": str(self.tenant.id), "startDate": "2026-10-01", "endDate": "2026-10-31"}

        before = await self._execute_mutation(BREAKDOWN_Q, variables, self.endpoint_path, user=me)
        assert before.errors is None, before.errors
        b = before.data["tenantConsumerBreakdown"]
        assert [(r["label"], r["kind"]) for r in b["byStore"]] == [("", "missing")]
        assert [(r["label"], r["kind"]) for r in b["byState"]] == [("", "missing")]
        market = await self._execute_mutation(MARKET_Q, variables, self.endpoint_path, user=me)
        assert market.errors is None, market.errors
        assert market.data["tenantMarketPerformance"] == []

        await sync_to_async(lambda: run(self.tenant, apply=True, use_ai=False))()

        after = await self._execute_mutation(BREAKDOWN_Q, variables, self.endpoint_path, user=me)
        assert after.errors is None, after.errors
        a = after.data["tenantConsumerBreakdown"]
        assert a["total"] == 40
        assert [(r["label"], r["kind"], r["consumers"]) for r in a["byStore"]] == [
            ("H-E-B #614", "named", 40)
        ]
        assert [(r["label"], r["kind"], r["consumers"]) for r in a["byState"]] == [
            ("TX", "named", 40)
        ]
        market = await self._execute_mutation(MARKET_Q, variables, self.endpoint_path, user=me)
        assert market.errors is None, market.errors
        rows = market.data["tenantMarketPerformance"]
        assert [(r["state"], r["recapCount"], r["consumersReached"]) for r in rows] == [("TX", 1, 40)]

    def test_field_export_reads_the_recap_city_state_and_retailer(self):
        from recaps.recap_field_export import build_recap_field_export

        self._recap(self._event(name="Sampling"), {"Store": "HEB #614", "City": "Austin", "State": "TX"})
        run(self.tenant, apply=True, use_ai=False)
        payload = build_recap_field_export(self.tenant, resolve_heic=False)
        keys = [c["key"] for c in payload["columns"]]
        row = dict(zip(keys, payload["rows"][0]))
        assert (row["city"], row["state"], row["retailer"]) == ("Austin", "Texas", "H-E-B")

    # Command and endpoint

    def test_command_is_a_dry_run_by_default(self):
        recap = self._recap(self._event(request=self._request(address="1 Congress Ave, Austin, TX 78701")), {"Store": "heb"})
        out = StringIO()
        call_command("normalize_recap_locations", tenant="location-brand", no_ai=True, stdout=out)
        text = out.getvalue()
        assert "applied=False" in text and "[WOULD-SET]" in text and "Thornhill" not in text
        recap.refresh_from_db()
        assert recap.retailer_id is None

    @override_settings(INTERNAL_CRON_SECRET="s3cret")
    def test_cron_endpoint_is_secret_gated_and_dry_run_by_default(self):
        recap = self._recap(self._event(request=self._request(address="1 Congress Ave, Austin, TX 78701")), {"Store": "heb"})
        http = DjangoClient()
        url = "/internal/cron/normalize-recap-locations"
        assert http.post(url, {"tenant": "location-brand"}).status_code in (401, 403)
        res = http.post(
            url, {"tenant": str(self.tenant.id), "use_ai": "false"}, HTTP_X_CRON_SECRET="s3cret"
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["applied"] is False and body["recaps_with_changes"] == 1
        recap.refresh_from_db()
        assert recap.retailer_id is None

        res = http.post(
            url, {"tenant": "location-brand", "use_ai": "false", "apply": "true"},
            HTTP_X_CRON_SECRET="s3cret",
        )
        assert res.status_code == 200 and res.json()["recaps_written"] == 1
        recap.refresh_from_db()
        assert recap.retailer.name == "H-E-B"
        assert http.post(url, {}, HTTP_X_CRON_SECRET="s3cret").status_code == 400
