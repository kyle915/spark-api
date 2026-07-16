"""
Tests for the walk-up self-serve clock-in flow (ambassadors/walkup.py).

Exercises the REAL GraphQL surface end-to-end across both schemas:
  admin generates a code (clients schema) → BA resolves + starts a walk-up
  (mobile schema) → admin sees it pending + confirms it (clients schema).
"""
import uuid

import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async

from ambassadors.models import Ambassador, AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from config.schema_mobile import schema_mobile


@pytest.mark.django_db(transaction=True)
class TestWalkupFlow(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Walk-up Test Tenant")
        uid = str(uuid.uuid4())[:8]

        # Admin/client user for the tenant (drives generate/confirm/list).
        self.client_user = self.create_user(
            username=f"walkup_client_{uid}@test.com",
            email=f"walkup_client_{uid}@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        # A brand-new (inactive) BA — the "pending review" path.
        self.new_user = self.create_user(
            username=f"walkup_new_{uid}@test.com",
            email=f"walkup_new_{uid}@test.com",
            role=self.roles["ambassador"],
        )
        self.new_amb = self.create_ambassador(
            user=self.new_user, is_active=False, created_by=self.system_user
        )

        # An already-active BA — the "auto-book" path.
        self.active_user = self.create_user(
            username=f"walkup_active_{uid}@test.com",
            email=f"walkup_active_{uid}@test.com",
            role=self.roles["ambassador"],
        )
        self.active_amb = self.create_ambassador(
            user=self.active_user, is_active=True, created_by=self.system_user
        )

        self.event = self.create_event(
            name="Walk-up Event",
            tenant=self.tenant,
            address="123 Test St",
        )

        self.endpoint_clients = "/api/v1/graphql/clients"
        self.endpoint_mobile = "/api/v1/graphql/mobile"

    async def _generate_code(self) -> str:
        self.schema = schema_clients
        mutation = """
            mutation Gen($input: GenerateWalkupCodeInput!) {
                generateWalkupCode(input: $input) {
                    success message code expiresAt
                }
            }
        """
        res = await self._execute_mutation_authenticated(
            mutation,
            {"input": {"eventUuid": str(self.event.uuid)}},
            self.client_user,
            self.endpoint_clients,
        )
        assert res.errors is None, res.errors
        payload = res.data["generateWalkupCode"]
        assert payload["success"] is True
        assert payload["code"]
        return payload["code"]

    @pytest.mark.asyncio
    async def test_generate_resolve_start_pending_then_confirm(self):
        code = await self._generate_code()

        # BA resolves the code (mobile schema).
        self.schema = schema_mobile
        resolve_q = """
            query Resolve($code: String!) {
                resolveWalkupCode(code: $code) {
                    found message event { eventUuid eventName brandName }
                }
            }
        """
        res = await self._execute_query_authenticated(
            resolve_q, {"code": code.lower()}, self.new_user, self.endpoint_mobile
        )
        assert res.errors is None, res.errors
        r = res.data["resolveWalkupCode"]
        assert r["found"] is True
        assert r["event"]["eventUuid"] == str(self.event.uuid)
        assert r["event"]["brandName"] == self.tenant.name

        # New BA starts the walk-up → pending review.
        start_m = """
            mutation Start($input: StartWalkupShiftInput!) {
                startWalkupShift(input: $input) {
                    success message ambassadorEventUuid eventUuid pendingReview
                }
            }
        """
        res = await self._execute_mutation_authenticated(
            start_m,
            {"input": {"code": code, "latitude": 40.0, "longitude": -80.0}},
            self.new_user,
            self.endpoint_mobile,
        )
        assert res.errors is None, res.errors
        s = res.data["startWalkupShift"]
        assert s["success"] is True
        assert s["pendingReview"] is True
        ae_uuid = s["ambassadorEventUuid"]
        assert ae_uuid

        # DB: walk-up row created, pending.
        ae = await sync_to_async(
            lambda: AmbassadorEvent.objects.get(uuid=ae_uuid)
        )()
        assert ae.source == AmbassadorEvent.SOURCE_WALKUP
        assert ae.is_approved is False

        # Admin sees it in the pending queue (clients schema).
        self.schema = schema_clients
        list_q = """
            query List($status: String) {
                walkupShifts(status: $status) {
                    ambassadorEventUuid isApproved isNewAccount eventName
                }
            }
        """
        res = await self._execute_query_authenticated(
            list_q, {"status": "pending"}, self.client_user, self.endpoint_clients
        )
        assert res.errors is None, res.errors
        rows = res.data["walkupShifts"]
        assert any(row["ambassadorEventUuid"] == ae_uuid for row in rows)
        row = next(r for r in rows if r["ambassadorEventUuid"] == ae_uuid)
        assert row["isNewAccount"] is True
        assert row["isApproved"] is False

        # Admin confirms → approved + BA activated.
        confirm_m = """
            mutation Confirm($input: ConfirmWalkupShiftInput!) {
                confirmWalkupShift(input: $input) { success message }
            }
        """
        res = await self._execute_mutation_authenticated(
            confirm_m,
            {"input": {"ambassadorEventUuid": ae_uuid}},
            self.client_user,
            self.endpoint_clients,
        )
        assert res.errors is None, res.errors
        assert res.data["confirmWalkupShift"]["success"] is True

        ae = await sync_to_async(
            lambda: AmbassadorEvent.objects.get(uuid=ae_uuid)
        )()
        assert ae.is_approved is True
        amb = await sync_to_async(lambda: Ambassador.objects.get(id=self.new_amb.id))()
        assert amb.is_active is True

    @pytest.mark.asyncio
    async def test_active_ba_auto_books(self):
        code = await self._generate_code()
        self.schema = schema_mobile
        start_m = """
            mutation Start($input: StartWalkupShiftInput!) {
                startWalkupShift(input: $input) {
                    success pendingReview ambassadorEventUuid
                }
            }
        """
        res = await self._execute_mutation_authenticated(
            start_m,
            {"input": {"code": code}},
            self.active_user,
            self.endpoint_mobile,
        )
        assert res.errors is None, res.errors
        s = res.data["startWalkupShift"]
        assert s["success"] is True
        assert s["pendingReview"] is False  # active BA auto-books
        ae = await sync_to_async(
            lambda: AmbassadorEvent.objects.get(uuid=s["ambassadorEventUuid"])
        )()
        assert ae.is_approved is True
        assert ae.source == AmbassadorEvent.SOURCE_WALKUP

    @pytest.mark.asyncio
    async def test_bad_code_fails_cleanly(self):
        self.schema = schema_mobile
        start_m = """
            mutation Start($input: StartWalkupShiftInput!) {
                startWalkupShift(input: $input) { success message }
            }
        """
        res = await self._execute_mutation_authenticated(
            start_m,
            {"input": {"code": "ZZZZZZ"}},
            self.new_user,
            self.endpoint_mobile,
        )
        assert res.errors is None, res.errors
        assert res.data["startWalkupShift"]["success"] is False
