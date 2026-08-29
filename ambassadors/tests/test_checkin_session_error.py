"""Standing GET must say WHY a bearer failed — not silently look unidentified.

Applies to every tenant standing code (FF / LD / TH / BD / …). Michelle Chin
(Feel Free, Aug 2026): clocked in, opened the photo-release QR, came back to
"not clocked in". The page wiped localStorage whenever GET returned bare
tenant context, and the API never said the token was dead vs briefly
unresolved. Surface ``sessionError`` so the front only drops a real dead token
and can resume an open punch via identify.
"""
import datetime as _dt
import uuid

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event


STANDING_CODE_PREFIXES = ("FF", "LD", "TH", "BD")


@pytest.mark.django_db(transaction=True)
class TestStandingContextSessionError(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Standing {uid}")
        self.tenant.checkin_code = f"FF-{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])
        ba_user = self.create_user(
            username=f"ba-{uid}",
            email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"],
            first_name="Michelle",
            last_name="Chin",
        )
        self.ambassador = self.create_ambassador(ba_user)
        self.http = DjangoClient()

    def _event(self, on_date, *, name="Tampa / St. Pete, FL", address=None):
        return Event.objects.create(
            tenant=self.tenant,
            name=name,
            address=address or name,
            date=checkin_web._event_date_utc(on_date),
            created_by=self.system_user,
        )

    def _mint_standing(self, prefix: str):
        uid = str(uuid.uuid4())[:6].upper()
        self.tenant.checkin_code = f"{prefix}-{uid}"
        self.tenant.save(update_fields=["checkin_code"])
        return self.tenant.checkin_code

    def test_valid_token_still_returns_session(self):
        today = dj_tz.localdate()
        event = self._event(today)
        source, _ = Source.objects.get_or_create(name="clock_in")
        Attendance.objects.create(
            ambassador=self.ambassador,
            event=event,
            source=source,
            clock_time=dj_tz.now() - _dt.timedelta(hours=1),
        )
        token = make_checkin_session_token(event.id, self.ambassador.id)
        res = self.http.get(
            reverse(
                "events.public_checkin_context",
                kwargs={"code": self.tenant.checkin_code},
            ),
            HTTP_X_CHECKIN_SESSION=token,
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["mode"] == "tenant"
        assert body["session"]["clock"]["state"] == "clocked_in"
        assert "sessionError" not in body

    def test_bad_token_returns_session_error_not_silent_tenant(self):
        res = self.http.get(
            reverse(
                "events.public_checkin_context",
                kwargs={"code": self.tenant.checkin_code},
            ),
            HTTP_X_CHECKIN_SESSION="not-a-real-token",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["mode"] == "tenant"
        assert body.get("session") is None
        assert body.get("sessionError") in {"bad_session", "expired"}

    def test_no_token_has_no_session_error(self):
        res = self.http.get(
            reverse(
                "events.public_checkin_context",
                kwargs={"code": self.tenant.checkin_code},
            ),
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["mode"] == "tenant"
        assert "sessionError" not in body

    @pytest.mark.parametrize("prefix", STANDING_CODE_PREFIXES)
    def test_session_error_on_every_standing_prefix(self, prefix):
        """LD / Torch / Brew Dr / Feel Free all surface sessionError the same way."""
        code = self._mint_standing(prefix)
        res = self.http.get(
            reverse("events.public_checkin_context", kwargs={"code": code}),
            HTTP_X_CHECKIN_SESSION="garbage-bearer",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["mode"] == "tenant"
        assert body.get("session") is None
        assert body.get("sessionError") in {"bad_session", "expired"}

    def test_address_mode_open_punch_still_resumes_via_identify(self):
        """Torch / LD-style store address: soft-miss resume lands on same event."""
        code = self._mint_standing("TH")
        today = dj_tz.localdate()
        store = "Total Wine & More, 5200 Burnet Rd, Austin, TX"
        event = self._event(today, name=store, address=store)
        stub, _ = checkin_web.get_or_create_checkin_ambassador(
            first_name="Joy",
            last_name="H",
            phone="5550100444",
            email=None,
        )
        source, _ = Source.objects.get_or_create(name="clock_in")
        Attendance.objects.create(
            ambassador=stub,
            event=event,
            source=source,
            clock_time=dj_tz.now() - _dt.timedelta(hours=2),
        )
        # Slightly different typed address — open punch must still win.
        res = self.http.post(
            reverse("events.public_checkin_identify", kwargs={"code": code}),
            data={
                "firstName": "Joy",
                "lastName": "H",
                "phone": "5550100444",
                "eventDate": today.isoformat(),
                "address": "Total Wine Burnet Austin",
            },
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        assert res.json()["event"]["uuid"] == str(event.uuid)
