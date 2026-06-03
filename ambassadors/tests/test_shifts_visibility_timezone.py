"""
Tests for task #269: a just-booked gig must reliably show on the BA mobile
Shifts screen — it must never fall into the gap between "Active today" and
"Upcoming" because of the UTC/local date boundary.

The bug: with settings.TIME_ZONE="UTC" + USE_TZ=True, my_active_shifts compared
the event's UTC date against a UTC `today`, and my_upcoming_shifts only listed
shifts with start_time >= now(). A shift booked for "today evening Pacific"
(e.g. 8 PM PDT) is stored as the NEXT calendar day in UTC, so its UTC date was
"tomorrow" → my_active_shifts missed it; and a shift that already started
earlier today (start_time < now) was excluded from my_upcoming_shifts too. A
freshly-booked today/near shift could therefore appear on NEITHER list.

The fix buckets each booking by its LOCAL date (the event's own timezone, with
a Pacific fallback) vs local "today" in the same zone:
    Active   = local date == local today  (incl. shifts started earlier today)
    Upcoming = local date in (today, today+14]

These tests assert the INVARIANT: no approved AmbassadorEvent in [today, +14d]
is invisible (missing from both lists), and the specific boundary/earlier-today
cases that used to vanish now show up.
"""

import pytest
from datetime import datetime, time, timedelta, timezone as _tz
from zoneinfo import ZoneInfo

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import TimeZone

User = get_user_model()

PACIFIC = ZoneInfo("America/Los_Angeles")

ACTIVE_QUERY = """
query { myActiveShifts { eventUuid isApproved } }
"""

UPCOMING_QUERY = """
query { myUpcomingShifts { eventUuid isApproved } }
"""


