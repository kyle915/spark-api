"""Web check-in recap submit is safe to retry, and identify won't fork a BA.

The check-in page keeps a recap on the phone and re-sends it when the answer
to the first send never arrives (one bar of LTE, a Cloud Run 502, iOS killing
the tab mid-request). Every send carries the same ``idempotencyKey``:

* a "file another" (``forceNew``) recap must not be filed twice
* the default submit still runs again (it updates the same recap — that is
  how photos left behind on a slow upload join it)
* a send that failed frees its key so the retry is not stuck "in progress"

Identify keys the BA on the phone, so it now refuses a number that is not 10
digits and asks "Is this you?" when the name matches a BA who has worked the
brand under a different number (FAMUS, Nov 2026: a cell typed one digit
short minted a second account with no shift and no recap draft).
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from events.models import Event
from recaps.models import (
    CheckinRecapSubmission,
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileType,
)


@pytest.mark.django_db(transaction=True)
class TestCheckinRecapIdempotency(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.roles = self.setup_default_roles()
        self.actor = self.get_system_user()
        uid = str(uuid.uuid4())[:8]
        self.uid = uid
        self.tenant = self.create_tenant(name=f"Idem {uid}")
        self.code = f"ID-{uid.upper()}"
        self.tenant.checkin_code = self.code
        self.tenant.save(update_fields=["checkin_code"])
        ba_user = self.create_user(
            username=f"ba-idem-{uid}@test.com",
            email=f"ba-idem-{uid}@test.com",
            role=self.roles["ambassador"],
            first_name="Pat",
            last_name="Sample",
        )
        self.ba = self.create_ambassador(ba_user)
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.tenant.checkin_event_type = etype
        self.tenant.save(update_fields=["checkin_event_type"])
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Idem Sampling",
            event_type=etype,
            created_by=self.actor,
        )
        FileType.objects.get_or_create(name="image", defaults={"created_by": self.actor})
        CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )
        self.event = Event.objects.create(
            tenant=self.tenant,
            name="Store 1",
            address="1 Main St, Radcliff, KY",
            date=checkin_web._event_date_utc(dj_tz.localdate()),
            event_type=etype,
            created_by=self.actor,
        )
        self.token = make_checkin_session_token(self.event.id, self.ba.id)
        self.http = DjangoClient()

    def _post(self, *, key=None, force_new=False, blob="a"):
        body = {
            "session": self.token,
            "fieldValues": [],
            "files": [{"blobName": f"recap_files/checkin/{self.event.uuid}/{blob}.jpg"}],
            "forceNew": force_new,
        }
        if key:
            body["idempotencyKey"] = key
        return self.http.post(
            reverse("events.public_checkin_recap", kwargs={"code": self.code}),
            data=body,
            content_type="application/json",
        )

    def _recaps(self):
        return CustomRecap.objects.filter(event=self.event, ambassador=self.ba)

    def test_retried_file_another_with_same_key_files_once(self):
        first = self._post()  # the shift's own recap
        assert first.status_code == 200, first.content
        key = str(uuid.uuid4())
        a = self._post(key=key, force_new=True, blob="b")
        assert a.status_code == 200, a.content
        assert self._recaps().count() == 2
        # The phone never saw that answer and sends again.
        b = self._post(key=key, force_new=True, blob="b")
        assert b.status_code == 200
        assert b.json()["already"] is True
        assert b.json()["success"] is True
        assert self._recaps().count() == 2

    def test_file_another_without_a_key_still_files_a_new_recap(self):
        assert self._post().status_code == 200
        assert self._post(force_new=True, blob="b").status_code == 200
        assert self._post(force_new=True, blob="c").status_code == 200
        assert self._recaps().count() == 3

    def test_default_submit_retry_runs_again_on_the_same_recap(self):
        key = str(uuid.uuid4())
        assert self._post(key=key).status_code == 200
        res = self._post(key=key, blob="late")
        assert res.status_code == 200
        assert res.json().get("already") is not True
        assert self._recaps().count() == 1
        sub = CheckinRecapSubmission.objects.get(ambassador=self.ba, key=key)
        assert sub.recap_id == self._recaps().get().id

    def test_failed_send_frees_its_key(self):
        key = str(uuid.uuid4())
        res = self.http.post(
            reverse("events.public_checkin_recap", kwargs={"code": self.code}),
            data={"session": self.token, "fieldValues": [], "files": [],
                  "idempotencyKey": key, "forceNew": True},
            content_type="application/json",
        )
        assert res.status_code == 400  # needs a photo
        assert not CheckinRecapSubmission.objects.filter(key=key).exists()
        assert self._post(key=key, force_new=True).status_code == 200

    def test_in_flight_claim_answers_retryable_503(self):
        key = str(uuid.uuid4())
        CheckinRecapSubmission.objects.create(key=key, ambassador=self.ba, event=self.event)
        res = self._post(key=key, force_new=True)
        assert res.status_code == 503
        assert res.json()["error"] == "in_progress"
        assert self._recaps().count() == 0

    def test_stale_claim_is_taken_over(self):
        key = str(uuid.uuid4())
        claim = CheckinRecapSubmission.objects.create(
            key=key, ambassador=self.ba, event=self.event
        )
        CheckinRecapSubmission.objects.filter(pk=claim.pk).update(
            created_at=dj_tz.now() - _dt.timedelta(minutes=10)
        )
        res = self._post(key=key, force_new=True)
        assert res.status_code == 200, res.content
        assert self._recaps().count() == 1
        claim.refresh_from_db()
        assert claim.recap_id is not None

    # -- identify ---------------------------------------------------------

    def _identify(self, **extra):
        body = {
            "firstName": "Pat",
            "lastName": "Sample",
            "phone": "5025550129",
            "eventDate": dj_tz.localdate().isoformat(),
            "address": "1 Main St, Radcliff, KY",
            "storeName": "Brook's Market",
            "shiftIsToday": True,
        }
        body.update(extra)
        return self.http.post(
            reverse("events.public_checkin_identify", kwargs={"code": self.code}),
            data=body,
            content_type="application/json",
        )

    def test_identify_refuses_a_short_phone(self):
        res = self._identify(phone="502555012")
        assert res.status_code == 400
        assert res.json()["error"] == "bad_phone"

    def test_identify_accepts_us_country_code(self):
        res = self._identify(phone="+1 (502) 555-0129")
        assert res.status_code == 200, res.content

    def test_identify_asks_is_this_you_for_same_name_other_number(self):
        first = self._identify()
        assert first.status_code == 200, first.content
        # Booked on this brand under 5025550129. Same name, one digit off:
        res = self._identify(phone="5025550128")
        assert res.status_code == 400
        body = res.json()
        assert body["error"] == "confirm_identity"
        assert body["phoneHint"] == "29"
        assert "5025550129" not in res.content.decode()
        # "No, I'm someone else" goes through.
        res = self._identify(phone="5025550128", newIdentityConfirmed=True)
        assert res.status_code == 200, res.content

    def test_same_number_is_not_asked(self):
        assert self._identify().status_code == 200
        assert self._identify().status_code == 200

    def test_name_without_a_booking_on_this_brand_is_not_asked(self):
        # self.ba is "Pat Sample" but has never been booked on this brand.
        assert (
            checkin_web.same_name_other_phone_hint(
                tenant=self.tenant, first_name="Pat", last_name="Sample", phone="5025550000"
            )
            is None
        )
