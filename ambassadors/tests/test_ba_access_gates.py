"""
Access gates on BA writes and BA reads (follow-up to #1424).

- createAmbassadorInvitation / upsertAmbassadorProfile: Spark admins only.
- createAttendance / updateAttendance: admins write any punch; an
  ambassador writes only their own, on an event they're booked on; clients
  are refused.
- ambassadorProfile / ambassadorEvents: admins read everything; an
  ambassador reads only their own; clients and anonymous callers are
  refused.

Roles are read from the DB (``resolve_request_user_access``), so a caller
whose in-memory role is missing (the async-resolver hydration gap) is still
classified correctly. All cases run through the real clients schema (the
one the web app talks to).
"""

from datetime import datetime, timezone as dt_tz

import pytest
from asgiref.sync import sync_to_async

from ambassadors.models import (
    Ambassador,
    AmbassadorEvent,
    AmbassadorInvitation,
    Attendance,
    AttendanceStatus,
)
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models

WHEN = datetime(2026, 12, 5, 17, 0, tzinfo=dt_tz.utc)

INVITE = """
mutation Invite($input: CreateAmbassadorInvitationInput!) {
  createAmbassadorInvitation(input: $input) { success message }
}
"""

UPSERT = """
mutation Upsert($input: UpsertAmbassadorProfileInput!) {
  upsertAmbassadorProfile(input: $input) { success message }
}
"""

CREATE_ATT = """
mutation CreateAtt($input: CreateAttendanceInput!) {
  createAttendance(input: $input) {
    success message attendance { ambassadorId eventId }
  }
}
"""

UPDATE_ATT = """
mutation UpdateAtt($input: UpdateAttendanceInput!) {
  updateAttendance(input: $input) { success message }
}
"""

PROFILE = """
query Profile($uuid: ID) {
  ambassadorProfile(uuid: $uuid) { ambassador { uuid } }
}
"""

AMB_EVENTS = """
query AmbEvents {
  ambassadorEvents(first: 50) { edges { node { uuid ambassador { uuid } } } }
}
"""


