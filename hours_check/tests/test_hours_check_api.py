"""payrollHoursCheck + markHoursFlagReviewed through the real clients schema
(the web app's endpoint): staff-only access, tenant scoping, facts gathered
from stored punches / recaps / pings, and the review mark persisting."""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from events import models as em
from events.tests.base import EventsGraphQLTestCase

CHECK = """
query Check($s: String!, $e: String!, $tid: ID!) {
  payrollHoursCheck(startDate: $s, endDate: $e, tenantId: $tid) {
    totalShifts flaggedShifts needsLookShifts
    byRule { code count unreviewed }
    thresholds { key value }
    shifts {
      eventUuid ambassadorUuid needsLook
      flags { code severity summary action reviewed staleReview reviewedBy reviewedAt
              facts { label value } }
    }
  }
}
"""

MARK = """
mutation Mark($input: MarkHoursFlagReviewedInput!) {
  markHoursFlagReviewed(input: $input) { success message reviewedBy reviewedAt }
}
"""


@pytest.mark.django_db(transaction=True)
class TestHoursCheckApi(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from ambassadors.models import Ambassador
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Hours Brand")
        self.other = self.create_tenant(name="Other Brand")
        self.admin = self.create_user(
            username="hc-admin", email="hc-admin@igniteproductions.co",
            role=self.roles["spark_admin"], is_staff=True,
            first_name="Olive", last_name="Ops",
        )
        self.client_user = self.create_user(
            username="hc-client", email="hc-client@brand.com", role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.ba_user = self.create_user(
            username="hc-ba", email="hc-ba@test.com", role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant)
        self.ba = Ambassador.objects.create(user=self.ba_user, created_by=self.sys)
        # Yesterday 10:00 UTC, so every shift below is over.
        self.day = (timezone.now() - timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

    # -- fixtures -----------------------------------------------------------
    def _shift(self, *, tenant=None, hours=4, start_offset_h=0, coords=(30.2672, -97.7431), name="HEB"):
        from ambassadors.models import AmbassadorEvent

        tenant = tenant or self.tenant
        start = self.day + timedelta(hours=start_offset_h)
        ev = em.Event.objects.create(
            name=name, tenant=tenant, date=start, start_time=start,
            end_time=start + timedelta(hours=hours),
            coordinates=list(coords) if coords else None, created_by=self.sys,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.ba, event=ev, tenant=tenant, is_approved=True,
            created_by=self.sys,
        )
        return ev

    def _punch(self, ev, kind, at, coords=(30.2672, -97.7431), **kw):
        from ambassadors.models import Attendance, Source

        src, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            ambassador=self.ba, event=ev, source=src, clock_time=at,
            coordinates=list(coords) if coords else None, **kw,
        )

    def _recap(self, ev, submitted_at):
        from recaps.models import CustomRecap, CustomRecapTemplate

        etype = self.create_event_type("Demo", self.tenant)
        tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant, name="T", event_type=etype, created_by=self.sys
        )
        return CustomRecap.objects.create(
            tenant=self.tenant, event=ev, ambassador=self.ba,
            custom_recap_template=tpl, name="Recap",
            submitted_at=submitted_at, created_by=self.sys,
        )

    async def _check(self, who=None, tenant=None):
        d = self.day.date()
        return await self._execute_mutation(
            CHECK,
            {
                "s": (d - timedelta(days=2)).isoformat(),
                "e": (d + timedelta(days=2)).isoformat(),
                "tid": str((tenant or self.tenant).id),
            },
            self.endpoint_path,
            user=who if who is not None else self.admin,
        )

    async def _mark(self, ev, code, *, who=None, tenant=None, undo=False, note=None):
        return await self._execute_mutation(
            MARK,
            {"input": {
                "tenantId": str((tenant or self.tenant).id),
                "eventUuid": str(ev.uuid),
                "ambassadorUuid": str(self.ba.uuid),
                "code": code, "undo": undo, "note": note,
            }},
            self.endpoint_path,
            user=who if who is not None else self.admin,
        )

    @staticmethod
    def _codes(data, ev):
        for s in data["payrollHoursCheck"]["shifts"]:
            if s["eventUuid"] == str(ev.uuid):
                return {f["code"]: f for f in s["flags"]}
        return {}

    # -- access ------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_brand_user_and_ba_are_refused(self):
        ev = await sync_to_async(self._shift)()
        await sync_to_async(self._punch)(ev, "clock_in", self.day, coords=(31.0, -97.0))
        for who in (self.client_user, self.ba_user):
            res = await self._check(who=who)
            assert res.errors, who
            assert res.data is None or res.data.get("payrollHoursCheck") is None
            res = await self._mark(ev, "far_clock_in", who=who)
            assert res.errors
        assert not await sync_to_async(
            lambda: __import__("hours_check.models").models.HoursFlagReview.objects.exists()
        )()

    # -- rules over stored data ------------------------------------------
    @pytest.mark.asyncio
    async def test_padded_far_away_shift_is_flagged_with_facts(self):
        def seed():
            ev = self._shift(hours=4)
            self._punch(ev, "clock_in", self.day + timedelta(seconds=12), coords=(30.40, -97.7431))
            self._punch(ev, "clock_out", self.day + timedelta(hours=6, seconds=40))
            self._recap(ev, self.day + timedelta(hours=1))
            return ev

        ev = await sync_to_async(seed)()
        res = await self._check()
        assert res.errors is None, res.errors
        rep = res.data["payrollHoursCheck"]
        assert rep["totalShifts"] == 1 and rep["flaggedShifts"] == 1 and rep["needsLookShifts"] == 1
        flags = self._codes(res.data, ev)
        assert {"over_booked", "far_clock_in", "late_clock_out", "recap_before_clock_out"} <= set(flags)
        assert flags["over_booked"]["severity"] == "high"
        assert any(f["label"] == "Over by" for f in flags["over_booked"]["facts"])
        assert "no_recap" not in flags
        assert {r["code"] for r in rep["byRule"]} >= {"over_booked", "far_clock_in"}
        assert any(t["key"] == "far_miles" for t in rep["thresholds"])

    @pytest.mark.asyncio
    async def test_clean_shift_is_not_listed(self):
        def seed():
            ev = self._shift(hours=4)
            self._punch(ev, "clock_in", self.day + timedelta(seconds=12))
            self._punch(ev, "clock_out", self.day + timedelta(hours=4, seconds=3))
            self._recap(ev, self.day + timedelta(hours=4, minutes=5))

        await sync_to_async(seed)()
        rep = (await self._check()).data["payrollHoursCheck"]
        assert rep["totalShifts"] == 1 and rep["flaggedShifts"] == 0 and rep["shifts"] == []

    @pytest.mark.asyncio
    async def test_auto_closed_and_staff_edited_punches(self):
        def seed():
            ev = self._shift(hours=4)
            cin = self._punch(ev, "clock_in", self.day + timedelta(seconds=9))
            cin.updated_by = self.admin
            cin.save()
            # What auto_clock_out_stale_shifts writes: scheduled end, no GPS, no author.
            self._punch(ev, "clock_out", ev.end_time, coords=None)
            return ev

        ev = await sync_to_async(seed)()
        flags = self._codes((await self._check()).data, ev)
        assert "auto_closed" in flags
        assert "edited_punch" in flags
        assert "Olive Ops" in flags["edited_punch"]["facts"][0]["value"]
        assert "no_recap" in flags

    @pytest.mark.asyncio
    async def test_overlap_with_a_shift_for_another_client(self):
        def seed():
            ev = self._shift(hours=4)
            self._punch(ev, "clock_in", self.day + timedelta(seconds=5))
            self._punch(ev, "clock_out", self.day + timedelta(hours=4, seconds=5))
            other = self._shift(tenant=self.other, hours=4, start_offset_h=1, name="Target")
            self._punch(other, "clock_in", self.day + timedelta(hours=1, seconds=5))
            self._punch(other, "clock_out", self.day + timedelta(hours=5, seconds=5))
            return ev

        ev = await sync_to_async(seed)()
        rep = (await self._check()).data["payrollHoursCheck"]
        # Only this client's shift is checked and listed…
        assert rep["totalShifts"] == 1
        flags = self._codes({"payrollHoursCheck": rep}, ev)
        # …but the other client's punches still count against it.
        assert "overlap" in flags

    # -- tenant scoping -----------------------------------------------------
    @pytest.mark.asyncio
    async def test_other_clients_shifts_are_not_reported(self):
        def seed():
            ev = self._shift(tenant=self.other, hours=4)
            self._punch(ev, "clock_in", self.day + timedelta(seconds=5), coords=(31.5, -97.0))
            return ev

        ev = await sync_to_async(seed)()
        rep = (await self._check()).data["payrollHoursCheck"]
        assert rep["totalShifts"] == 0 and rep["shifts"] == []
        # And a review can't be filed through the wrong client.
        res = await self._mark(ev, "far_clock_in", tenant=self.tenant)
        assert res.data["markHoursFlagReviewed"]["success"] is False

    # -- review mark ---------------------------------------------------------
    @pytest.mark.asyncio
    async def test_mark_reviewed_persists_until_the_facts_change(self):
        from hours_check.models import HoursFlagReview

        def seed():
            ev = self._shift(hours=4)
            self._punch(ev, "clock_in", self.day + timedelta(seconds=5))
            # 45 min over the booking, under the 60 min "late" line.
            out = self._punch(ev, "clock_out", self.day + timedelta(hours=4, minutes=45, seconds=5))
            self._recap(ev, self.day + timedelta(hours=4, minutes=47))
            return ev, out

        ev, out = await sync_to_async(seed)()
        res = await self._mark(ev, "over_booked", note="Stayed for restock, RMM ok'd")
        assert res.errors is None, res.errors
        body = res.data["markHoursFlagReviewed"]
        assert body["success"] is True and body["reviewedBy"] == "Olive Ops" and body["reviewedAt"]

        row = await sync_to_async(HoursFlagReview.objects.get)(event=ev, code="over_booked")
        assert row.reviewed_by_id == self.admin.id and row.tenant_id == self.tenant.id

        rep = (await self._check()).data["payrollHoursCheck"]
        flag = self._codes({"payrollHoursCheck": rep}, ev)["over_booked"]
        assert flag["reviewed"] is True and flag["reviewedBy"] == "Olive Ops"
        assert rep["needsLookShifts"] == 0  # its only flag is reviewed

        # A later edit pushes the clock-out further: the review no longer covers it.
        def push():
            out.clock_time = self.day + timedelta(hours=6, seconds=5)
            out.save()

        await sync_to_async(push)()
        rep = (await self._check()).data["payrollHoursCheck"]
        flag = self._codes({"payrollHoursCheck": rep}, ev)["over_booked"]
        assert flag["reviewed"] is False and flag["staleReview"] is True
        assert rep["needsLookShifts"] == 1

        # Undo removes the row.
        res = await self._mark(ev, "over_booked", undo=True)
        assert res.data["markHoursFlagReviewed"]["success"] is True
        assert not await sync_to_async(HoursFlagReview.objects.filter(event=ev).exists)()

    @pytest.mark.asyncio
    async def test_cannot_review_a_flag_that_does_not_fire(self):
        def seed():
            ev = self._shift(hours=4)
            self._punch(ev, "clock_in", self.day + timedelta(seconds=5))
            self._punch(ev, "clock_out", self.day + timedelta(hours=4, seconds=5))
            return ev

        ev = await sync_to_async(seed)()
        res = await self._mark(ev, "over_booked")
        assert res.data["markHoursFlagReviewed"]["success"] is False

    @pytest.mark.asyncio
    async def test_recap_filed_far_from_the_venue_uses_the_nearest_ping(self):
        def seed():
            from ambassadors.models import LocationPing

            ev = self._shift(hours=4)
            self._punch(ev, "clock_in", self.day + timedelta(seconds=5))
            self._punch(ev, "clock_out", self.day + timedelta(hours=4, seconds=5))
            filed = self._recap(ev, self.day + timedelta(hours=4, minutes=10)).submitted_at
            # On the road ~9 mi north, 3 minutes after filing.
            LocationPing.objects.create(
                ambassador=self.ba, event=ev, lat=30.40, lng=-97.7431,
                recorded_at=filed + timedelta(minutes=3),
            )
            # An on-site ping long before does not count.
            LocationPing.objects.create(
                ambassador=self.ba, event=ev, lat=30.2672, lng=-97.7431,
                recorded_at=self.day + timedelta(hours=1),
            )
            return ev

        ev = await sync_to_async(seed)()
        flags = self._codes((await self._check()).data, ev)
        assert "recap_far_away" in flags
        assert "no_gps" not in flags
