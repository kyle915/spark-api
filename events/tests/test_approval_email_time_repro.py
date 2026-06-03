"""
Regression coverage for the approval-email activation time (REQ-1072: an
Encinitas / Pacific request whose email showed 3:00 AM).

Findings the repro nailed down:
  * The email faithfully renders whatever start_time is STORED. A correctly
    stored 3 PM Pacific (22:00 UTC) renders "3:00 PM"; a value stored 12h off
    (10:00 UTC) renders "3:00 AM" — i.e. REQ-1072's time was mis-captured at
    input, not mangled by the email.
  * BEFORE the fix, a request with NO TimeZone row rendered its raw UTC time
    ("10:00 PM"). NOW it falls back to the activation's state (parsed from the
    address) so it renders LOCAL time ("3:00 PM").
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone as djtz

from events import models as event_models
from events.envelopes import RequestorRequestApprovedMailer
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db
class TestApprovalEmailTime(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death")
        self.request_type = self.create_request_type(
            name="Retail Sampling", tenant=self.tenant
        )
        self.pacific = event_models.TimeZone.objects.create(
            name="Pacific Time", code="PST", offset=-8, created_by=self.system_user
        )
        # 3:00 PM Pacific on 2026-06-13 (PDT, -7) == 22:00 UTC — a CORRECT store.
        self.correct_3pm = djtz.make_aware(
            dt.datetime(2026, 6, 13, 22, 0), dt.timezone.utc
        )
        # 3:00 AM Pacific == 10:00 UTC — a store that is 12h off (PM dropped).
        self.stored_12h_off = djtz.make_aware(
            dt.datetime(2026, 6, 13, 10, 0), dt.timezone.utc
        )

    def _make_request(self, *, start_utc, tz):
        return event_models.Request.objects.create(
            name="Walmart 5886 Encinitas",
            address="1550 Leucadia Blvd Encinitas CA 92024",
            request_type=self.request_type,
            tenant=self.tenant,
            timezone=tz,
            date=start_utc,
            start_time=start_utc,
            end_time=start_utc + dt.timedelta(hours=3),
            created_by=self.system_user,
        )

    def _rendered_start(self, req):
        env = RequestorRequestApprovedMailer(
            request=req, location=None, to_emails=["x@example.com"]
        ).envelope()
        return env.context["request_start_time"]

    def test_explicit_timezone_renders_local_time(self):
        req = self._make_request(start_utc=self.correct_3pm, tz=self.pacific)
        assert self._rendered_start(req) == "3:00 PM"

    def test_no_timezone_falls_back_to_state_from_address(self):
        # THE FIX: no TimeZone row, but the address says "...Encinitas CA
        # 92024" → Pacific → renders local 3:00 PM instead of raw UTC 10 PM.
        req = self._make_request(start_utc=self.correct_3pm, tz=None)
        assert self._rendered_start(req) == "3:00 PM"

    def test_email_faithfully_renders_a_mis_captured_time(self):
        # A time STORED 12h off still renders 3:00 AM — confirming the email
        # is faithful and the REQ-1072 bug is upstream at input/capture.
        req = self._make_request(start_utc=self.stored_12h_off, tz=self.pacific)
        assert self._rendered_start(req) == "3:00 AM"
