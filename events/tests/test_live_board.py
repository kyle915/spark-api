"""
Coverage for events/live_board.py — the admin "on the clock" board.

Pins the four things that were wrong when Kyle opened the board on the Total
Wireless tab (2026-08-01) and saw Feel Free's shifts and none of his own
clocked-in BAs:

1. Unapproved WALK-UP bookings with a real clock punch must appear. They come
   in through the standing check-in link at ``is_approved=False`` and only get
   approved when their recap is signed off — the old ``if not ae.is_approved:
   continue`` hid exactly the people the board exists to show.
2. Unapproved bookings with NO punch stay hidden (applicants, pending invites).
3. ``tenantId`` actually scopes. For an Ignite admin an unscoped call means
   "every client", which is how Feel Free leaked onto the TW tab.
4. "Today" is the OPS calendar day, not the UTC one. settings.TIME_ZONE is UTC,
   so ``timezone.localdate()`` rolls to tomorrow at 5pm Pacific.

Plus the GPS payload, which is the point of the board for a walk-up crew.

The resolver is async, so every bit of ORM seeding inside a test goes through
``sync_to_async`` — calling it directly raises SynchronousOnlyOperation.
"""

from datetime import datetime, time as _time, timedelta, timezone as _tz
from zoneinfo import ZoneInfo

import pytest
from asgiref.sync import sync_to_async

from ambassadors.models import AmbassadorEvent, Attendance, LocationPing, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.live_board import OPS_TZ, _ops_today


def _ops_noon() -> datetime:
    """Noon UTC on the ops day — the same convention
    find_or_create_walkin_event uses, and stable no matter what wall-clock
    hour the suite happens to run at."""
    return datetime.combine(_ops_today(), _time(12, 0), tzinfo=_tz.utc)


