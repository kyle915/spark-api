"""
Access-scoping sweep 4: every access-control use of the old sync
``is_spark_schema_request`` now goes through the DB-backed
``SparkGraphQLMixin.is_admin_request``
(``_is_admin_access(*resolve_request_user_access(user))``).

The old check read ``user.role`` synchronously. Two failure modes, both
exercised here through the real clients schema:

  * An admin whose JWT role didn't hydrate (async resolver) read as a
    non-admin, fell through to the membership lookup and got "not a member
    of this tenant" (admins aren't members of client tenants).
  * A removed admin (IGNITE_ADMIN_EXCLUDE) whose request user still carried
    the spark-admin role passed as an admin and skipped the tenant gate.

Sites covered in this module:
  - utils/graphql/mixins.py      get_user_tenant, BaseMutationService.set_user_and_tenant
  - utils/graphql/queries.py     resolve_query_tenant_id, get_single_record
  - ambassadors/services.py      the 7 list queries
  - events/activation_plans.py   activationPlans, activationPlan
"""

from contextlib import contextmanager

import pytest
from asgiref.sync import sync_to_async

from ambassadors.models import AmbassadorGroup, AmbassadorNote, GroupType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import ActivationPlan
from jobs.models import JobTitle


@contextmanager
def _excluded(email):
    from utils.graphql.permissions import IGNITE_ADMIN_EXCLUDE

    IGNITE_ADMIN_EXCLUDE.add(email)
    try:
        yield
    finally:
        IGNITE_ADMIN_EXCLUDE.discard(email)


STATUSES = """
query { ambassadorJobStatuses(first: 50) { edges { node { name } } } }
"""

STATUS = """
query S($id: ID) { ambassadorJobStatus(id: $id) { name } }
"""

CREATE_TITLE = """
mutation C($input: CreateJobTitleInput!) {
  createJobTitle(input: $input) { success message }
}
"""

NOTES = """
query N($filters: AmbassadorNoteFiltersInput) {
  ambassadorNotes(first: 50, filters: $filters) { edges { node { note } } }
}
"""

LIST_QUERIES = {
    "sentInvitations": "query { sentInvitations(first: 5) { totalCount } }",
    "availableAmbassadors": "query { availableAmbassadors(first: 5) { totalCount } }",
    "invitedGroupsByJob": (
        'query { invitedGroupsByJob(first: 5, filters: {jobId: "1"}) { totalCount } }'
    ),
    "ambassadors": "query { ambassadors(first: 5) { totalCount } }",
    "ambassadorReviews": "query { ambassadorReviews(first: 5) { totalCount } }",
    "ambassadorNotes": "query { ambassadorNotes(first: 5) { totalCount } }",
    "ambassadorSkills": "query { ambassadorSkills(first: 5) { totalCount } }",
}

GROUPS = """
query { ambassadorGroups(first: 50) { edges { node { name } } } }
"""

PLANS = """
query P($tenantId: ID) { activationPlans(tenantId: $tenantId) { name } }
"""

PLAN = """
query P($id: ID) { activationPlan(id: $id) { name } }
"""