@pytest.mark.django_db(transaction=True)
class TestBaAccessGates(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()

        self.tenant = self.create_tenant(name="BA Gate Brand")
        self.other_tenant = self.create_tenant(name="BA Gate Other")

        self.admin = self.create_user(
            username="bagate-admin", email="bagate-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="bagate-client", email="bagate-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.ba_user = self.create_user(
            username="bagate-ba", email="bagate-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant)
        self.ba = self.create_ambassador(self.ba_user, phone="555-0100")

        self.other_ba_user = self.create_user(
            username="bagate-ba2", email="bagate-ba2@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.other_ba_user, self.tenant)
        self.other_ba = self.create_ambassador(self.other_ba_user)

        self.event = event_models.Event.objects.create(
            name="Booked Shift", tenant=self.tenant, date=WHEN,
            created_by=self.sys,
        )
        self.unbooked_event = event_models.Event.objects.create(
            name="Not Booked", tenant=self.other_tenant, date=WHEN,
            created_by=self.sys,
        )
        self.ae = AmbassadorEvent.objects.create(
            ambassador=self.ba, event=self.event, tenant=self.tenant,
            is_approved=True, created_by=self.sys,
        )
        self.other_ae = AmbassadorEvent.objects.create(
            ambassador=self.other_ba, event=self.event, tenant=self.tenant,
            is_approved=True, created_by=self.sys,
        )
        for slug in ("approved", "pending"):
            AttendanceStatus.objects.get_or_create(
                slug=slug, tenant=None,
                defaults={"name": slug.title(), "created_by": self.sys},
            )
        self.other_punch = Attendance.objects.create(
            ambassador=self.other_ba, event=self.event, clock_time=WHEN,
        )
        self.own_punch = Attendance.objects.create(
            ambassador=self.ba, event=self.event, clock_time=WHEN,
        )

    def _who(self, who):
        return {
            "anon": None,
            "client": self.client_user,
            "ba": self.ba_user,
            "admin": self.admin,
        }[who]

    async def _run(self, query, variables, who):
        return await self._execute_mutation(
            query, variables, self.endpoint_path, user=self._who(who)
        )

    # ── createAmbassadorInvitation ───────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client", "ba"])
    async def test_invitation_refused_for_non_admins(self, who):
        result = await self._run(
            INVITE,
            {"input": {"email": "new-ba@x.test", "tenantId": str(self.other_tenant.id)}},
            who,
        )
        assert result.data is None or not (
            result.data["createAmbassadorInvitation"] or {}
        ).get("success")
        assert result.errors, "non-admin must be refused"
        made = await sync_to_async(
            AmbassadorInvitation.objects.filter(email="new-ba@x.test").exists
        )()
        assert not made

    @pytest.mark.asyncio
    async def test_invitation_refused_cannot_add_existing_ba_to_other_roster(self):
        """The existing-BA branch used to add the BA to any tenant named."""
        from tenants.models import TenantedUser

        result = await self._run(
            INVITE,
            {"input": {"email": self.ba_user.email, "tenantId": str(self.other_tenant.id)}},
            "client",
        )
        assert result.errors
        linked = await sync_to_async(
            TenantedUser.objects.filter(
                user=self.ba_user, tenant=self.other_tenant
            ).exists
        )()
        assert not linked

    @pytest.mark.asyncio
    async def test_admin_can_invite(self):
        result = await self._run(
            INVITE,
            {"input": {"email": "new-ba@x.test", "tenantId": str(self.tenant.id)}},
            "admin",
        )
        assert result.errors is None, result.errors
        assert result.data["createAmbassadorInvitation"]["success"] is True

    # ── upsertAmbassadorProfile ──────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client", "ba"])
    async def test_upsert_refused_for_non_admins(self, who):
        result = await self._run(
            UPSERT,
            {"input": {"ambassadorUuid": str(self.other_ba.uuid), "phone": "999", "rating": 5, "isActive": False}},
            who,
        )
        assert result.errors
        await sync_to_async(self.other_ba.refresh_from_db)()
        assert self.other_ba.phone != "999"
        assert self.other_ba.is_active is True

    @pytest.mark.asyncio
    async def test_ba_cannot_upsert_even_own_admin_fields(self):
        result = await self._run(
            UPSERT,
            {"input": {"ambassadorUuid": str(self.ba.uuid), "rating": 5}},
            "ba",
        )
        assert result.errors
        await sync_to_async(self.ba.refresh_from_db)()
        assert self.ba.rating != 5

    @pytest.mark.asyncio
    async def test_admin_can_upsert(self):
        result = await self._run(
            UPSERT,
            {"input": {"ambassadorUuid": str(self.other_ba.uuid), "phone": "999"}},
            "admin",
        )
        assert result.errors is None, result.errors
        assert result.data["upsertAmbassadorProfile"]["success"] is True
        await sync_to_async(self.other_ba.refresh_from_db)()
        assert self.other_ba.phone == "999"

    # ── createAttendance / updateAttendance ──────────────────────────

    def _att_input(self, **extra):
        data = {"clockTime": "2026-12-05T17:05:00+00:00", "eventId": str(self.event.id)}
        data.update(extra)
        return {"input": data}

    async def _punch_count(self, ambassador):
        return await sync_to_async(
            Attendance.objects.filter(ambassador=ambassador).count
        )()

    @pytest.mark.asyncio
    async def test_anon_cannot_create_attendance(self):
        before = await sync_to_async(Attendance.objects.count)()
        result = await self._run(CREATE_ATT, self._att_input(), "anon")
        assert result.errors
        assert await sync_to_async(Attendance.objects.count)() == before

    @pytest.mark.asyncio
    async def test_client_cannot_create_attendance(self):
        before = await self._punch_count(self.ba)
        result = await self._run(
            CREATE_ATT, self._att_input(ambassadorId=str(self.ba.id)), "client"
        )
        assert result.errors is None, result.errors
        assert result.data["createAttendance"]["success"] is False
        assert await self._punch_count(self.ba) == before

    @pytest.mark.asyncio
    async def test_ba_cannot_punch_for_another_ba(self):
        before = await self._punch_count(self.other_ba)
        result = await self._run(
            CREATE_ATT, self._att_input(ambassadorId=str(self.other_ba.id)), "ba"
        )
        assert result.data["createAttendance"]["success"] is False
        assert await self._punch_count(self.other_ba) == before

    @pytest.mark.asyncio
    async def test_ba_cannot_punch_on_unbooked_event(self):
        before = await self._punch_count(self.ba)
        result = await self._run(
            CREATE_ATT,
            self._att_input(eventId=str(self.unbooked_event.id)),
            "ba",
        )
        assert result.data["createAttendance"]["success"] is False
        assert "not booked" in result.data["createAttendance"]["message"]
        assert await self._punch_count(self.ba) == before

    @pytest.mark.asyncio
    async def test_ba_can_punch_own_booked_shift_and_is_pending(self):
        before = await self._punch_count(self.ba)
        result = await self._run(CREATE_ATT, self._att_input(), "ba")
        assert result.errors is None, result.errors
        payload = result.data["createAttendance"]
        assert payload["success"] is True, payload
        assert str(payload["attendance"]["ambassadorId"]) == str(self.ba.id)
        assert await self._punch_count(self.ba) == before + 1
        latest = await sync_to_async(
            lambda: Attendance.objects.filter(ambassador=self.ba)
            .select_related("attendance_status").order_by("-id").first()
        )()
        assert latest.attendance_status.slug == "pending"

    @pytest.mark.asyncio
    async def test_ba_with_unhydrated_role_still_forced_pending(self):
        """The role comes from the DB, so a BA whose JWT user lost its role
        can't pick their own status (or slip past the ownership check)."""
        self.ba_user.role = None
        approved = await sync_to_async(
            AttendanceStatus.objects.get
        )(slug="approved", tenant=None)
        result = await self._run(
            CREATE_ATT,
            self._att_input(attendanceStatusId=str(approved.id)),
            "ba",
        )
        assert result.data["createAttendance"]["success"] is True
        latest = await sync_to_async(
            lambda: Attendance.objects.filter(ambassador=self.ba)
            .select_related("attendance_status").order_by("-id").first()
        )()
        assert latest.attendance_status.slug == "pending"

    @pytest.mark.asyncio
    async def test_ba_cannot_update_another_bas_punch(self):
        result = await self._run(
            UPDATE_ATT,
            {"input": {"id": str(self.other_punch.id), "clockTime": "2026-12-05T09:00:00+00:00"}},
            "ba",
        )
        assert result.data["updateAttendance"]["success"] is False
        assert result.data["updateAttendance"]["message"] == "Attendance not found."
        await sync_to_async(self.other_punch.refresh_from_db)()
        assert self.other_punch.clock_time == WHEN

    @pytest.mark.asyncio
    async def test_ba_cannot_move_own_punch_onto_unbooked_event(self):
        result = await self._run(
            UPDATE_ATT,
            {"input": {
                "id": str(self.own_punch.id),
                "clockTime": "2026-12-05T18:00:00+00:00",
                "eventId": str(self.unbooked_event.id),
            }},
            "ba",
        )
        assert result.data["updateAttendance"]["success"] is False
        await sync_to_async(self.own_punch.refresh_from_db)()
        assert self.own_punch.event_id == self.event.id

    @pytest.mark.asyncio
    async def test_ba_can_update_own_punch(self):
        result = await self._run(
            UPDATE_ATT,
            {"input": {"id": str(self.own_punch.id), "clockTime": "2026-12-05T18:00:00+00:00"}},
            "ba",
        )
        assert result.data["updateAttendance"]["success"] is True, result.data
        await sync_to_async(self.own_punch.refresh_from_db)()
        assert self.own_punch.clock_time.hour == 18

    @pytest.mark.asyncio
    async def test_client_cannot_update_punch(self):
        result = await self._run(
            UPDATE_ATT,
            {"input": {"id": str(self.own_punch.id), "clockTime": "2026-12-05T09:00:00+00:00"}},
            "client",
        )
        assert result.data["updateAttendance"]["success"] is False
        await sync_to_async(self.own_punch.refresh_from_db)()
        assert self.own_punch.clock_time == WHEN

    @pytest.mark.asyncio
    async def test_admin_can_update_any_punch(self):
        result = await self._run(
            UPDATE_ATT,
            {"input": {"id": str(self.other_punch.id), "clockTime": "2026-12-05T18:00:00+00:00"}},
            "admin",
        )
        assert result.data["updateAttendance"]["success"] is True, result.data

    # ── ambassadorProfile ────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client"])
    async def test_profile_refused_for_anon_and_clients(self, who):
        result = await self._run(PROFILE, {"uuid": str(self.ba.uuid)}, who)
        assert result.errors
        assert (result.data or {}).get("ambassadorProfile") is None

    @pytest.mark.asyncio
    async def test_ba_reads_own_profile_not_others(self):
        own = await self._run(PROFILE, {"uuid": str(self.ba.uuid)}, "ba")
        assert own.errors is None, own.errors
        assert own.data["ambassadorProfile"]["ambassador"]["uuid"] == str(self.ba.uuid)

        other = await self._run(PROFILE, {"uuid": str(self.other_ba.uuid)}, "ba")
        assert other.errors is None
        assert other.data["ambassadorProfile"] is None

    @pytest.mark.asyncio
    async def test_admin_reads_any_profile(self):
        result = await self._run(PROFILE, {"uuid": str(self.other_ba.uuid)}, "admin")
        assert result.errors is None, result.errors
        assert result.data["ambassadorProfile"]["ambassador"]["uuid"] == str(self.other_ba.uuid)

    # ── ambassadorEvents ─────────────────────────────────────────────

    def _amb_uuids(self, result):
        return {
            e["node"]["ambassador"]["uuid"]
            for e in result.data["ambassadorEvents"]["edges"]
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["anon", "client"])
    async def test_amb_events_refused_for_anon_and_clients(self, who):
        result = await self._run(AMB_EVENTS, {}, who)
        assert result.errors
        assert (result.data or {}).get("ambassadorEvents") is None

    @pytest.mark.asyncio
    async def test_ba_sees_only_own_bookings(self):
        result = await self._run(AMB_EVENTS, {}, "ba")
        assert result.errors is None, result.errors
        assert self._amb_uuids(result) == {str(self.ba.uuid)}

    @pytest.mark.asyncio
    async def test_ba_with_unhydrated_role_still_sees_only_own(self):
        """Old code read user.role synchronously; an empty role fell through
        to 'everyone's bookings'."""
        self.ba_user.role = None
        result = await self._run(AMB_EVENTS, {}, "ba")
        assert result.errors is None, result.errors
        assert self._amb_uuids(result) == {str(self.ba.uuid)}

    @pytest.mark.asyncio
    async def test_admin_sees_all_bookings(self):
        result = await self._run(AMB_EVENTS, {}, "admin")
        assert result.errors is None, result.errors
        assert {str(self.ba.uuid), str(self.other_ba.uuid)} <= self._amb_uuids(result)
