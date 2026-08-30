"""Admin manual clock-in / clock-out / edit punch mutations."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone as dj_tz

from ambassadors.models import Attendance, AmbassadorEvent, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events.models import RequestActivityLog


@pytest.mark.django_db(transaction=True)
class TestAdminAttendanceMutations(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Admin Clock {uid}")

        self.client_user = self.create_user(
            username=f"admin_clock_client_{uid}@test.com",
            email=f"admin_clock_client_{uid}@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        ba_user = self.create_user(
            username=f"admin_clock_ba_{uid}@test.com",
            email=f"admin_clock_ba_{uid}@test.com",
            role=self.roles["ambassador"],
            first_name="Jenn",
            last_name="Gilley",
        )
        self.ba = self.create_ambassador(ba_user, is_active=True)

        self.event = self.create_event(
            name="Clock Event",
            tenant=self.tenant,
            address="100 Main St",
        )
        # Parent request so KIND_ATTENDANCE_ADJUSTED audit rows have a home.
        location = self.create_location(
            name=f"Loc {uid}",
            code=f"L{uid[:4]}",
            zip_code="78701",
            tenant=self.tenant,
        )
        client = self.create_client(
            name=f"Client {uid}",
            email=f"client_{uid}@test.com",
            tenant=self.tenant,
        )
        distributor = self.create_distributor(
            name=f"Dist {uid}",
            email=f"dist_{uid}@test.com",
            location=location,
            tenant=self.tenant,
        )
        retailer = self.create_retailer(
            name=f"Retail {uid}",
            address="100 Main St",
            store_contact="x",
            location=location,
            tenant=self.tenant,
        )
        req_type = self.create_request_type(name=f"Type {uid}", tenant=self.tenant)
        self.request = self.create_request(
            name="Clock Req",
            date=dj_tz.now().date(),
            address="100 Main St",
            client=client,
            distributor=distributor,
            retailer=retailer,
            request_type=req_type,
            tenant=self.tenant,
        )
        self.event.request = self.request
        self.event.save(update_fields=["request"])
        AmbassadorEvent.objects.create(
            ambassador=self.ba,
            event=self.event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

        self.endpoint = "/api/v1/graphql/clients"
        self.schema = schema_clients

    async def _mutate(self, mutation, variables):
        return await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint
        )

    @pytest.mark.asyncio
    async def test_manual_clock_in_then_out_then_edit(self):
        clock_in_m = """
            mutation In($input: AdminClockPunchInput!) {
                adminManualClockIn(input: $input) {
                    success message clockInAt clockOutAt attendanceUuid
                }
            }
        """
        when_in = (dj_tz.now() - timedelta(hours=3)).isoformat()
        res = await self._mutate(
            clock_in_m,
            {
                "input": {
                    "eventUuid": str(self.event.uuid),
                    "ambassadorUuid": str(self.ba.uuid),
                    "clockTime": when_in,
                    "note": "check-in failed on phone",
                }
            },
        )
        assert res.errors is None, res.errors
        payload = res.data["adminManualClockIn"]
        assert payload["success"] is True
        assert payload["clockInAt"]
        assert payload["attendanceUuid"]
        in_uuid = payload["attendanceUuid"]

        def _load_att(u):
            return Attendance.objects.select_related("source").get(uuid=u)

        att = await sync_to_async(_load_att)(in_uuid)
        assert att.source.name == "clock_in"
        assert att.created_by_id == self.client_user.id

        # Second clock-in while open should fail.
        res = await self._mutate(
            clock_in_m,
            {
                "input": {
                    "eventUuid": str(self.event.uuid),
                    "ambassadorUuid": str(self.ba.uuid),
                }
            },
        )
        assert res.errors is None, res.errors
        assert res.data["adminManualClockIn"]["success"] is False

        clock_out_m = """
            mutation Out($input: AdminClockPunchInput!) {
                adminManualClockOut(input: $input) {
                    success message clockInAt clockOutAt attendanceUuid
                }
            }
        """
        when_out = (dj_tz.now() - timedelta(hours=1)).isoformat()
        res = await self._mutate(
            clock_out_m,
            {
                "input": {
                    "eventUuid": str(self.event.uuid),
                    "ambassadorUuid": str(self.ba.uuid),
                    "clockTime": when_out,
                }
            },
        )
        assert res.errors is None, res.errors
        out_payload = res.data["adminManualClockOut"]
        assert out_payload["success"] is True
        assert out_payload["clockOutAt"]

        # Edit clock-in via attendance uuid.
        edit_m = """
            mutation Edit($input: AdminEditPunchInput!) {
                adminEditPunch(input: $input) {
                    success message clockInAt clockOutAt
                }
            }
        """
        new_in = (dj_tz.now() - timedelta(hours=4)).isoformat()
        res = await self._mutate(
            edit_m,
            {
                "input": {
                    "attendanceUuid": in_uuid,
                    "clockTime": new_in,
                    "note": "corrected start",
                }
            },
        )
        assert res.errors is None, res.errors
        assert res.data["adminEditPunch"]["success"] is True

        att = await sync_to_async(_load_att)(in_uuid)
        assert abs((att.clock_time - dj_tz.now() + timedelta(hours=4)).total_seconds()) < 120

        # Edit clock-out via event + ambassador + kind.
        new_out = (dj_tz.now() - timedelta(minutes=30)).isoformat()
        res = await self._mutate(
            edit_m,
            {
                "input": {
                    "eventUuid": str(self.event.uuid),
                    "ambassadorUuid": str(self.ba.uuid),
                    "kind": "clock_out",
                    "clockTime": new_out,
                }
            },
        )
        assert res.errors is None, res.errors
        assert res.data["adminEditPunch"]["success"] is True

        logs = await sync_to_async(
            lambda: list(
                RequestActivityLog.objects.filter(
                    request=self.request,
                    kind=RequestActivityLog.KIND_ATTENDANCE_ADJUSTED,
                )
            )
        )()
        assert len(logs) >= 3

    @pytest.mark.asyncio
    async def test_clock_out_requires_open_punch(self):
        clock_out_m = """
            mutation Out($input: AdminClockPunchInput!) {
                adminManualClockOut(input: $input) {
                    success message
                }
            }
        """
        res = await self._mutate(
            clock_out_m,
            {
                "input": {
                    "eventUuid": str(self.event.uuid),
                    "ambassadorUuid": str(self.ba.uuid),
                }
            },
        )
        assert res.errors is None, res.errors
        assert res.data["adminManualClockOut"]["success"] is False
        assert "Not clocked in" in res.data["adminManualClockOut"]["message"]

    @pytest.mark.asyncio
    async def test_clock_in_requires_assignment(self):
        uid = str(uuid.uuid4())[:8]

        def _make_other():
            other_user = self.create_user(
                username=f"unassigned_{uid}@test.com",
                email=f"unassigned_{uid}@test.com",
                role=self.roles["ambassador"],
                first_name="No",
                last_name="Roster",
            )
            return self.create_ambassador(other_user, is_active=True)

        other = await sync_to_async(_make_other)()
        clock_in_m = """
            mutation In($input: AdminClockPunchInput!) {
                adminManualClockIn(input: $input) { success message }
            }
        """
        res = await self._mutate(
            clock_in_m,
            {
                "input": {
                    "eventUuid": str(self.event.uuid),
                    "ambassadorUuid": str(other.uuid),
                }
            },
        )
        assert res.errors is None, res.errors
        assert res.data["adminManualClockIn"]["success"] is False
        assert "not assigned" in res.data["adminManualClockIn"]["message"].lower()

    @pytest.mark.asyncio
    async def test_event_attendance_returns_punch_uuids(self):
        source, _ = await sync_to_async(Source.objects.get_or_create)(name="clock_in")
        att = await sync_to_async(Attendance.objects.create)(
            ambassador=self.ba,
            event=self.event,
            source=source,
            clock_time=dj_tz.now() - timedelta(hours=2),
            created_by=self.system_user,
        )
        q = """
            query Att($eventUuid: ID!) {
                eventAttendance(eventUuid: $eventUuid) {
                    ambassadorUuid
                    clockInAt
                    clockInAttendanceUuid
                    clockOutAttendanceUuid
                }
            }
        """
        res = await self._execute_query_authenticated(
            q,
            {"eventUuid": str(self.event.uuid)},
            self.client_user,
            self.endpoint,
        )
        assert res.errors is None, res.errors
        rows = res.data["eventAttendance"]
        assert len(rows) == 1
        assert rows[0]["clockInAttendanceUuid"] == str(att.uuid)
        assert rows[0]["clockOutAttendanceUuid"] is None
