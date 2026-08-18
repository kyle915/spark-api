"""Recap-only standing link: 3rd-party / agency filing with no time clock.

A tenant's `checkin_code` is the BA walk-up (clock in, stops, recap).
`checkin_recap_code` is a second URL on the same `/checkin/<code>` page that
skips punch and goes name + date + store + the same recap questions.

The load-bearing behaviours:

* minting the recap code must NOT repoint the BA clock URL
* clock / ping / sampling-stop refuse the recap code
* a past date still find-or-creates (agencies never clocked in)
* identify does not require a phone
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event
from recaps.models import (
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileType,
)


@pytest.mark.django_db(transaction=True)
class TestRecapOnlyCheckinLink(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Torch Recap {uid}")
        self.clock_code = f"TH-{uid.upper()}"
        self.recap_code = f"THA-{uid.upper()}"
        self.tenant.checkin_code = self.clock_code
        self.tenant.checkin_recap_code = self.recap_code
        self.tenant.save(update_fields=["checkin_code", "checkin_recap_code"])

        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.tenant.checkin_event_type = etype
        self.tenant.save(update_fields=["checkin_event_type"])
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Torch THC-Retail Sampling",
            event_type=etype,
            created_by=self.system_user,
        )
        FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.system_user}
        )
        CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.system_user}
        )
        self.http = DjangoClient()

    def _identify(self, **extra):
        body = {
            "firstName": "Alex",
            "lastName": "Agency",
            "eventDate": dj_tz.localdate().isoformat(),
            "address": "1648 NW Chipman Road, LEE'S SUMMIT, MO 64081",
            "storeName": "Total Wine & More (Lee's Summit)",
        }
        body.update(extra)
        return self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.recap_code},
            ),
            data=body,
            content_type="application/json",
        )

    def test_recap_code_resolves_as_tenant_without_shadowing_clock_code(self):
        kind, target = checkin_web.resolve_checkin_target(self.recap_code)
        assert kind == "tenant"
        assert target.id == self.tenant.id
        assert checkin_web.is_recap_only_code(self.recap_code, target)

        kind, target = checkin_web.resolve_checkin_target(self.clock_code)
        assert kind == "tenant"
        assert not checkin_web.is_recap_only_code(self.clock_code, target)

    def test_tenant_context_flags_recap_only_and_lists_stores(self):
        Event.objects.create(
            tenant=self.tenant,
            name="Torch Sampling - Total Wine & More (Lee's Summit)",
            address="1648 NW Chipman Road, LEE'S SUMMIT, MO 64081",
            created_by=self.system_user,
        )
        Event.objects.create(
            tenant=self.tenant,
            name="Torch Sampling - Total Wine & More (Lee's Summit)",
            address="1648 nw chipman road, lee's summit, mo 64081",
            created_by=self.system_user,
        )
        payload = checkin_web.build_tenant_context(self.tenant, recap_only=True)
        assert payload["recapOnly"] is True
        assert payload["mode"] == "tenant"
        addrs = [s["address"] for s in payload["recentLocations"]]
        keys = {checkin_web.normalize_place(a) for a in addrs}
        assert len(keys) == len(addrs)
        assert any("chipman" in checkin_web.normalize_place(a) for a in addrs)
        names = [s["name"] for s in payload["recentLocations"]]
        assert any("Total Wine" in n for n in names)

    def test_identify_without_phone_creates_a_session(self):
        res = self._identify()
        assert res.status_code == 200, res.content
        body = res.json()
        assert body.get("sessionToken")
        assert body.get("recapOnly") is True
        assert body["event"]["address"]
        assert "clock" in (body.get("session") or {})

    def test_identify_past_date_creates_event_without_a_clock(self):
        past = dj_tz.localdate() - _dt.timedelta(days=5)
        res = self._identify(eventDate=past.isoformat())
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["event"]["date"].startswith(past.isoformat())

    def test_clock_code_still_refuses_a_past_date_without_a_punch(self):
        past = dj_tz.localdate() - _dt.timedelta(days=5)
        res = self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.clock_code},
            ),
            data={
                "firstName": "Alex",
                "lastName": "Agency",
                "phone": "5550100999",
                "eventDate": past.isoformat(),
                "address": "1648 NW Chipman Road, LEE'S SUMMIT, MO 64081",
            },
            content_type="application/json",
        )
        assert res.status_code == 400
        assert "No check-in found for that date" in res.json()["message"]

    def test_clock_endpoint_refuses_the_recap_link(self):
        identified = self._identify()
        token = identified.json()["sessionToken"]
        res = self.http.post(
            reverse(
                "events.public_checkin_clock",
                kwargs={"code": self.recap_code},
            ),
            data={"session": token, "kind": "in"},
            content_type="application/json",
        )
        assert res.status_code == 403
        assert res.json()["error"] == "recap_only"
        assert "no time clock" in res.json()["message"].lower()

    def test_sampling_stop_refuses_the_recap_link(self):
        identified = self._identify()
        token = identified.json()["sessionToken"]
        res = self.http.post(
            reverse(
                "events.public_checkin_sampling_stop",
                kwargs={"code": self.recap_code},
            ),
            data={"session": token, "name": "Aisle 4"},
            content_type="application/json",
        )
        assert res.status_code == 403
        assert res.json()["error"] == "recap_only"

    def test_second_recap_does_not_overwrite_the_first(self):
        identified = self._identify()
        body = identified.json()
        token = body["sessionToken"]
        event = Event.objects.get(uuid=body["event"]["uuid"])
        blob = f"recap_files/checkin/{event.uuid}/shot.jpg"

        first = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": self.recap_code},
            ),
            data={
                "session": token,
                "fieldValues": [],
                "files": [{"blobName": blob}],
            },
            content_type="application/json",
        )
        assert first.status_code == 200, first.content
        second = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": self.recap_code},
            ),
            data={
                "session": token,
                "fieldValues": [],
                "files": [{"blobName": blob.replace("shot", "shot2")}],
            },
            content_type="application/json",
        )
        assert second.status_code == 200, second.content
        assert CustomRecap.objects.filter(event=event).count() == 2
