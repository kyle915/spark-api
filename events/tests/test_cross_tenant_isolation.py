"""Cross-tenant isolation through the real clients schema.

An outside review (logged in as a Spark admin) saw other brands on the
Staffing Board and a stray plan on /plans. Admins see every brand by design;
these tests pin down that a brand (client) user and a BA never can — by
default, or by passing another brand's tenantId / plan id.

Setup: brand A and brand B. ``client_a`` is a client member of A only;
``ba_a`` is an ambassador member of A only; ``admin`` is a spark-admin.
"""

from datetime import date, datetime, time, timezone as _tz

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as em

STAFFING = """
query SB($s: String!, $e: String!, $tid: ID) {
  staffingBoard(startDate: $s, endDate: $e, tenantId: $tid) {
    eventName brandName tenantId
  }
}
"""

PLANS = """
query Plans($tid: ID) { activationPlans(tenantId: $tid) { id name } }
"""

PLAN = """
query Plan($id: ID) { activationPlan(id: $id) { id name } }
"""

UPDATE_PLAN = """
mutation U($input: UpdateActivationPlanInput!) {
  updateActivationPlan(input: $input) { success message activationPlan { name } }
}
"""

DELETE_PLAN = """
mutation D($input: DeleteActivationPlanInput!) {
  deleteActivationPlan(input: $input) { success message }
}
"""

ATTACH = """
mutation A($input: AttachRequestsToPlanInput!) {
  attachRequestsToPlan(input: $input) { success message updatedCount }
}
"""

DETACH = """
mutation X($input: DetachRequestsFromPlanInput!) {
  detachRequestsFromPlan(input: $input) { success message updatedCount }
}
"""

CREATE_PLAN = """
mutation C($input: CreateActivationPlanInput!) {
  createActivationPlan(input: $input) { success message activationPlan { id } }
}
"""

REQUESTS = """
query R($f: RequestFiltersInput) {
  requests(first: 50, filters: $f) { edges { node { name } } }
}
"""

EVENTS = """
query E($f: EventFiltersInput) {
  events(first: 50, filters: $f) { edges { node { name } } }
}
"""

DAY = date(2026, 11, 20)