BOARD_QUERY = """
    query LiveShiftBoard($date: String, $tenantId: ID) {
      liveShiftBoard(date: $date, tenantId: $tenantId) {
        eventUuid
        eventName
        brandName
        date
        startTime
        endTime
        address
        store
        requestUuid
        onClock
        noShows
        pendingApproval
        assigned {
          ambassadorUuid
          name
          status
          clockInAt
          clockOutAt
          workedHours
          lat
          lng
          locationAt
          locationSource
          accuracyMeters
          pendingApproval
        }
      }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestLiveShiftBoard(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.tenant = self.create_tenant(name="Total Wireless")
        self.other_tenant = self.create_tenant(name="Feel Free")

        self.admin = self.create_user(
            username="admin-live@test.com",
            email="admin-live@test.com",
            role=self.roles["spark_admin"],
        )

        self.noon = _ops_noon()
        self.event = self.create_event(
            name="Avenue P, Galveston",
            tenant=self.tenant,
            date=self.noon,
            start_time=None,
            end_time=None,
        )

    # ---------- sync seeding helpers (always via self._seed) ----------

    def _ba(self, slug: str):
        user = self.create_user(
            username=f"{slug}@test.com",
            email=f"{slug}@test.com",
            role=self.roles["ambassador"],
        )
        user.first_name = slug.title()
        user.last_name = "Tester"
        user.save(update_fields=["first_name", "last_name"])
        return self.create_ambassador(user)

    def _book(self, ambassador, *, approved: bool, event=None, tenant=None):
        return AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=event or self.event,
            tenant=tenant or self.tenant,
            is_approved=approved,
            created_by=self.admin,
        )

    def _punch(self, ambassador, kind: str, when, *, event=None, coords=None):
        source, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            clock_time=when,
            coordinates=coords,
            ambassador=ambassador,
            job=None,
            event=event or self.event,
            source=source,
        )

    async def _seed(self, fn):
        """Run a block of ORM writes off the event loop."""
        return await sync_to_async(fn)()

    async def _board(self, tenant_id=None):
        variables = {"tenantId": str(tenant_id) if tenant_id else None}
        result = await self._execute_query_authenticated(
            BOARD_QUERY, variables, self.admin, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data["liveShiftBoard"]

    @staticmethod
    def _names(shifts):
        return {ba["name"] for s in shifts for ba in s["assigned"]}

    # ---------- the walk-up visibility bug ----------

    @pytest.mark.asyncio
    async def test_unapproved_walkup_with_a_punch_is_on_the_board(self):
        """The whole point: a BA who clocked in through the standing link is
        working right now, even though their booking stays unapproved until
        the recap is signed off."""

        def seed():
            ba = self._ba("collin")
            self._book(ba, approved=False)
            self._punch(ba, "clock_in", self.noon + timedelta(hours=3))

        await self._seed(seed)
        shifts = await self._board(self.tenant.id)

        assert len(shifts) == 1
        row = shifts[0]["assigned"][0]
        assert row["name"] == "Collin Tester"
        assert row["status"] == "clocked_in"
        assert row["pendingApproval"] is True
        assert row["clockInAt"] is not None
        assert shifts[0]["onClock"] == 1
        assert shifts[0]["pendingApproval"] == 1

    @pytest.mark.asyncio
    async def test_unapproved_without_a_punch_stays_hidden(self):
        """Applicants and pending invites are not 'on the clock' — clock
        activity is the tiebreaker, not the approval flag alone."""

        def seed():
            self._book(self._ba("applicant"), approved=False)

        await self._seed(seed)

        assert await self._board(self.tenant.id) == []

    @pytest.mark.asyncio
    async def test_approved_ba_is_not_flagged_pending(self):
        def seed():
            ba = self._ba("rostered")
            self._book(ba, approved=True)
            self._punch(ba, "clock_in", self.noon + timedelta(hours=1))

        await self._seed(seed)
        shifts = await self._board(self.tenant.id)

        assert shifts[0]["assigned"][0]["pendingApproval"] is False
        assert shifts[0]["pendingApproval"] == 0

    @pytest.mark.asyncio
    async def test_clock_pair_reports_times_and_hours(self):
        def seed():
            ba = self._ba("wrapped")
            self._book(ba, approved=False)
            self._punch(ba, "clock_in", self.noon)
            self._punch(ba, "clock_out", self.noon + timedelta(hours=3, minutes=30))

        await self._seed(seed)
        row = (await self._board(self.tenant.id))[0]["assigned"][0]

        assert row["status"] == "clocked_out"
        assert row["clockInAt"] is not None
        assert row["clockOutAt"] is not None
        assert row["workedHours"] == 3.5

    # ---------- tenant scoping ----------

    @pytest.mark.asyncio
    async def test_tenant_id_scopes_out_other_clients(self):
        """An Ignite admin on the TW tab must not see Feel Free's shifts."""

        def seed():
            mine = self._ba("twba")
            self._book(mine, approved=False)
            self._punch(mine, "clock_in", self.noon)

            theirs_event = self.create_event(
                name="Coconut Grove",
                tenant=self.other_tenant,
                date=self.noon,
                start_time=None,
                end_time=None,
            )
            theirs = self._ba("ffba")
            self._book(
                theirs, approved=True, event=theirs_event, tenant=self.other_tenant
            )
            self._punch(theirs, "clock_in", self.noon, event=theirs_event)

        await self._seed(seed)

        assert self._names(await self._board(self.tenant.id)) == {"Twba Tester"}
        # Unscoped, the same admin sees both — the behaviour the page was
        # accidentally relying on.
        assert "Ffba Tester" in self._names(await self._board(None))

    # ---------- GPS ----------

    @pytest.mark.asyncio
    async def test_latest_location_ping_wins(self):
        def seed():
            ba = self._ba("pinged")
            self._book(ba, approved=False)
            self._punch(ba, "clock_in", self.noon, coords=[29.28, -94.83])
            LocationPing.objects.create(
                ambassador=ba,
                event=self.event,
                lat=29.3010,
                lng=-94.7977,
                accuracy_meters=12.5,
                recorded_at=self.noon + timedelta(hours=2),
                source="foreground",
            )

        await self._seed(seed)
        row = (await self._board(self.tenant.id))[0]["assigned"][0]

        assert row["lat"] == pytest.approx(29.3010)
        assert row["lng"] == pytest.approx(-94.7977)
        assert row["locationSource"] == "foreground"
        assert row["accuracyMeters"] == pytest.approx(12.5)
        assert row["locationAt"] is not None

    @pytest.mark.asyncio
    async def test_falls_back_to_clock_punch_coordinates(self):
        """Browser BAs grant location once at clock-in and no ping loop is
        running — they should still get a pin."""

        def seed():
            ba = self._ba("punchgps")
            self._book(ba, approved=False)
            self._punch(ba, "clock_in", self.noon, coords=[29.28, -94.83])

        await self._seed(seed)
        row = (await self._board(self.tenant.id))[0]["assigned"][0]

        assert row["lat"] == pytest.approx(29.28)
        assert row["lng"] == pytest.approx(-94.83)
        assert row["locationSource"] == "clock_in"

    @pytest.mark.asyncio
    async def test_no_fix_is_null_not_zero_zero(self):
        """[0, 0] must read as 'no GPS', never as a pin off the coast of
        Ghana."""

        def seed():
            ba = self._ba("nogps")
            self._book(ba, approved=False)
            self._punch(ba, "clock_in", self.noon, coords=[0.0, 0.0])

        await self._seed(seed)
        row = (await self._board(self.tenant.id))[0]["assigned"][0]

        assert row["lat"] is None
        assert row["lng"] is None

    # ---------- the day boundary ----------

    def test_ops_today_is_pacific_not_utc(self):
        """Regression pin for the 5pm rollover. Between 00:00 and 07:00 UTC
        the two genuinely differ, and the ops day is the correct one."""
        from django.utils import timezone

        now = timezone.now()
        assert _ops_today() == now.astimezone(ZoneInfo(OPS_TZ)).date()
        if now.hour < 7:
            assert _ops_today() != now.date(), "should trail the UTC date overnight"

    @pytest.mark.asyncio
    async def test_open_shift_survives_the_day_rollover(self):
        """A BA still punched in on yesterday's event stays on the board — the
        shift must not vanish out from under someone standing in the store."""
        from django.utils import timezone

        def seed():
            stale_event = self.create_event(
                name="Yesterday, still working",
                tenant=self.tenant,
                date=self.noon - timedelta(days=1),
                start_time=None,
                end_time=None,
            )
            ba = self._ba("overnight")
            self._book(ba, approved=False, event=stale_event)
            # Inside OPEN_SHIFT_LOOKBACK_HOURS, and never clocked out.
            self._punch(
                ba, "clock_in", timezone.now() - timedelta(hours=2), event=stale_event
            )

        await self._seed(seed)

        assert "Overnight Tester" in self._names(await self._board(self.tenant.id))

    @pytest.mark.asyncio
    async def test_closed_shift_from_another_day_does_not_linger(self):
        """The open-punch union is for people still working, not history."""

        def seed():
            old_event = self.create_event(
                name="Last week",
                tenant=self.tenant,
                date=self.noon - timedelta(days=7),
                start_time=None,
                end_time=None,
            )
            ba = self._ba("finished")
            self._book(ba, approved=True, event=old_event)
            self._punch(ba, "clock_in", self.noon - timedelta(days=7), event=old_event)
            self._punch(
                ba,
                "clock_out",
                self.noon - timedelta(days=7, hours=-4),
                event=old_event,
            )

        await self._seed(seed)

        assert "Finished Tester" not in self._names(await self._board(self.tenant.id))
