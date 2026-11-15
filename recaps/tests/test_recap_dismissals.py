"""Dismissing a missing recap: "no recap is owed here".

Staff dismiss a Missing Recaps row that will never get a recap. These tests
run the web app's own documents (``fe_documents``) through the real clients
schema, and the real reminder commands, and check that a dismissed row is
gone from every place that counts or chases a missing recap — and back in
all of them once restored. They also hold the two promises around it: only
Ignite staff can dismiss or see dismissals, and a dismissal changes nothing
but its own record (hours, bookings, recaps and KPIs are left alone).
"""

import io
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.core.management import call_command

from ambassadors.models import AmbassadorEvent, Attendance, PushDevice, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as em
from recaps import inputs as recap_inputs
from recaps.dismissals import is_recap_dismissed
from recaps.models import Recap, RecapDismissal
from events.tests import fe_documents as fe


@pytest.mark.django_db(transaction=True)
class TestRecapDismissals(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Dismissal Brand")
        self.staff = self.create_user(
            username="staff-dismiss",
            email="staff-dismiss@igniteproductions.co",
            role=self.roles["spark_admin"],
            first_name="Sam",
            last_name="Staff",
        )
        self.brand_user = self.create_user(
            username="brand-dismiss",
            email="brand-dismiss@brand.test",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.brand_user, self.tenant)
        self.ba_user, self.ba = self._ba("ba-one", "Rue", "Vance")
        self.ba2_user, self.ba2 = self._ba("ba-two", "Kit", "Marlow")

    # -- fixtures ----------------------------------------------------------

    def _ba(self, username, first, last):
        user = self.create_user(
            username=username,
            email=f"{username}@test.com",
            role=self.roles["ambassador"],
            first_name=first,
            last_name=last,
        )
        PushDevice.objects.create(
            user=user, token=f"ExponentPushToken[{username}]", platform="android"
        )
        return user, self.create_ambassador(user)

    def _past_event(self, *, hours_ago=3, name="Past shift", request=None):
        end = datetime.now(_tz.utc) - timedelta(hours=hours_ago)
        return self.create_event(
            name=name,
            tenant=self.tenant,
            date=end,
            start_time=end - timedelta(hours=4),
            end_time=end,
            **({"request": request} if request is not None else {}),
        )

    def _book(self, event, ambassador=None, approved=True):
        return AmbassadorEvent.objects.create(
            ambassador=ambassador or self.ba,
            event=event,
            tenant=self.tenant,
            is_approved=approved,
            created_by=self.staff,
        )

    def _seed_one(self, **kwargs):
        event = self._past_event(**kwargs)
        return event, self._book(event)

    # -- the web app's calls ----------------------------------------------

    async def _gql(self, document, variables, user):
        return await self._execute_mutation(
            document, variables, self.endpoint_path, user=user
        )

    async def _missing(self, user, tenant_id="default"):
        res = await self._gql(
            fe.MISSING_Q,
            {
                "tenantId": str(self.tenant.id) if tenant_id == "default" else tenant_id,
                "lookbackDays": 30,
            },
            user,
        )
        assert res.errors is None, res.errors
        return res.data["missingRecapEvents"]

    async def _badge(self, user):
        res = await self._gql(fe.SIDEBAR_Q, {"tenantId": str(self.tenant.id)}, user)
        assert res.errors is None, res.errors
        return res.data["sidebarRequestCounts"]["recapsDue"]

    async def _dismissed(self, user=None):
        res = await self._gql(
            fe.DISMISSED_Q,
            {"tenantId": str(self.tenant.id), "lookbackDays": 30},
            user or self.staff,
        )
        assert res.errors is None, res.errors
        return res.data["dismissedMissingRecaps"]

    async def _dismiss(self, *, rosters=(), events=(), reason="did_not_work",
                       note=None, user=None):
        res = await self._gql(
            fe.DISMISS_M,
            {
                "input": {
                    "ambassadorEventUuids": [str(r.uuid) for r in rosters],
                    "eventUuids": [str(e.uuid) for e in events],
                    "reason": reason,
                    "note": note,
                }
            },
            user or self.staff,
        )
        assert res.errors is None, res.errors
        return res.data["dismissMissingRecaps"]

    async def _restore(self, uuids, user=None):
        res = await self._gql(
            fe.RESTORE_M, {"input": {"dismissalUuids": list(uuids)}}, user or self.staff
        )
        assert res.errors is None, res.errors
        return res.data["restoreMissingRecaps"]

    # -- list, badge, dismissed tab, restore ------------------------------

    @pytest.mark.asyncio
    async def test_dismissed_row_leaves_list_and_badge_and_restore_returns_it(self):
        event, roster = await sync_to_async(self._seed_one)(name="No-show shift")

        assert [r["eventName"] for r in await self._missing(self.staff)] == [
            "No-show shift"
        ]
        assert await self._badge(self.staff) == 1
        assert await self._badge(self.brand_user) == 1

        out = await self._dismiss(rosters=[roster], reason="did_not_work")
        assert out["success"] is True, out
        assert out["dismissedCount"] == 1
        assert len(out["dismissalUuids"]) == 1

        # Gone from the list and the badge — for staff and for the brand.
        assert await self._missing(self.staff) == []
        assert await self._missing(self.brand_user) == []
        assert await self._badge(self.staff) == 0
        assert await self._badge(self.brand_user) == 0

        # On the Dismissed tab with who, when and why.
        rows = await self._dismissed()
        assert len(rows) == 1
        row = rows[0]
        assert row["dismissalUuid"] == out["dismissalUuids"][0]
        assert row["eventName"] == "No-show shift"
        assert row["ambassadorName"] == "Rue Vance"
        assert row["reason"] == "did_not_work"
        assert row["reasonLabel"] == "Did not work the shift"
        assert row["dismissedByName"] == "Sam Staff"
        assert row["dismissedAt"]
        assert row["recapFiled"] is False

        # Dismissing again changes nothing and hands back the same record.
        again = await self._dismiss(rosters=[roster], reason="other", note="x")
        assert again["dismissalUuids"] == out["dismissalUuids"]

        restored = await self._restore(out["dismissalUuids"])
        assert restored["success"] is True and restored["restoredCount"] == 1
        assert [r["eventName"] for r in await self._missing(self.staff)] == [
            "No-show shift"
        ]
        assert await self._badge(self.staff) == 1
        assert await self._badge(self.brand_user) == 1
        assert await self._dismissed() == []

        # Restoring keeps the record; it is only taken out of force.
        def kept():
            d = RecapDismissal.objects.get(uuid=out["dismissalUuids"][0])
            return d.restored_at is not None and d.restored_by_id == self.staff.id

        assert await sync_to_async(kept)()
        # A second restore is a no-op, and the row can be dismissed afresh.
        assert (await self._restore(out["dismissalUuids"]))["restoredCount"] == 0
        fresh = await self._dismiss(rosters=[roster], reason="not_needed")
        assert fresh["dismissalUuids"] != out["dismissalUuids"]
        assert await self._missing(self.staff) == []

    @pytest.mark.asyncio
    async def test_one_ambassador_of_two_keeps_the_event_for_the_other(self):
        def seed():
            event = self._past_event(name="Two-person shift")
            return event, self._book(event), self._book(event, self.ba2)

        event, first, second = await sync_to_async(seed)()
        await self._dismiss(rosters=[first], reason="filed_elsewhere")

        rows = await self._missing(self.staff)
        assert len(rows) == 1
        assert [a["name"] for a in rows[0]["assignedAmbassadors"]] == ["Kit Marlow"]
        assert await self._badge(self.staff) == 1

        await self._dismiss(rosters=[second], reason="filed_elsewhere")
        assert await self._missing(self.staff) == []
        assert await self._badge(self.staff) == 0

        # Someone new on the shift owes a recap: the event is due again,
        # and only they are listed.
        def add_third():
            _user, third = self._ba("ba-three", "Ines", "Okafor")
            self._book(event, third)

        await sync_to_async(add_third)()
        rows = await self._missing(self.staff)
        assert [a["name"] for a in rows[0]["assignedAmbassadors"]] == ["Ines Okafor"]

    @pytest.mark.asyncio
    async def test_a_shift_with_nobody_assigned_is_dismissed_as_an_event(self):
        event = await sync_to_async(self._past_event)(name="Test shift")
        assert len(await self._missing(self.staff)) == 1

        out = await self._dismiss(events=[event], reason="duplicate_or_test")
        assert out["success"] is True
        assert await self._missing(self.staff) == []
        assert await self._badge(self.staff) == 0
        rows = await self._dismissed()
        assert rows[0]["ambassadorName"] is None
        assert rows[0]["reasonLabel"] == "Duplicate or test shift"

        # Dismissed as a whole: assigning someone later does not revive it.
        await sync_to_async(self._book)(event)
        assert await self._missing(self.staff) == []

    @pytest.mark.asyncio
    async def test_bulk_dismiss_takes_rows_and_events_together(self):
        def seed():
            a = self._past_event(name="A", hours_ago=2)
            b = self._past_event(name="B", hours_ago=5)
            c = self._past_event(name="C", hours_ago=7)
            return [self._book(a), self._book(a, self.ba2), self._book(b)], c

        rosters, empty = await sync_to_async(seed)()
        assert await self._badge(self.staff) == 3
        out = await self._dismiss(
            rosters=rosters, events=[empty], reason="not_needed", note="Client call"
        )
        assert out["dismissedCount"] == 4
        assert await self._missing(self.staff) == []
        assert await self._badge(self.staff) == 0
        assert {r["note"] for r in await self._dismissed()} == {"Client call"}

        # Undo restores the lot in one call.
        assert (await self._restore(out["dismissalUuids"]))["restoredCount"] == 4
        assert await self._badge(self.staff) == 3

    @pytest.mark.asyncio
    async def test_reason_is_required_and_other_needs_a_note(self):
        _event, roster = await sync_to_async(self._seed_one)()

        bad = await self._dismiss(rosters=[roster], reason="because")
        assert bad["success"] is False and "reason" in bad["message"].lower()
        other = await self._dismiss(rosters=[roster], reason="other", note="  ")
        assert other["success"] is False and "note" in other["message"].lower()
        nothing = await self._dismiss(reason="did_not_work")
        assert nothing["success"] is False
        unknown = await self._gql(
            fe.DISMISS_M,
            {"input": {"ambassadorEventUuids": ["not-a-uuid"], "eventUuids": [],
                       "reason": "did_not_work", "note": None}},
            self.staff,
        )
        assert unknown.data["dismissMissingRecaps"]["success"] is False
        assert await sync_to_async(RecapDismissal.objects.count)() == 0

        ok = await self._dismiss(rosters=[roster], reason="other", note="Rained out")
        assert ok["success"] is True
        assert (await self._dismissed())[0]["note"] == "Rained out"

    # -- who may dismiss ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_brand_users_and_ambassadors_cannot_dismiss_restore_or_look(self):
        _event, roster = await sync_to_async(self._seed_one)()
        made = await self._dismiss(rosters=[roster], reason="did_not_work")
        await self._restore(made["dismissalUuids"])
        _event2, roster2 = await sync_to_async(self._seed_one)(name="Second")
        live = await self._dismiss(rosters=[roster2], reason="did_not_work")

        for outsider in (self.brand_user, self.ba_user):
            refused = await self._dismiss(
                rosters=[roster], reason="did_not_work", user=outsider
            )
            assert refused["success"] is False
            assert "staff" in refused["message"].lower()
            assert refused["dismissalUuids"] == []

            unrestored = await self._restore(live["dismissalUuids"], user=outsider)
            assert unrestored["success"] is False
            assert unrestored["restoredCount"] == 0

            looked = await self._gql(
                fe.DISMISSED_Q,
                {"tenantId": str(self.tenant.id), "lookbackDays": 30},
                outsider,
            )
            assert looked.errors is not None
            assert "staff" in str(looked.errors[0]).lower()

        def state():
            return (
                RecapDismissal.objects.filter(restored_at__isnull=True).count(),
                RecapDismissal.objects.count(),
            )

        # Exactly what staff did, nothing the outsiders tried.
        assert await sync_to_async(state)() == (1, 2)

    # -- reminders ---------------------------------------------------------

    def _run_command(self, name, *args):
        out = io.StringIO()
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as send:
            call_command(name, *args, stdout=out)
        return [c.args[0] for c in send.call_args_list], out.getvalue()

    def test_daily_reminders_and_timely_nudges_skip_a_dismissed_row(self):
        from recaps.dismissals import dismiss_missing_recaps, restore_missing_recaps

        event = self._past_event(hours_ago=3, name="Chased shift")
        mine = self._book(event)
        self._book(event, self.ba2)

        rows = dismiss_missing_recaps(
            user=self.staff, ambassador_event_uuids=[mine.uuid], reason="did_not_work"
        )

        # Daily sweep (the "Daily recap reminders" cron runs this command).
        pushed, log = self._run_command("send_recap_reminders")
        assert pushed == [self.ba2_user.id]
        assert "1 dismissed" in log

        # Timely per-shift nudge: the dismissed row is neither pushed nor
        # stamped, the other ambassador is.
        pushed, log = self._run_command("send_recap_nudges")
        assert pushed == [self.ba2_user.id]
        assert "1 dismissed" in log
        mine.refresh_from_db()
        assert mine.recap_nudge_sent_at is None

        # Restored: chased again like anyone else.
        restore_missing_recaps(user=self.staff, dismissal_uuids=[r.uuid for r in rows])
        pushed, _ = self._run_command("send_recap_reminders")
        assert sorted(pushed) == sorted([self.ba_user.id, self.ba2_user.id])
        pushed, _ = self._run_command("send_recap_nudges")
        assert pushed == [self.ba_user.id]

    def test_whole_event_dismissal_stops_every_reminder_for_it(self):
        from recaps.dismissals import dismiss_missing_recaps

        event = self._past_event(hours_ago=3)
        self._book(event)
        self._book(event, self.ba2)
        dismiss_missing_recaps(
            user=self.staff, event_uuids=[event.uuid], reason="duplicate_or_test"
        )
        assert self._run_command("send_recap_reminders")[0] == []
        assert self._run_command("send_recap_nudges")[0] == []
        assert is_recap_dismissed(ambassador_id=self.ba.id, event_id=event.id)
        assert is_recap_dismissed(ambassador_id=None, event_id=event.id)

    @pytest.mark.asyncio
    async def test_push_nudge_refuses_a_dismissed_row(self):
        _event, roster = await sync_to_async(self._seed_one)()
        made = await self._dismiss(rosters=[roster], reason="filed_elsewhere")

        with patch(
            "ambassadors.push.send_push_to_user", new=AsyncMock(return_value=2)
        ) as push:
            res = await self._gql(
                fe.NUDGE_M,
                {"input": {"ambassadorEventUuid": str(roster.uuid)}},
                self.staff,
            )
            assert res.errors is None, res.errors
            out = res.data["nudgeAmbassadorForRecap"]
            assert out["success"] is False
            assert "dismissed" in out["message"].lower()
            assert out["devicesNotified"] == 0
            push.assert_not_called()

            await self._restore(made["dismissalUuids"])
            res = await self._gql(
                fe.NUDGE_M,
                {"input": {"ambassadorEventUuid": str(roster.uuid)}},
                self.staff,
            )
            assert res.data["nudgeAmbassadorForRecap"]["success"] is True
            push.assert_called_once()

    def test_admin_digest_and_the_ambassadors_own_lists_leave_it_out(self):
        from ambassadors.checkin_history import shift_history_for
        from ambassadors.checkin_web import unfiled_shifts_for
        from digest.services import build_unfiled_recaps
        from recaps.dismissals import dismiss_missing_recaps, restore_missing_recaps

        event = self._past_event(hours_ago=6, name="Clocked shift")
        roster = self._book(event)
        for kind, hours in (("clock_in", 10), ("clock_out", 6)):
            source, _ = Source.objects.get_or_create(name=kind)
            Attendance.objects.create(
                ambassador=self.ba, event=event, source=source,
                clock_time=datetime.now(_tz.utc) - timedelta(hours=hours),
            )

        def history_flags():
            shifts = shift_history_for(ambassador=self.ba, tenant=self.tenant)["shifts"]
            return [(s["recapFiled"], s["recapDismissed"]) for s in shifts]

        assert build_unfiled_recaps(self.tenant).count == 1
        assert len(unfiled_shifts_for(ambassador=self.ba, tenant=self.tenant)) == 1
        assert history_flags() == [(False, False)]

        rows = dismiss_missing_recaps(
            user=self.staff, ambassador_event_uuids=[roster.uuid], reason="not_needed"
        )
        assert build_unfiled_recaps(self.tenant).count == 0
        assert unfiled_shifts_for(ambassador=self.ba, tenant=self.tenant) == []
        assert history_flags() == [(False, True)]

        restore_missing_recaps(user=self.staff, dismissal_uuids=[r.uuid for r in rows])
        assert build_unfiled_recaps(self.tenant).count == 1
        assert len(unfiled_shifts_for(ambassador=self.ba, tenant=self.tenant)) == 1
        assert history_flags() == [(False, False)]

    @pytest.mark.asyncio
    async def test_mobile_needs_a_recap_list_leaves_it_out(self):
        from config.schema_spark import schema_spark

        event, roster = await sync_to_async(self._seed_one)(name="Mobile shift")
        owing = "query { myPastShiftsOwingRecap(withinDays: 30) { eventUuid } }"

        async def owed():
            self.schema = schema_spark
            try:
                res = await self._execute_mutation(
                    owing, {}, "/api/v1/graphql/spark", user=self.ba_user
                )
            finally:
                from config.schema_client import schema_clients

                self.schema = schema_clients
            assert res.errors is None, res.errors
            return [r["eventUuid"] for r in res.data["myPastShiftsOwingRecap"]]

        assert await owed() == [str(event.uuid)]
        made = await self._dismiss(rosters=[roster], reason="did_not_work")
        assert await owed() == []
        await self._restore(made["dismissalUuids"])
        assert await owed() == [str(event.uuid)]

    # -- master tracker ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_tracker_row_says_dismissed_through_the_full_web_query(self):
        def seed():
            rt = em.RequestType.objects.create(
                name="Retail Sampling", tenant=self.tenant, created_by=self.sys
            )
            req = em.Request.objects.create(
                name="Dismissed stop", address="1 A St", request_type=rt,
                tenant=self.tenant, created_by=self.sys,
            )
            other = em.Request.objects.create(
                name="Owed stop", address="2 B St", request_type=rt,
                tenant=self.tenant, created_by=self.sys,
            )
            ev = self._past_event(name="Dismissed stop", request=req)
            self._past_event(name="Owed stop", request=other, hours_ago=5)
            return req, other, self._book(ev), self._book(ev, self.ba2)

        req, other, first, second = await sync_to_async(seed)()

        async def flags():
            res = await self._gql(
                fe.TRACKER_Q,
                {"first": 50, "filters": {"tenantId": str(self.tenant.id)}},
                self.staff,
            )
            assert res.errors is None, res.errors
            nodes = [e["node"] for e in res.data["requests"]["edges"]]
            return {n["name"]: (n["recapDismissed"], n["eventsCount"],
                                n["recapsFiledCount"]) for n in nodes}

        assert await flags() == {
            "Dismissed stop": (False, 1, 0), "Owed stop": (False, 1, 0)
        }
        made = await self._dismiss(rosters=[first], reason="did_not_work")
        # One of two ambassadors: the request still owes a recap.
        assert (await flags())["Dismissed stop"] == (False, 1, 0)
        more = await self._dismiss(rosters=[second], reason="did_not_work")
        assert await flags() == {
            "Dismissed stop": (True, 1, 0), "Owed stop": (False, 1, 0)
        }
        await self._restore(made["dismissalUuids"] + more["dismissalUuids"])
        assert (await flags())["Dismissed stop"] == (False, 1, 0)

    def test_tracker_flag_costs_one_batched_query_not_one_per_row(self):
        """Hot path: the scalar reads a prefetch, however many rows."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from recaps.dismissals import dismiss_missing_recaps

        rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )

        def add(n):
            for i in range(n):
                req = em.Request.objects.create(
                    name=f"Stop {em.Request.objects.count()}", address="1 A St",
                    request_type=rt, tenant=self.tenant, created_by=self.sys,
                )
                ev = self._past_event(name=req.name, request=req, hours_ago=2 + i)
                roster = self._book(ev)
                dismiss_missing_recaps(
                    user=self.staff, ambassador_event_uuids=[roster.uuid],
                    reason="did_not_work",
                )

        def dismissal_queries():
            with CaptureQueriesContext(connection) as ctx:
                res = async_to_sync(self._gql)(
                    fe.TRACKER_Q,
                    {"first": 50, "filters": {"tenantId": str(self.tenant.id)}},
                    self.staff,
                )
            assert res.errors is None, res.errors
            nodes = [e["node"] for e in res.data["requests"]["edges"]]
            assert nodes and all(n["recapDismissed"] for n in nodes)
            hits = [
                q for q in ctx.captured_queries if "recaps_recapdismissal" in q["sql"]
            ]
            return len(hits), len(nodes)

        add(2)
        assert dismissal_queries() == (1, 2)
        add(8)
        assert dismissal_queries() == (1, 10)

    # -- filing still works, and nothing else moves -----------------------

    def test_a_recap_can_still_be_filed_for_a_dismissed_row(self):
        from recaps.dismissals import dismiss_missing_recaps
        from recaps.filed import legacy_filed_q
        from recaps.models import FileType
        from recaps.mutations import RecapMutationService

        FileType.objects.create(name="Image", created_by=self.sys)
        event, roster = self._seed_one(name="Filed after all")
        dismiss_missing_recaps(
            user=self.staff, ambassador_event_uuids=[roster.uuid],
            reason="filed_elsewhere",
        )

        # The same service the createRecap mutation drives, as the BA.
        service = RecapMutationService.with_input(
            recap_inputs.CreateRecapInput(
                event_id=str(event.uuid),
                name="Late recap",
                files=[recap_inputs.RecapFileInput(file="recaps/photos/late.jpg")],
                products_sold=4,
                consumer_engagements=recap_inputs.ConsumerEngagementsInput(
                    total_consumer=20,
                    first_time_consumers=5,
                    brand_aware_consumers=5,
                    willing_to_purchase_consumers=5,
                ),
            )
        )
        service.user = self.ba_user
        recap = async_to_sync(service.create_recap)()
        assert recap.id is not None and recap.event_id == event.id
        assert Recap.objects.filter(event=event).filter(legacy_filed_q()).exists()

        # The dismissal is still on record and now simply does not matter.
        rows = async_to_sync(self._dismissed)()
        assert [r["recapFiled"] for r in rows] == [True]
        assert async_to_sync(self._missing)(self.staff) == []

    def test_a_dismissal_touches_nothing_but_its_own_record(self):
        from recaps.dismissals import dismiss_missing_recaps, restore_missing_recaps

        event, roster = self._seed_one()
        source, _ = Source.objects.get_or_create(name="clock_in")
        Attendance.objects.create(
            ambassador=self.ba, event=event, source=source,
            clock_time=datetime.now(_tz.utc) - timedelta(hours=7),
        )
        stub = Recap.objects.create(
            name="stub", event=event, ambassador=self.ba,
            created_by=self.sys, updated_by=self.sys,
        )

        def snapshot():
            return (
                list(AmbassadorEvent.objects.order_by("id").values()),
                list(Attendance.objects.order_by("id").values()),
                list(Recap.objects.order_by("id").values()),
                list(em.Event.objects.order_by("id").values()),
            )

        before = snapshot()
        rows = dismiss_missing_recaps(
            user=self.staff, ambassador_event_uuids=[roster.uuid], reason="did_not_work"
        )
        assert snapshot() == before
        restore_missing_recaps(user=self.staff, dismissal_uuids=[r.uuid for r in rows])
        assert snapshot() == before
        assert Recap.objects.filter(pk=stub.pk).exists()
        assert RecapDismissal.objects.count() == 1  # kept, marked restored
