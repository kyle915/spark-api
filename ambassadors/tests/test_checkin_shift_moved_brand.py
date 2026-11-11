"""A shift whose event changes brand after clock-in.

FAMUS, Nov 2026: an ambassador clocked in on the FAMUS standing link, the
event was then put under the sister brand, and submit answered "This session
doesn't match the link." — nothing she could act on, and nobody at the office
knew. Now she is told what happened, her session and draft stay, and the
hours inbox hears about it once.
"""
from __future__ import annotations

import uuid
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestShiftMovedToOtherBrand(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Famus {uid}")
        self.tenant.checkin_code = f"FM-{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])
        self.sister = self.create_tenant(name=f"Wook {uid}")
        ba_user = self.create_user(
            username=f"ba-{uid}@test.com",
            email=f"ba-{uid}@test.com",
            role=self.roles["ambassador"],
            first_name="Dana",
            last_name="R",
        )
        self.ba = self.create_ambassador(ba_user)
        self.event = Event.objects.create(
            tenant=self.tenant,
            name="7939 N Main St, Dayton",
            address="7939 N Main St, Dayton, OH 45415",
            date=checkin_web._event_date_utc(dj_tz.localdate()),
            created_by=self.system_user,
        )
        self.http = DjangoClient()

    def _get(self, token):
        return self.http.get(
            reverse("events.public_checkin_context", kwargs={"code": self.tenant.checkin_code}),
            HTTP_X_CHECKIN_SESSION=token,
        )

    def _move_event(self):
        Event.objects.filter(id=self.event.id).update(tenant=self.sister)

    def test_moved_shift_is_explained_and_staff_are_told_once(self):
        checkin_web.ensure_walkup_booking(self.event, self.ba, actor=self.ba.user)
        assert AmbassadorEvent.objects.filter(
            ambassador=self.ba, event=self.event, tenant=self.tenant
        ).exists()
        token = make_checkin_session_token(self.event.id, self.ba.id)
        self._move_event()

        with mock.patch.object(
            checkin_web, "_email_admins_shift_moved_brand"
        ) as send:
            from events.checkin_views import _load_session

            loaded, err = _load_session(self.tenant.checkin_code, token)
            assert loaded is None
            assert err.status_code == 409
            body = err.content.decode()
            assert "moved_brand" in body
            assert self.sister.name in body
            # A second call (every later tap) does not email again.
            _load_session(self.tenant.checkin_code, token)
            assert send.call_count == 1
            event, ambassador, link_tenant = send.call_args.args
            assert (event.id, ambassador.id, link_tenant.id) == (
                self.event.id, self.ba.id, self.tenant.id,
            )

        # The page keeps the session: the code is not one it treats as dead.
        with mock.patch.object(checkin_web, "_email_admins_shift_moved_brand"):
            assert self._get(token).json().get("sessionError") == "moved_brand"

    def test_a_token_for_someone_elses_brand_is_still_just_a_mismatch(self):
        # No booking under this link's brand: not a moved shift, no alert.
        other_event = Event.objects.create(
            tenant=self.sister,
            name="Elsewhere",
            address="1 Other St",
            date=checkin_web._event_date_utc(dj_tz.localdate()),
            created_by=self.system_user,
        )
        token = make_checkin_session_token(other_event.id, self.ba.id)
        with mock.patch.object(
            checkin_web, "_email_admins_shift_moved_brand"
        ) as send:
            from events.checkin_views import _load_session

            loaded, err = _load_session(self.tenant.checkin_code, token)
            assert loaded is None
            assert err.status_code == 401
            assert "mismatch" in err.content.decode()
            assert send.call_count == 0
