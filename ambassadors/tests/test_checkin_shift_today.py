""""Is your shift today?" before a standing link opens a new walk-in event.

A BA opened the walk-up link weeks before her demo. Identify minted a walk-in
event for that day and she clocked in and out on it, which then texted her
for a recap. Now a check-in that would CREATE a walk-in event for today (or
later) must say the BA confirmed the shift is today (``shiftIsToday: true``);
otherwise identify answers 400 ``confirm_shift_today`` and creates nothing.

Joining an event that already exists, recap-only links and past-day recaps
are never asked. The whole check is behind ``CHECKIN_REQUIRE_SHIFT_TODAY`` so
the API can ship before the page that asks the question.
"""
import datetime
import json
import uuid

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event

ON = dict(CHECKIN_REQUIRE_SHIFT_TODAY=True)


@pytest.mark.django_db(transaction=True)
class TestShiftTodayConfirmation(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.sys = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Shift Today {uid}")
        self.clock_code = f"ST{uid.upper()}"
        self.recap_code = f"STR{uid.upper()}"
        self.tenant.checkin_code = self.clock_code
        self.tenant.checkin_recap_code = self.recap_code
        # An activation: the venue name is optional, so the store-name rule
        # never gets in the way of what these tests look at.
        self.etype = self.create_event_type("Event Activation", self.tenant)
        self.tenant.checkin_event_type = self.etype
        self.tenant.save(
            update_fields=["checkin_code", "checkin_recap_code", "checkin_event_type"]
        )
        self.http = DjangoClient()

    def _identify(self, *, code=None, phone="5550142", **extra):
        body = {
            "firstName": "Pat",
            "lastName": "Sampler",
            "phone": phone,
            "eventDate": dj_tz.localdate().isoformat(),
            "address": "100 Test Rd, Testville, TX 75001",
        }
        body.update(extra)
        return self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": code or self.clock_code},
            ),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_off_by_default_so_the_old_page_still_checks_in(self):
        res = self._identify()
        assert res.status_code == 200, res.content
        assert Event.objects.filter(tenant=self.tenant).count() == 1

    @override_settings(**ON)
    def test_a_new_walk_in_without_the_answer_creates_nothing(self):
        res = self._identify()
        assert res.status_code == 400, res.content
        body = res.json()
        assert body["error"] == "confirm_shift_today"
        assert body["message"] == checkin_web.SHIFT_TODAY_CONFIRM_ERROR
        assert body["shiftDate"] == dj_tz.localdate().isoformat()
        assert not Event.objects.filter(tenant=self.tenant).exists()
        assert not AmbassadorEvent.objects.filter(tenant=self.tenant).exists()

    @override_settings(**ON)
    def test_a_false_answer_is_not_a_yes(self):
        res = self._identify(shiftIsToday=False)
        assert res.status_code == 400
        assert res.json()["error"] == "confirm_shift_today"

    @override_settings(**ON)
    def test_yes_im_working_now_opens_the_event(self):
        res = self._identify(shiftIsToday=True)
        assert res.status_code == 200, res.content
        assert res.json()["sessionToken"]
        assert Event.objects.filter(tenant=self.tenant).count() == 1

    @override_settings(**ON)
    def test_joining_an_event_that_already_exists_is_never_asked(self):
        assert self._identify(shiftIsToday=True).status_code == 200
        # A second BA at the same place on the same day joins that event.
        res = self._identify(phone="5550199", firstName="Sam")
        assert res.status_code == 200, res.content
        assert Event.objects.filter(tenant=self.tenant).count() == 1

    @override_settings(**ON)
    def test_a_past_day_recap_is_never_asked(self):
        past = (dj_tz.localdate() - datetime.timedelta(days=5)).isoformat()
        res = self._identify(eventDate=past)
        assert res.status_code == 200, res.content
        assert Event.objects.filter(tenant=self.tenant).count() == 1

    @override_settings(**ON)
    def test_the_recap_only_link_is_never_asked(self):
        res = self._identify(code=self.recap_code, storeName="Test Venue")
        assert res.status_code == 200, res.content


def test_shift_today_confirmed_reads_only_an_explicit_yes():
    assert checkin_web.shift_today_confirmed({"shiftIsToday": True})
    assert checkin_web.shift_today_confirmed({"shiftIsToday": "true"})
    assert not checkin_web.shift_today_confirmed({"shiftIsToday": False})
    assert not checkin_web.shift_today_confirmed({"shiftIsToday": "yes please"})
    assert not checkin_web.shift_today_confirmed({})