@pytest.mark.django_db(transaction=True)
class TestShiftsVisibilityTimezone(AmbassadorsGraphQLTestCase):
    """my_active_shifts / my_upcoming_shifts: no booked shift falls in the gap."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Shift TZ Tenant")
        self.ba_user = self.create_user(
            username="ba-shift-tz",
            email="ba-shift-tz@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.admin = self.create_user(
            username="admin-shift-tz",
            email="adm-shift-tz@test.com",
            role=self.roles["spark_admin"],
        )
        # A Pacific timezone row resolvable by utils.tz (code PDT/PST →
        # America/Los_Angeles), so the resolver buckets these events in
        # Pacific local time.
        self.pacific_tz = TimeZone.objects.create(
            name="PACIFIC", code="PDT", offset=-480,
            created_by=self.get_system_user(),
        )

    # ---- helpers -----------------------------------------------------------

    def _pacific_now(self):
        return datetime.now(_tz.utc).astimezone(PACIFIC)

    def _utc_for_pacific_local(self, local_date, hour):
        """UTC-aware datetime for `hour`:00 on `local_date` in Pacific."""
        naive_local = datetime.combine(local_date, time(hour=hour))
        return naive_local.replace(tzinfo=PACIFIC).astimezone(_tz.utc)

    def _book(self, *, start_utc, with_tz=True, name="Shift"):
        """Create an approved AmbassadorEvent for self.ambassador."""
        event = self.create_event(
            name=name,
            tenant=self.tenant,
            address="1 Demo St",
            start_time=start_utc,
            date=start_utc,
            timezone=self.pacific_tz if with_tz else None,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.admin,
        )
        return event

    async def _active_uuids(self):
        res = await self._execute_query_authenticated(
            ACTIVE_QUERY, {}, self.ba_user, self.endpoint_path
        )
        assert res.errors is None, f"active errored: {res.errors}"
        return {s["eventUuid"] for s in res.data["myActiveShifts"]}

    async def _upcoming_uuids(self):
        res = await self._execute_query_authenticated(
            UPCOMING_QUERY, {}, self.ba_user, self.endpoint_path
        )
        assert res.errors is None, f"upcoming errored: {res.errors}"
        return {s["eventUuid"] for s in res.data["myUpcomingShifts"]}

    # ---- tests -------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_today_evening_pacific_shift_is_visible_as_active(self):
        """The headline bug: a shift at 8 PM Pacific TODAY is stored as
        TOMORROW in UTC. It must still show as Active (local date == today),
        not vanish into the UTC/local gap."""
        pac_today = self._pacific_now().date()
        start_utc = self._utc_for_pacific_local(pac_today, 20)  # 8 PM PDT today
        # Sanity: this booking's UTC date really is tomorrow (the bug shape).
        assert start_utc.date() == pac_today + timedelta(days=1)

        event = await sync_to_async(self._book)(
            start_utc=start_utc, name="Tonight 8PM Pacific"
        )

        active = await self._active_uuids()
        upcoming = await self._upcoming_uuids()
        assert str(event.uuid) in active, (
            "today-evening Pacific shift missing from Active"
        )
        # Visible on exactly one list (not double-counted).
        assert str(event.uuid) not in upcoming

    @pytest.mark.asyncio
    async def test_shift_started_earlier_today_still_active(self):
        """A shift that already started earlier today (start_time < now) must
        remain Active so the mid-shift BA can still clock in / file."""
        pac_now = self._pacific_now()
        # 6 hours ago, but clamp to keep it on *today's* Pacific date so the
        # assertion is about "started earlier today", not "yesterday".
        earlier_hour = max(0, pac_now.hour - 6)
        start_utc = self._utc_for_pacific_local(pac_now.date(), earlier_hour)
        assert start_utc < datetime.now(_tz.utc)

        event = await sync_to_async(self._book)(
            start_utc=start_utc, name="Started this morning"
        )

        active = await self._active_uuids()
        assert str(event.uuid) in active, (
            "in-progress (started-earlier-today) shift missing from Active"
        )

    @pytest.mark.asyncio
    async def test_future_shift_is_upcoming_not_active(self):
        """A shift a few days out shows on Upcoming, not Active."""
        pac_today = self._pacific_now().date()
        start_utc = self._utc_for_pacific_local(pac_today + timedelta(days=3), 12)

        event = await sync_to_async(self._book)(
            start_utc=start_utc, name="In 3 days"
        )

        active = await self._active_uuids()
        upcoming = await self._upcoming_uuids()
        assert str(event.uuid) in upcoming
        assert str(event.uuid) not in active

    @pytest.mark.asyncio
    async def test_invariant_no_booked_shift_in_window_is_invisible(self):
        """Every approved booking with a local date in [today, today+14] must
        appear on exactly one of Active / Upcoming — none invisible, none
        double-counted. Covers a noon shift each day across the boundary."""
        pac_today = self._pacific_now().date()
        events = []
        for offset in range(0, 15):  # today .. +14 inclusive
            start_utc = self._utc_for_pacific_local(
                pac_today + timedelta(days=offset), 12
            )
            ev = await sync_to_async(self._book)(
                start_utc=start_utc, name=f"Day +{offset}"
            )
            events.append((offset, ev))

        active = await self._active_uuids()
        upcoming = await self._upcoming_uuids()

        for offset, ev in events:
            uid = str(ev.uuid)
            in_active = uid in active
            in_upcoming = uid in upcoming
            assert in_active or in_upcoming, (
                f"booking at +{offset}d is invisible on BOTH lists (the gap bug)"
            )
            assert not (in_active and in_upcoming), (
                f"booking at +{offset}d double-counted on both lists"
            )
            # Day 0 (today) belongs to Active; the rest to Upcoming.
            if offset == 0:
                assert in_active, "today's noon shift should be Active"
            else:
                assert in_upcoming, f"+{offset}d shift should be Upcoming"

    @pytest.mark.asyncio
    async def test_tzless_evening_shift_still_visible(self):
        """An event with NO timezone row (not yet geocoded/tz-stamped) booked
        for the evening must still surface — the Pacific fallback keeps it on
        a list rather than letting it slip through the UTC boundary."""
        pac_today = self._pacific_now().date()
        start_utc = self._utc_for_pacific_local(pac_today, 21)  # 9 PM, tz-less

        event = await sync_to_async(self._book)(
            start_utc=start_utc, with_tz=False, name="No-TZ tonight"
        )

        active = await self._active_uuids()
        upcoming = await self._upcoming_uuids()
        assert str(event.uuid) in (active | upcoming), (
            "tz-less evening shift fell through the gap"
        )

    @pytest.mark.asyncio
    async def test_empty_for_non_ambassador(self):
        """A signed-in user with no Ambassador profile gets empty lists."""
        res_a = await self._execute_query_authenticated(
            ACTIVE_QUERY, {}, self.admin, self.endpoint_path
        )
        res_u = await self._execute_query_authenticated(
            UPCOMING_QUERY, {}, self.admin, self.endpoint_path
        )
        assert res_a.errors is None and res_u.errors is None
        assert res_a.data["myActiveShifts"] == []
        assert res_u.data["myUpcomingShifts"] == []