@pytest.mark.django_db(transaction=True)
class TestCrossTenantIsolation(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant_a = self.create_tenant(name="Brand A")
        self.tenant_b = self.create_tenant(name="Brand B")

        self.admin = self.create_user(
            username="iso-admin", email="iso-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_a = self.create_user(
            username="iso-client-a", email="iso-client-a@brand-a.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_a, self.tenant_a)
        self.ba_a = self.create_user(
            username="iso-ba-a", email="iso-ba-a@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_a, self.tenant_a)

        noon = datetime.combine(DAY, time(12, 0), tzinfo=_tz.utc)
        self.create_event(name="A shift", tenant=self.tenant_a, date=noon)
        self.create_event(name="B shift", tenant=self.tenant_b, date=noon)

        self.plan_a = em.ActivationPlan.objects.create(
            name="A plan", tenant=self.tenant_a
        )
        self.plan_b = em.ActivationPlan.objects.create(
            name="B plan", tenant=self.tenant_b
        )

        rt_b = em.RequestType.objects.create(
            name="Sampling", tenant=self.tenant_b, created_by=self.sys
        )
        st_b = self.create_request_status(
            name="Pending", tenant=self.tenant_b, slug="pending"
        )
        self.req_b = em.Request.objects.create(
            name="B request", date=noon, address="1 Main", coordinates=[0.0, 0.0],
            request_type=rt_b, status=st_b, tenant=self.tenant_b,
            created_by=self.sys, activation_plan=self.plan_b,
        )
        rt_a = em.RequestType.objects.create(
            name="Sampling", tenant=self.tenant_a, created_by=self.sys
        )
        st_a = self.create_request_status(
            name="Pending", tenant=self.tenant_a, slug="pending"
        )
        self.req_a = em.Request.objects.create(
            name="A request", date=noon, address="1 Main", coordinates=[0.0, 0.0],
            request_type=rt_a, status=st_a, tenant=self.tenant_a,
            created_by=self.sys,
        )

    async def _q(self, query, variables, user):
        return await self._execute_query_authenticated(
            query, variables, user, self.endpoint_path
        )

    # ---------------- Staffing Board ----------------

    async def _board(self, user, tid=None):
        d = DAY.isoformat()
        return await self._q(STAFFING, {"s": d, "e": d, "tid": tid}, user)

    @pytest.mark.asyncio
    async def test_staffing_board_client_sees_only_own_brand(self):
        for tid in (None, str(self.tenant_a.id), str(self.tenant_b.id)):
            res = await self._board(self.client_a, tid)
            assert res.errors is None, res.errors
            names = {r["eventName"] for r in res.data["staffingBoard"]}
            assert "B shift" not in names, (tid, names)
        res = await self._board(self.client_a)
        assert {r["eventName"] for r in res.data["staffingBoard"]} == {"A shift"}

    @pytest.mark.asyncio
    async def test_staffing_board_refuses_ambassadors(self):
        res = await self._board(self.ba_a, str(self.tenant_b.id))
        assert res.errors, res.data
        assert not (res.data or {}).get("staffingBoard")

    @pytest.mark.asyncio
    async def test_staffing_board_admin_sees_all_brands(self):
        res = await self._board(self.admin)
        assert res.errors is None, res.errors
        names = {r["eventName"] for r in res.data["staffingBoard"]}
        assert {"A shift", "B shift"} <= names
        res = await self._board(self.admin, str(self.tenant_b.id))
        assert {r["eventName"] for r in res.data["staffingBoard"]} == {"B shift"}

    # ---------------- Plans: reads ----------------

    @pytest.mark.asyncio
    async def test_plans_list_client_cannot_read_other_brand(self):
        res = await self._q(PLANS, {"tid": str(self.tenant_b.id)}, self.client_a)
        names = {p["name"] for p in ((res.data or {}).get("activationPlans") or [])}
        assert "B plan" not in names
        res = await self._q(PLANS, {"tid": str(self.tenant_a.id)}, self.client_a)
        assert res.errors is None, res.errors
        assert {p["name"] for p in res.data["activationPlans"]} == {"A plan"}

    @pytest.mark.asyncio
    async def test_plans_list_ba_cannot_read_other_brand(self):
        res = await self._q(PLANS, {"tid": str(self.tenant_b.id)}, self.ba_a)
        names = {p["name"] for p in ((res.data or {}).get("activationPlans") or [])}
        assert "B plan" not in names

    @pytest.mark.asyncio
    async def test_plan_detail_client_gets_null_for_other_brand(self):
        res = await self._q(PLAN, {"id": str(self.plan_b.id)}, self.client_a)
        assert res.errors is None, res.errors
        assert res.data["activationPlan"] is None

    @pytest.mark.asyncio
    async def test_plans_admin_reads_any_brand(self):
        res = await self._q(PLANS, {"tid": str(self.tenant_b.id)}, self.admin)
        assert res.errors is None, res.errors
        assert {p["name"] for p in res.data["activationPlans"]} == {"B plan"}
        res = await self._q(PLAN, {"id": str(self.plan_b.id)}, self.admin)
        assert res.data["activationPlan"]["name"] == "B plan"

    # ---------------- Plans: writes ----------------

    @pytest.mark.asyncio
    async def test_plan_create_client_cannot_target_other_brand(self):
        res = await self._q(
            CREATE_PLAN,
            {"input": {"tenantId": str(self.tenant_b.id), "name": "Injected"}},
            self.client_a,
        )
        assert res.data["createActivationPlan"]["success"] is False
        assert not await sync_to_async(
            em.ActivationPlan.objects.filter(name="Injected").exists
        )()

    @pytest.mark.asyncio
    async def test_plan_update_client_cannot_edit_other_brand(self):
        for user in (self.client_a, self.ba_a):
            res = await self._q(
                UPDATE_PLAN,
                {"input": {"id": str(self.plan_b.id), "name": "Hijacked"}},
                user,
            )
            assert res.data["updateActivationPlan"]["success"] is False
        plan = await sync_to_async(em.ActivationPlan.objects.get)(id=self.plan_b.id)
        assert plan.name == "B plan"

    @pytest.mark.asyncio
    async def test_plan_delete_client_cannot_delete_other_brand(self):
        for user in (self.client_a, self.ba_a):
            res = await self._q(
                DELETE_PLAN, {"input": {"id": str(self.plan_b.id)}}, user
            )
            assert res.data["deleteActivationPlan"]["success"] is False
        assert await sync_to_async(
            em.ActivationPlan.objects.filter(id=self.plan_b.id).exists
        )()

    @pytest.mark.asyncio
    async def test_plan_attach_client_cannot_touch_other_brand(self):
        res = await self._q(
            ATTACH,
            {"input": {
                "activationPlanId": str(self.plan_b.id),
                "requestIds": [str(self.req_b.id)],
            }},
            self.client_a,
        )
        assert res.data["attachRequestsToPlan"]["success"] is False

    @pytest.mark.asyncio
    async def test_plan_detach_client_cannot_touch_other_brand(self):
        for user in (self.client_a, self.ba_a):
            res = await self._q(
                DETACH, {"input": {"requestIds": [str(self.req_b.id)]}}, user
            )
            assert (res.data["detachRequestsFromPlan"]["updatedCount"] or 0) == 0
        req = await sync_to_async(em.Request.objects.get)(id=self.req_b.id)
        assert req.activation_plan_id == self.plan_b.id

    @pytest.mark.asyncio
    async def test_plan_writes_client_own_brand_still_work(self):
        res = await self._q(
            UPDATE_PLAN,
            {"input": {"id": str(self.plan_a.id), "name": "A plan v2"}},
            self.client_a,
        )
        assert res.data["updateActivationPlan"]["success"] is True, res.data
        res = await self._q(
            ATTACH,
            {"input": {
                "activationPlanId": str(self.plan_a.id),
                "requestIds": [str(self.req_a.id)],
            }},
            self.client_a,
        )
        assert res.data["attachRequestsToPlan"]["updatedCount"] == 1
        res = await self._q(
            DETACH, {"input": {"requestIds": [str(self.req_a.id)]}}, self.client_a
        )
        assert res.data["detachRequestsFromPlan"]["updatedCount"] == 1

    @pytest.mark.asyncio
    async def test_plan_writes_admin_any_brand(self):
        res = await self._q(
            UPDATE_PLAN,
            {"input": {"id": str(self.plan_b.id), "name": "B plan v2"}},
            self.admin,
        )
        assert res.data["updateActivationPlan"]["success"] is True, res.data
        res = await self._q(
            DETACH, {"input": {"requestIds": [str(self.req_b.id)]}}, self.admin
        )
        assert res.data["detachRequestsFromPlan"]["updatedCount"] == 1
        res = await self._q(
            DELETE_PLAN, {"input": {"id": str(self.plan_b.id)}}, self.admin
        )
        assert res.data["deleteActivationPlan"]["success"] is True

    # ---------------- Tracker lists (requests / events) ----------------

    async def _names(self, query, key, user, filters):
        res = await self._q(query, {"f": filters}, user)
        if res.errors:
            return None
        return {e["node"]["name"] for e in res.data[key]["edges"]}

    @pytest.mark.asyncio
    async def test_requests_client_cannot_read_other_brand(self):
        for f in (None, {"tenantId": str(self.tenant_b.id)}):
            names = await self._names(REQUESTS, "requests", self.client_a, f)
            assert not names or "B request" not in names, (f, names)

    @pytest.mark.asyncio
    async def test_requests_ba_cannot_read_other_brand(self):
        for f in (None, {"tenantId": str(self.tenant_b.id)}):
            names = await self._names(REQUESTS, "requests", self.ba_a, f)
            assert not names or "B request" not in names, (f, names)

    @pytest.mark.asyncio
    async def test_events_ba_cannot_read_other_brand(self):
        for f in (None, {"tenantId": str(self.tenant_b.id)}):
            names = await self._names(EVENTS, "events", self.ba_a, f)
            assert not names or "B shift" not in names, (f, names)

    @pytest.mark.asyncio
    async def test_requests_admin_sees_all(self):
        names = await self._names(REQUESTS, "requests", self.admin, None)
        assert {"A request", "B request"} <= names

    @pytest.mark.asyncio
    async def test_plan_owner_must_be_brand_member_for_clients(self):
        # Naming another brand's user as owner would echo their name/email.
        outsider = await sync_to_async(self.create_user)(
            username="iso-outsider", email="iso-outsider@brand-b.com",
            role=self.roles["client"],
        )
        await sync_to_async(self.create_tenanted_user)(outsider, self.tenant_b)
        res = await self._q(
            UPDATE_PLAN,
            {"input": {"id": str(self.plan_a.id), "ownerId": str(outsider.id)}},
            self.client_a,
        )
        assert res.data["updateActivationPlan"]["success"] is False
        res = await self._q(
            UPDATE_PLAN,
            {"input": {"id": str(self.plan_a.id), "ownerId": str(self.client_a.id)}},
            self.client_a,
        )
        assert res.data["updateActivationPlan"]["success"] is True, res.data
