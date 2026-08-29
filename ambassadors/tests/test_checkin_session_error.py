"""Standing GET must say WHY a bearer failed — not silently look unidentified.

Michelle Chin (Feel Free, Aug 2026): clocked in, opened the photo-release QR,
came back to "not clocked in". The page wiped localStorage whenever GET returned
bare tenant context, and the API never said the token was dead vs briefly
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


@pytest.mark.django_db(transaction=True)
class TestStandingContextSessionError(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Feel Free {uid}")
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

    def _event(self, on_date):
        return Event.objects.create(
            tenant=self.tenant,
            name="Tampa / St. Pete, FL",
            address="Tampa / St. Pete, FL",
            date=checkin_web._event_date_utc(on_date),
            created_by=self.system_user,
        )

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
