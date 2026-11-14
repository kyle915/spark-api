"""Shift texts: who gets one, when, and never twice.

Pre-shift goes to an approved booking two hours ahead. The recap texts go to
whoever actually clocked in — booked or walk-up — once they have finished
with no recap filed: one 30 minutes later, one the next morning.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.models import AmbassadorEvent, Attendance, ShiftSms, Source
from ambassadors.shift_sms import normalize_us_phone, send_shift_sms
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import TimeZone

CRON_URL = "/internal/cron/shift-sms"
VALID_SECRET = "test-cron-secret-value-only-for-tests"
LIVE = dict(
    SHIFT_SMS_ENABLED=True, QUO_API_KEY="test-key", QUO_FROM_NUMBER="+17755550100"
)
CHICAGO = ZoneInfo("America/Chicago")


def test_normalize_us_phone():
    assert normalize_us_phone("(606) 448-9015") == "+16064489015"
    assert normalize_us_phone("16064489015") == "+16064489015"
    assert normalize_us_phone("+1 606 448 9015") == "+16064489015"
    assert normalize_us_phone("448-9015") is None
    assert normalize_us_phone("0064489015") is None
    assert normalize_us_phone(None) is None


@pytest.mark.django_db(transaction=True)
class TestShiftSms(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Famus Energy")
        self.tenant.checkin_code = "FM-TEST1"
        self.tenant.save(update_fields=["checkin_code"])
        self.zone = TimeZone.objects.create(
            name="America/Chicago", code="", offset=-360
        )
        user = self.create_user(
            username="ba-sms@test.com",
            email="ba-sms@test.com",
            role=self.roles["ambassador"],
            first_name="Dana",
            last_name="Reyes",
        )
        self.ba = self.create_ambassador(user)
        self.ba.phone = "(606) 448-9015"
        self.ba.save(update_fields=["phone"])
        self.etype = self.create_event_type("Retail Sampling", self.tenant)
        # A fixed afternoon in Chicago, so "next morning" is unambiguous.
        self.now = datetime(2026, 11, 10, 15, 0, tzinfo=CHICAGO)
        self.sent: list[dict] = []

    def _send(self, **kwargs):
        self.sent.append(kwargs)
        return "AC-test"

    def _event(self, *, start, end, name="11/10/2026 - Plano Country Store · Retail Sampling"):
        event = self.create_event(
            name=name,
            tenant=self.tenant,
            address="3205 Plano Rd",
            event_type=self.etype,
            date=start,
        )
        type(event).objects.filter(id=event.id).update(
            start_time=start, end_time=end, timezone=self.zone
        )
        event.refresh_from_db()
        return event

    def _book(self, event, approved=True):
        return AmbassadorEvent.objects.create(
            ambassador=self.ba,
            event=event,
            tenant=self.tenant,
            is_approved=approved,
            created_by=self.get_system_user(),
        )

    def _punch(self, event, kind, when):
        source, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            ambassador=self.ba, event=event, source=source, clock_time=when
        )

    def _run(self, **kwargs):
        kwargs.setdefault("now", self.now)
        kwargs.setdefault("send", self._send)
        return send_shift_sms(**kwargs)

    # --- pre-shift -------------------------------------------------------

    @override_settings(**LIVE)
    def test_pre_shift_text_names_brand_venue_local_time_and_link_once(self):
        event = self._event(
            start=self.now + timedelta(minutes=90), end=self.now + timedelta(hours=5)
        )
        self._book(event)
        result = self._run()
        assert result["sent"] == 1
        assert self.sent[0]["to"] == "+16064489015"
        body = self.sent[0]["body"]
        assert "Hi Dana" in body
        assert "Famus Energy shift at Plano Country Store starts at 4:30 PM" in body
        assert "https://client.igniteproductions.co/checkin/FM-TEST1" in body
        assert "Reply STOP" in body
        row = ShiftSms.objects.get()
        assert (row.kind, row.status, row.provider_id) == ("pre_shift", "sent", "AC-test")

        again = self._run()
        assert again["sent"] == 0 and again["already_sent"] == 1
        assert len(self.sent) == 1

    @override_settings(**LIVE)
    def test_pre_shift_skips_unapproved_too_far_out_and_already_clocked_in(self):
        soon = self._event(
            start=self.now + timedelta(minutes=60), end=self.now + timedelta(hours=4)
        )
        self._book(soon, approved=False)
        later = self._event(
            start=self.now + timedelta(hours=3), end=self.now + timedelta(hours=6)
        )
        self._book(later)
        assert self._run()["sent"] == 0

        AmbassadorEvent.objects.filter(event=soon).update(is_approved=True)
        self._punch(soon, "clock_in", self.now - timedelta(minutes=5))
        assert self._run()["sent"] == 0
        assert self.sent == []

    # --- recap due -------------------------------------------------------

    @override_settings(**LIVE)
    def test_recap_text_goes_30_minutes_after_clock_out_to_a_walk_up(self):
        event = self._event(
            start=self.now - timedelta(hours=5), end=self.now - timedelta(hours=1)
        )
        # No booking at all: a walk-up who clocked in from the link.
        self._punch(event, "clock_in", self.now - timedelta(hours=5))
        self._punch(event, "clock_out", self.now - timedelta(minutes=20))
        assert self._run()["sent"] == 0  # only 20 minutes ago

        result = self._run(now=self.now + timedelta(minutes=15))
        assert result["sent"] == 1
        assert ShiftSms.objects.get().kind == "recap_due"
        assert "Your recap isn't in yet" in self.sent[0]["body"]
        assert "/checkin/FM-TEST1" in self.sent[0]["body"]

    @override_settings(**LIVE)
    def test_no_recap_text_while_on_the_clock_or_once_filed(self):
        from recaps.models import CustomRecap, CustomRecapTemplate

        open_event = self._event(
            start=self.now - timedelta(hours=1), end=self.now + timedelta(hours=2)
        )
        self._punch(open_event, "clock_in", self.now - timedelta(hours=1))

        done = self._event(
            start=self.now - timedelta(hours=6), end=self.now - timedelta(hours=2)
        )
        self._punch(done, "clock_in", self.now - timedelta(hours=6))
        self._punch(done, "clock_out", self.now - timedelta(hours=2))
        actor = self.get_system_user()
        template = CustomRecapTemplate.objects.create(
            tenant=self.tenant, name="T", event_type=self.etype, created_by=actor
        )
        CustomRecap.objects.create(
            tenant=self.tenant,
            event=done,
            ambassador=self.ba,
            custom_recap_template=template,
            name="Filed",
            submitted_at=self.now - timedelta(hours=1),
            created_by=actor,
        )
        result = self._run()
        assert result["sent"] == 0 and self.sent == []

    @override_settings(**LIVE)
    def test_never_clocked_out_counts_from_the_event_end(self):
        event = self._event(
            start=self.now - timedelta(hours=5), end=self.now - timedelta(minutes=45)
        )
        self._punch(event, "clock_in", self.now - timedelta(hours=5))
        assert self._run()["sent"] == 1
        assert ShiftSms.objects.get().kind == "recap_due"

    # --- next morning ----------------------------------------------------

    @override_settings(**LIVE)
    def test_follow_up_goes_once_the_next_morning_local_time(self):
        event = self._event(
            start=self.now - timedelta(hours=5), end=self.now - timedelta(hours=1)
        )
        self._punch(event, "clock_in", self.now - timedelta(hours=5))
        self._punch(event, "clock_out", self.now - timedelta(hours=1))
        self._run()  # recap_due, 3 PM
        assert [r.kind for r in ShiftSms.objects.all()] == ["recap_due"]

        # 5 AM next day in Chicago: more than 12h on, but not morning yet.
        assert self._run(now=self.now + timedelta(hours=14))["sent"] == 0
        # 9:30 AM next day.
        result = self._run(now=self.now + timedelta(hours=18, minutes=30))
        assert result["sent"] == 1
        assert sorted(r.kind for r in ShiftSms.objects.all()) == [
            "recap_due",
            "recap_morning",
        ]
        assert "still missing" in self.sent[-1]["body"]
        # And never again.
        assert self._run(now=self.now + timedelta(hours=20))["sent"] == 0

    # --- guards ----------------------------------------------------------

    @override_settings(**LIVE)
    def test_opt_out_bad_phone_and_missing_link_send_nothing(self):
        event = self._event(
            start=self.now + timedelta(minutes=60), end=self.now + timedelta(hours=4)
        )
        self._book(event)

        self.ba.sms_opt_out = True
        self.ba.save(update_fields=["sms_opt_out"])
        assert self._run()["skipped_opt_out"] == 1

        self.ba.sms_opt_out = False
        self.ba.phone = "448-9015"
        self.ba.save(update_fields=["sms_opt_out", "phone"])
        assert self._run()["skipped_no_phone"] == 1

        self.ba.phone = "6064489015"
        self.ba.save(update_fields=["phone"])
        self.tenant.checkin_code = ""
        self.tenant.save(update_fields=["checkin_code"])
        assert self._run()["skipped_no_link"] == 1
        assert self.sent == [] and not ShiftSms.objects.exists()

    @override_settings(**LIVE)
    def test_a_failed_send_is_recorded_and_not_retried(self):
        event = self._event(
            start=self.now + timedelta(minutes=60), end=self.now + timedelta(hours=4)
        )
        self._book(event)

        def boom(**kwargs):
            raise RuntimeError("Quo returned 402: no credits")

        assert self._run(send=boom)["failed"] == 1
        row = ShiftSms.objects.get()
        assert row.status == "failed" and "402" in row.error
        assert self._run()["sent"] == 0 and self.sent == []

    def test_off_by_default_but_dry_run_still_previews(self):
        event = self._event(
            start=self.now + timedelta(minutes=60), end=self.now + timedelta(hours=4)
        )
        self._book(event)
        result = self._run()
        assert result["enabled"] is False and result["sent"] == 0
        assert self.sent == [] and not ShiftSms.objects.exists()

        preview = self._run(dry_run=True)
        assert preview["would_send"] == 1
        assert preview["preview"][0].startswith("pre_shift → Dana (…9015): Ignite: Hi Dana")
        assert self.sent == [] and not ShiftSms.objects.exists()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_cron_endpoint_needs_the_secret_and_runs_dry(self):
        http = Client()
        assert http.post(CRON_URL).status_code in (401, 403)
        res = http.post(f"{CRON_URL}?dry_run=1", HTTP_X_CRON_SECRET=VALID_SECRET)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True and body["dry_run"] is True
        assert "would send 0" in body["log"]