@pytest.mark.django_db(transaction=True)
class TestAdminRequestGateSweep(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant_a = self.create_tenant(name="Sweep Brand A")
        self.tenant_b = self.create_tenant(name="Sweep Brand B")

        # Real spark-admin, member of nothing (admins aren't client members).
        self.admin = self.create_user(
            username="sw-admin", email="sw-admin@test.com",
            role=self.roles["spark_admin"],
        )
        # Removed Ignite admin: stale spark-admin role, member of tenant A.
        self.removed_email = "sw-removed@igniteproductions.co"
        self.removed = self.create_user(
            username="sw-removed", email=self.removed_email,
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(self.removed, self.tenant_a)
        self.client_a = self.create_user(
            username="sw-client-a", email="sw-client-a@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_a, self.tenant_a)

        self.status_a = self.create_status("A status", self.tenant_a)
        self.status_b = self.create_status("B status", self.tenant_b)

        ba_user = self.create_user(
            username="sw-ba", email="sw-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)
        AmbassadorNote.objects.create(
            note="A note", tenant=self.tenant_a, ambassador=self.ba,
            created_by=self.sys,
        )
        AmbassadorNote.objects.create(
            note="B note", tenant=self.tenant_b, ambassador=self.ba,
            created_by=self.sys,
        )
        group_type = GroupType.objects.create(name="Sweep type", created_by=self.sys)
        for tenant, name in ((self.tenant_a, "A group"), (self.tenant_b, "B group")):
            AmbassadorGroup.objects.create(
                name=name, group_type=group_type, tenant=tenant,
                created_by=self.sys,
            )
        self.plan_b = ActivationPlan.objects.create(
            name="B plan", tenant=self.tenant_b,
        )

    def _unhydrated_admin(self):
        # The JWT request user's role FK doesn't load inside async
        # resolvers; the DB row still says spark-admin.
        self.admin.role = None
        return self.admin

    async def _run(self, query, user, variables=None):
        return await self._execute_mutation(
            query, variables or {}, self.endpoint_path, user=user
        )

    # ── utils/graphql/queries.py: resolve_query_tenant_id ────────────

    @pytest.mark.asyncio
    async def test_list_unhydrated_admin_sees_all_tenants(self):
        result = await self._run(GROUPS, self._unhydrated_admin())
        assert result.errors is None, result.errors
        names = {e["node"]["name"] for e in result.data["ambassadorGroups"]["edges"]}
        assert {"A group", "B group"} <= names

    @pytest.mark.asyncio
    async def test_list_client_still_scoped_to_own_tenant(self):
        result = await self._run(GROUPS, self.client_a)
        assert result.errors is None, result.errors
        names = {e["node"]["name"] for e in result.data["ambassadorGroups"]["edges"]}
        assert names == {"A group"}

    # ── utils/graphql/queries.py: get_single_record ──────────────────

    @pytest.mark.asyncio
    async def test_single_record_unhydrated_admin_reads_any_tenant(self):
        result = await self._run(
            STATUS, self._unhydrated_admin(), {"id": str(self.status_b.id)}
        )
        assert result.errors is None, result.errors
        assert result.data["ambassadorJobStatus"] == {"name": "B status"}

    @pytest.mark.asyncio
    async def test_single_record_removed_admin_cannot_read_other_tenant(self):
        with _excluded(self.removed_email):
            result = await self._run(
                STATUS, self.removed, {"id": str(self.status_b.id)}
            )
        assert result.errors is None, result.errors
        assert result.data["ambassadorJobStatus"] is None

    @pytest.mark.asyncio
    async def test_single_record_client_still_scoped(self):
        result = await self._run(
            STATUS, self.client_a, {"id": str(self.status_b.id)}
        )
        assert result.data["ambassadorJobStatus"] is None
        result = await self._run(
            STATUS, self.client_a, {"id": str(self.status_a.id)}
        )
        assert result.data["ambassadorJobStatus"] == {"name": "A status"}

    # ── utils/graphql/mixins.py: get_user_tenant (explicit tenant) ───

    @pytest.mark.asyncio
    async def test_explicit_tenant_removed_admin_needs_membership(self):
        with _excluded(self.removed_email):
            result = await self._run(
                NOTES, self.removed,
                {"filters": {"tenantId": str(self.tenant_b.id)}},
            )
        assert result.errors, "removed admin must not read tenant B's notes"
        assert "B note" not in str(result.data)

    @pytest.mark.asyncio
    async def test_explicit_tenant_unhydrated_admin_reads_any_tenant(self):
        result = await self._run(
            NOTES, self._unhydrated_admin(),
            {"filters": {"tenantId": str(self.tenant_b.id)}},
        )
        assert result.errors is None, result.errors
        notes = [e["node"]["note"] for e in result.data["ambassadorNotes"]["edges"]]
        assert notes == ["B note"]

    @pytest.mark.asyncio
    async def test_explicit_tenant_client_still_needs_membership(self):
        result = await self._run(
            NOTES, self.client_a, {"filters": {"tenantId": str(self.tenant_b.id)}}
        )
        assert result.errors
        assert "B note" not in str(result.data)

    # ── utils/graphql/mixins.py: set_user_and_tenant (create path) ───

    async def _title_count(self, tenant):
        return await sync_to_async(
            JobTitle.objects.filter(tenant=tenant, name="Sweep title").count
        )()

    @pytest.mark.asyncio
    async def test_create_removed_admin_cannot_write_other_tenant(self):
        with _excluded(self.removed_email):
            result = await self._run(
                CREATE_TITLE, self.removed,
                {"input": {"name": "Sweep title", "tenantId": str(self.tenant_b.id)}},
            )
        assert result.data["createJobTitle"]["success"] is False, result.data
        assert await self._title_count(self.tenant_b) == 0

    @pytest.mark.asyncio
    async def test_create_staff_admin_writes_named_tenant(self):
        """is_staff is an admin by the DB-backed check though the role slug
        is "client" (the old sync check said no and demanded membership)."""
        staff = await sync_to_async(self.create_user)(
            username="sw-staff", email="sw-staff@test.com",
            role=self.roles["client"], is_staff=True,
        )
        result = await self._run(
            CREATE_TITLE, staff,
            {"input": {"name": "Sweep title", "tenantId": str(self.tenant_b.id)}},
        )
        assert result.data["createJobTitle"]["success"] is True, result.data
        assert await self._title_count(self.tenant_b) == 1

    # ── ambassadors/services.py: the 7 list queries ──────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", sorted(LIST_QUERIES))
    async def test_ambassador_lists_unhydrated_admin_not_refused(self, field):
        result = await self._run(LIST_QUERIES[field], self._unhydrated_admin())
        assert result.errors is None, (field, result.errors)
        assert result.data[field]["totalCount"] >= 0

    @pytest.mark.asyncio
    async def test_ambassador_notes_removed_admin_scoped_to_own_tenant(self):
        with _excluded(self.removed_email):
            result = await self._run(NOTES, self.removed)
        assert result.errors is None, result.errors
        notes = {e["node"]["note"] for e in result.data["ambassadorNotes"]["edges"]}
        assert notes == {"A note"}

    @pytest.mark.asyncio
    async def test_ambassador_notes_admin_sees_all(self):
        result = await self._run(NOTES, self.admin)
        assert result.errors is None, result.errors
        notes = {e["node"]["note"] for e in result.data["ambassadorNotes"]["edges"]}
        assert {"A note", "B note"} <= notes

    # ── events/activation_plans.py ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_activation_plans_removed_admin_needs_membership(self):
        with _excluded(self.removed_email):
            result = await self._run(
                PLANS, self.removed, {"tenantId": str(self.tenant_b.id)}
            )
        assert result.errors
        assert "B plan" not in str(result.data)

    @pytest.mark.asyncio
    async def test_activation_plans_unhydrated_admin_reads_any_tenant(self):
        result = await self._run(
            PLANS, self._unhydrated_admin(), {"tenantId": str(self.tenant_b.id)}
        )
        assert result.errors is None, result.errors
        assert result.data["activationPlans"] == [{"name": "B plan"}]

    @pytest.mark.asyncio
    async def test_activation_plan_removed_admin_reads_as_not_found(self):
        with _excluded(self.removed_email):
            result = await self._run(PLAN, self.removed, {"id": str(self.plan_b.id)})
        assert result.errors is None, result.errors
        assert result.data["activationPlan"] is None

    @pytest.mark.asyncio
    async def test_activation_plan_client_other_tenant_reads_as_not_found(self):
        result = await self._run(PLAN, self.client_a, {"id": str(self.plan_b.id)})
        assert result.errors is None, result.errors
        assert result.data["activationPlan"] is None

    @pytest.mark.asyncio
    async def test_activation_plan_unhydrated_admin_reads_any_tenant(self):
        result = await self._run(
            PLAN, self._unhydrated_admin(), {"id": str(self.plan_b.id)}
        )
        assert result.errors is None, result.errors
        assert result.data["activationPlan"] == {"name": "B plan"}
