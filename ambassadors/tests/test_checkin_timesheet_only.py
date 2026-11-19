"""Time-sheet-only check-in: a plain time clock, no recap, nothing chasing one.

Seven Hills Hospitality only needs who worked (name + mobile phone), when
(event date, clock in, clock out) and where (the phone's GPS, reverse-
geocoded). `Tenant.checkin_timesheet_only` turns the standing link into
exactly that. The load-bearing behaviours:

* the page payload carries no recap form, photo buckets, product questions,
  resources or store-name requirement — even for a "Retail Sampling" program
* identify needs only name + phone + where + date (no store name)
* clock in / out work exactly as on any web check-in
* the recap endpoint refuses (a stale page or replayed queue can't file one)
* no recap is ever owed: Missing Recaps, recap texts and the unfiled list
  all skip these shifts
* setup_tenant_checkin --timesheet-only on/off flips it
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client as DjangoClient
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import AmbassadorEvent, Attendance, ShiftSms, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event, TimeZone
from tenants.models import Tenant

# Fictional numbers (555-01xx is reserved for fiction).
PHONE = "(555) 010-0142"
ADDRESS = "100 Example Ave, Springfield, IL 62701, USA"


@pytest.mark.django_db(transaction=True)
class TestTimesheetOnlyCheckin(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:6].upper()
        self.tenant = self.create_tenant(name=f"Hospitality Test {uid}")
        self.code = f"HT-{uid}"
        # Retail Sampling would normally demand a store name on a new event.
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.tenant.checkin_code = self.code
        self.tenant.checkin_event_type = etype
        self.tenant.checkin_timesheet_only = True
        self.tenant.checkin_resources = [
            {"label": "Training", "kind": "link", "url": "https://example.com/t"}
        ]
        self.tenant.save(
            update_fields=[
                "checkin_code", "checkin_event_type", "checkin_timesheet_only",
                "checkin_resources",
            ]
        )
        self.http = DjangoClient()

    # -- helpers -------------------------------------------------------------

    def _url(self, name):
        return reverse(f"events.public_checkin_{name}", kwargs={"code": self.code})

    def _identify(self, **extra):
        body = {
            "firstName": "Test",
            "lastName": "Worker",
            "phone": PHONE,
            "eventDate": dj_tz.localdate().isoformat(),
            "address": ADDRESS,
        }
        body.update(extra)
        return self.http.post(
            self._url("identify"), data=body, content_type="application/json"
        )

    def _clock(self, session, kind):
        return self.http.post(
            self._url("clock"),
            data={"session": session, "kind": kind, "latitude": 39.78, "longitude": -89.65},
            content_type="application/json",
        )

    # -- payloads ------------------------------------------------------------

    def test_landing_payload_is_a_time_clock(self):
        payload = checkin_web.build_tenant_context(self.tenant)
        assert payload["timesheetOnly"] is True
        assert payload["storeNameRequired"] is False
        assert payload["resources"] == []
        assert payload["trainingUrl"] == ""
        assert payload["locationMode"] == "address"

    def test_market_mode_is_ignored_for_a_time_clock(self):
        self.tenant.checkin_location_mode = Tenant.CHECKIN_LOCATION_MARKET
        self.tenant.checkin_markets = ["Springfield, IL"]
        self.tenant.save(update_fields=["checkin_location_mode", "checkin_markets"])
        assert checkin_web.tenant_location_mode(self.tenant) == "address"
        assert checkin_web.build_tenant_context(self.tenant)["markets"] == []

    def test_normal_tenant_is_untouched(self):
        self.tenant.checkin_timesheet_only = False
        self.tenant.save(update_fields=["checkin_timesheet_only"])
        payload = checkin_web.build_tenant_context(self.tenant)
        assert payload["timesheetOnly"] is False
        assert payload["storeNameRequired"] is True
        assert payload["resources"] != []

    # -- identify → clock → done -------------------------------------------

    def test_name_phone_and_gps_address_is_enough(self):
        res = self._identify()
        assert res.status_code == 200, res.content
        data = res.json()
        assert data["timesheetOnly"] is True
        assert data["template"] is None
        assert data["photoBuckets"] == []
        assert data["resources"] == []
        assert "productQa" not in data
        assert data["unfiledShifts"] == []
        event = Event.objects.get(uuid=data["event"]["uuid"])
        assert event.tenant_id == self.tenant.id
        assert event.address == ADDRESS
        assert event.date.date() == dj_tz.localdate()
        # Walk-up rule unchanged: pending until staff confirm it.
        booking = AmbassadorEvent.objects.get(event=event)
        assert booking.is_approved is False
        assert booking.ambassador.phone == PHONE

    def test_clock_in_out_then_recap_is_refused(self):
        session = self._identify().json()["sessionToken"]
        assert self._clock(session, "in").json()["clock"]["state"] == "clocked_in"
        assert self._clock(session, "out").json()["clock"]["state"] == "clocked_out"
        res = self.http.post(
            self._url("recap"),
            data={"session": session, "fieldValues": [], "files": []},
            content_type="application/json",
        )
        assert res.status_code == 409
        assert res.json()["error"] == "timesheet_only"

    def test_locations_not_stores(self):
        """Event locations: title is date + address, no retailer, and the
        event runs the tenant's EVENT program even though the link was pinned
        to Retail Sampling."""
        event_type = self.create_event_type("Event", self.tenant)
        data = self._identify(storeName="Total Wine & More").json()
        event = Event.objects.get(uuid=data["event"]["uuid"])
        today = dj_tz.localdate()
        assert event.name == f"{today.month}/{today.day}/{today.year} - {ADDRESS}"
        assert event.retailer_id is None
        assert event.event_type_id == event_type.id

    def test_setup_command_pins_an_event_program(self):
        from django.core.management.base import CommandError

        event_type = self.create_event_type("Event", self.tenant)
        self.create_event_type("On-Premise Sampling", self.tenant)
        call_command(
            "setup_tenant_checkin", tenant_id=self.tenant.id,
            timesheet_only="on", apply=True, stdout=io.StringIO(),
        )
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_event_type_id == event_type.id
        with pytest.raises(CommandError):
            call_command(
                "setup_tenant_checkin", tenant_id=self.tenant.id,
                timesheet_only="on", event_types=["Retail Sampling"],
                stdout=io.StringIO(),
            )

    # -- nothing chases a recap ---------------------------------------------

    def test_no_recap_is_owed(self):
        from recaps.dismissals import DismissedLookup, is_recap_dismissed
        from recaps.filed import missing_recap_events_queryset

        data = self._identify().json()
        session = data["sessionToken"]
        event = Event.objects.get(uuid=data["event"]["uuid"])
        amb_id = AmbassadorEvent.objects.get(event=event).ambassador_id
        self._clock(session, "in")
        self._clock(session, "out")

        assert is_recap_dismissed(ambassador_id=amb_id, event_id=event.id)
        assert DismissedLookup([event.id]).covers(event.id, amb_id)
        later = dj_tz.now() + timedelta(days=1)
        assert not missing_recap_events_queryset(now=later).filter(id=event.id).exists()

        # Same shift on a normal brand IS owed — the rule is the flag.
        self.tenant.checkin_timesheet_only = False
        self.tenant.save(update_fields=["checkin_timesheet_only"])
        assert not is_recap_dismissed(ambassador_id=amb_id, event_id=event.id)

    @override_settings(
        SHIFT_SMS_ENABLED=True,
        QUO_API_KEY="test-key",
        QUO_FROM_NUMBER="+15550100100",
        SHIFT_SMS_EVENTS_FROM="",
    )
    def test_no_recap_texts(self):
        from ambassadors.shift_sms import send_shift_sms

        zone = TimeZone.objects.create(name="America/Chicago", code="", offset=-360)
        now = datetime(2026, 11, 10, 15, 0, tzinfo=ZoneInfo("America/Chicago"))
        user = self.create_user(
            username="ts-ba@test.com", email="ts-ba@test.com",
            role=self.roles["ambassador"], first_name="Test", last_name="Worker",
        )
        ba = self.create_ambassador(user)
        ba.phone = PHONE
        ba.save(update_fields=["phone"])
        event = self.create_event(
            name="Shift", tenant=self.tenant, address=ADDRESS, date=now,
        )
        Event.objects.filter(id=event.id).update(
            start_time=now - timedelta(hours=5), end_time=now - timedelta(hours=1),
            timezone=zone,
        )
        for kind, when in (("clock_in", now - timedelta(hours=5)), ("clock_out", now - timedelta(hours=1))):
            src, _ = Source.objects.get_or_create(name=kind)
            Attendance.objects.create(ambassador=ba, event=event, source=src, clock_time=when)

        sent = []
        result = send_shift_sms(
            now=now, send=lambda **kw: sent.append(kw) or "x", opted_out=lambda to: False
        )
        assert result["sent"] == 0 and sent == []
        assert not ShiftSms.objects.exists()

        # Control: the same shift on a normal brand gets the recap text.
        self.tenant.checkin_timesheet_only = False
        self.tenant.save(update_fields=["checkin_timesheet_only"])
        result = send_shift_sms(
            now=now, send=lambda **kw: sent.append(kw) or "x", opted_out=lambda to: False
        )
        assert result["sent"] == 1

    # -- setup command ---------------------------------------------------------

    def test_setup_command_turns_it_on_and_off(self):
        self.create_event_type("Event", self.tenant)
        self.tenant.checkin_timesheet_only = False
        self.tenant.checkin_location_mode = Tenant.CHECKIN_LOCATION_MARKET
        self.tenant.save(update_fields=["checkin_timesheet_only", "checkin_location_mode"])

        out = io.StringIO()
        call_command(
            "setup_tenant_checkin", tenant_id=self.tenant.id,
            timesheet_only="on", stdout=out,
        )
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_timesheet_only is False  # dry run
        assert "Time sheet only: ON" in out.getvalue()

        call_command(
            "setup_tenant_checkin", tenant_id=self.tenant.id,
            timesheet_only="on", apply=True, stdout=io.StringIO(),
        )
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_timesheet_only is True
        assert self.tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert self.tenant.checkin_code == self.code  # never re-minted

        # Omitted = leave as-is.
        call_command(
            "setup_tenant_checkin", tenant_id=self.tenant.id, apply=True,
            event_types=["Event"], stdout=io.StringIO(),
        )
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_timesheet_only is True

        call_command(
            "setup_tenant_checkin", tenant_id=self.tenant.id,
            timesheet_only="off", apply=True, event_types=["Event"],
            stdout=io.StringIO(),
        )
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_timesheet_only is False

    def test_setup_endpoint_passes_timesheet_only(self):
        with override_settings(INTERNAL_CRON_SECRET="test-cron-secret-value-only-for-tests"):
            res = self.http.post(
                "/internal/cron/setup-tenant-checkin",
                data={
                    "tenant_id": str(self.tenant.id),
                    "timesheet_only": "off",
                    "apply": "true",
                },
                HTTP_X_CRON_SECRET="test-cron-secret-value-only-for-tests",
            )
        assert res.status_code == 200, res.content
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_timesheet_only is False
