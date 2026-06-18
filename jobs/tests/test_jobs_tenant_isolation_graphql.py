"""Cross-tenant isolation tests for the Jobs clients-schema ops.

Round-2 of the clients-schema tenant-isolation sweep (same bug class as the
Favorites leak fixed in PR #692 and the academy/announcements clusters in
PR #694): a clients-schema op that honored a client-supplied ``tenantId`` —
or operated on a client-supplied job/template pk belonging to ANOTHER tenant
— without an ownership/admin check.

Proves the fix (``jobs.job_scope.JobScope``): a client-role caller can neither
read nor mutate another tenant's jobs / briefings / templates by supplying a
foreign ``tenantId`` or a foreign resource pk, while same-tenant access and
admin cross-tenant access still work.

Covered ops:
  * JobLifecycleMutations: postJob, postEventToBoard, openJobToAll,
    assignAmbassadorToJob (pk-addressed gate)
  * BriefingTemplateMutations: createBriefingTemplate (tenantId -> own),
    updateBriefingTemplate / archiveBriefingTemplate (pk gate)
  * JobBriefingMutations: setJobBriefing, applyBriefingTemplate (pk gate)
  * Queries: briefingTemplates(tenantId) scope, jobBriefing(jobId) pk gate

Mirrors ``academy/tests/test_academy_isolation_graphql.py`` and
``jobs/tests/test_favorite_ambassadors_graphql.py``.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from config.schema_client import schema_clients
from jobs import models
from jobs.tests.base import JobsGraphQLTestCase

User = get_user_model()


POST_JOB_MUTATION = """
mutation PostJob($input: PostJobInput!) {
  postJob(input: $input) { success message lifecycleStatus jobUuid }
}
"""

POST_EVENT_MUTATION = """
mutation PostEvent($input: PostEventToBoardInput!) {
  postEventToBoard(input: $input) { success message lifecycleStatus }
}
"""

OPEN_JOB_MUTATION = """
mutation OpenJob($input: OpenJobToAllInput!) {
  openJobToAll(input: $input) { success message }
}
"""

ASSIGN_MUTATION = """
mutation Assign($input: AssignAmbassadorToJobInput!) {
  assignAmbassadorToJob(input: $input) { success message lifecycleStatus }
}
"""

CREATE_TEMPLATE_MUTATION = """
mutation CreateTpl($input: CreateBriefingTemplateInput!) {
  createBriefingTemplate(input: $input) {
    success message briefingTemplate { uuid name tenantId }
  }
}
"""

UPDATE_TEMPLATE_MUTATION = """
mutation UpdateTpl($input: UpdateBriefingTemplateInput!) {
  updateBriefingTemplate(input: $input) {
    success message briefingTemplate { uuid name }
  }
}
"""

ARCHIVE_TEMPLATE_MUTATION = """
mutation ArchiveTpl($input: ArchiveBriefingTemplateInput!) {
  archiveBriefingTemplate(input: $input) { success message }
}
"""

SET_BRIEFING_MUTATION = """
mutation SetBriefing($input: SetJobBriefingInput!) {
  setJobBriefing(input: $input) { success message title }
}
"""

APPLY_TEMPLATE_MUTATION = """
mutation ApplyTpl($input: ApplyBriefingTemplateInput!) {
  applyBriefingTemplate(input: $input) { success message title }
}
"""

TEMPLATES_QUERY = """
query Templates($tenantId: ID) {
  briefingTemplates(tenantId: $tenantId) { uuid name tenantId }
}
"""

JOB_BRIEFING_QUERY = """
query JobBriefing($jobId: ID!) {
  jobBriefing(jobId: $jobId) { title body }
}
"""


@pytest.mark.django_db(transaction=True)
class TestJobsTenantIsolationGraphQL(JobsGraphQLTestCase):
    """Cross-tenant isolation for the Jobs lifecycle / briefing / template
    ops on the clients schema."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Jobs Mine")
        self.other_tenant = self.create_tenant(name="Jobs Theirs")

    # -- fixtures ------------------------------------------------------------

    async def _client_user_for(self, username, email, tenant) -> User:
        user = await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["client"],
            password="password123",
        )
        await sync_to_async(self.create_tenanted_user)(user=user, tenant=tenant)
        return user

    async def _admin_user(self, username, email) -> User:
        return await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["spark_admin"],
            password="password123",
        )

    async def _job(self, tenant, *, lifecycle_status=models.Job.STATUS_PENDING,
                   favorites_only=True, name="Gig", code="J1") -> models.Job:
        """A minimal Job graph (event + job_title) for the given tenant."""
        event = await sync_to_async(self.create_event)(
            name=f"{name} Event", tenant=tenant, address="123 St"
        )
        job_title = await sync_to_async(self.create_job_title)(
            name=f"{name} Title", tenant=tenant
        )
        return await sync_to_async(self.create_job)(
            name=name,
            code=code,
            address="123 St",
            event=event,
            job_title=job_title,
            tenant=tenant,
            lifecycle_status=lifecycle_status,
            favorites_only=favorites_only,
        )

    async def _event(self, tenant, name="Ev"):
        return await sync_to_async(self.create_event)(
            name=name, tenant=tenant, address="9 Ave",
            date=timezone.now(),
        )

    async def _template(self, tenant, name="Tpl", title="T", body="B"):
        return await sync_to_async(models.BriefingTemplate.objects.create)(
            tenant=tenant, name=name, title=title, body=body,
        )

    async def _ambassador(self, username, email):
        ba_user = await sync_to_async(self.create_user)(
            username=username, email=email, role=self.roles["ambassador"],
            password="password123", first_name="Bay", last_name="Area",
        )
        return await sync_to_async(self.create_ambassador)(user=ba_user)

    # == postJob =============================================================

    @pytest.mark.asyncio
    async def test_client_cannot_post_other_tenant_job(self):
        user = await self._client_user_for("pj-x", "pjx@test.com", self.tenant)
        theirs = await self._job(self.other_tenant)

        result = await self._execute_mutation(
            POST_JOB_MUTATION,
            {"input": {"id": str(theirs.id), "totalHours": 4.0,
                       "hourlyRate": 25.0}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["postJob"]["success"] is False
        refreshed = await sync_to_async(models.Job.objects.get)(pk=theirs.pk)
        assert refreshed.lifecycle_status == models.Job.STATUS_PENDING

    @pytest.mark.asyncio
    async def test_client_can_post_own_tenant_job(self):
        user = await self._client_user_for("pj-o", "pjo@test.com", self.tenant)
        mine = await self._job(self.tenant)

        result = await self._execute_mutation(
            POST_JOB_MUTATION,
            {"input": {"id": str(mine.id), "totalHours": 4.0,
                       "hourlyRate": 25.0}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["postJob"]["success"] is True
        refreshed = await sync_to_async(models.Job.objects.get)(pk=mine.pk)
        assert refreshed.lifecycle_status == models.Job.STATUS_POSTED
        # Posting must flip the BA-board visibility gates, or the gig never
        # shows on my_available_jobs (which filters ongoing+public).
        assert refreshed.public is True
        assert refreshed.ongoing is True

    @pytest.mark.asyncio
    async def test_admin_can_post_any_tenant_job(self):
        admin = await self._admin_user("pj-a", "pja@test.com")
        theirs = await self._job(self.other_tenant)

        result = await self._execute_mutation(
            POST_JOB_MUTATION,
            {"input": {"id": str(theirs.id), "totalHours": 4.0,
                       "hourlyRate": 25.0}},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        assert result.data["postJob"]["success"] is True
        refreshed = await sync_to_async(models.Job.objects.get)(pk=theirs.pk)
        assert refreshed.lifecycle_status == models.Job.STATUS_POSTED

    # == postEventToBoard ====================================================

    @pytest.mark.asyncio
    async def test_client_cannot_post_other_tenant_event(self):
        user = await self._client_user_for("pe-x", "pex@test.com", self.tenant)
        theirs = await self._event(self.other_tenant)

        result = await self._execute_mutation(
            POST_EVENT_MUTATION,
            {"input": {"eventId": str(theirs.id), "totalHours": 4.0,
                       "hourlyRate": 25.0}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["postEventToBoard"]["success"] is False
        # No job was created for the other tenant's event.
        leaked = await sync_to_async(
            models.Job.objects.filter(event_id=theirs.id).exists
        )()
        assert leaked is False

    @pytest.mark.asyncio
    async def test_admin_can_post_any_tenant_event(self):
        admin = await self._admin_user("pe-a", "pea@test.com")
        theirs = await self._event(self.other_tenant)

        result = await self._execute_mutation(
            POST_EVENT_MUTATION,
            {"input": {"eventId": str(theirs.id), "totalHours": 4.0,
                       "hourlyRate": 25.0}},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        assert result.data["postEventToBoard"]["success"] is True

    # == openJobToAll ========================================================

    @pytest.mark.asyncio
    async def test_client_cannot_open_other_tenant_job(self):
        user = await self._client_user_for("oj-x", "ojx@test.com", self.tenant)
        theirs = await self._job(
            self.other_tenant, lifecycle_status=models.Job.STATUS_POSTED,
            favorites_only=True,
        )

        result = await self._execute_mutation(
            OPEN_JOB_MUTATION,
            {"input": {"id": str(theirs.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["openJobToAll"]["success"] is False
        refreshed = await sync_to_async(models.Job.objects.get)(pk=theirs.pk)
        assert refreshed.favorites_only is True

    @pytest.mark.asyncio
    async def test_client_can_open_own_tenant_job(self):
        user = await self._client_user_for("oj-o", "ojo@test.com", self.tenant)
        mine = await self._job(
            self.tenant, lifecycle_status=models.Job.STATUS_POSTED,
            favorites_only=True,
        )

        result = await self._execute_mutation(
            OPEN_JOB_MUTATION,
            {"input": {"id": str(mine.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["openJobToAll"]["success"] is True
        refreshed = await sync_to_async(models.Job.objects.get)(pk=mine.pk)
        assert refreshed.favorites_only is False

    # == assignAmbassadorToJob (the serious one) =============================

    @pytest.mark.asyncio
    async def test_client_cannot_staff_other_tenant_job(self):
        """A client cannot assign a BA to another tenant's gig."""
        user = await self._client_user_for("as-x", "asx@test.com", self.tenant)
        theirs = await self._job(
            self.other_tenant, lifecycle_status=models.Job.STATUS_POSTED,
        )
        amb = await self._ambassador("as-ba", "asba@test.com")

        result = await self._execute_mutation(
            ASSIGN_MUTATION,
            {"input": {"jobId": str(theirs.id), "ambassadorId": str(amb.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["assignAmbassadorToJob"]["success"] is False
        # Job not filled and no application row was created.
        refreshed = await sync_to_async(models.Job.objects.get)(pk=theirs.pk)
        assert refreshed.lifecycle_status == models.Job.STATUS_POSTED
        leaked = await sync_to_async(
            models.JobApplication.objects.filter(
                job_id=theirs.id, ambassador_id=amb.id
            ).exists
        )()
        assert leaked is False

    @pytest.mark.asyncio
    async def test_admin_can_staff_any_tenant_job(self):
        admin = await self._admin_user("as-a", "asa@test.com")
        theirs = await self._job(
            self.other_tenant, lifecycle_status=models.Job.STATUS_POSTED,
        )
        amb = await self._ambassador("as-ba2", "asba2@test.com")

        result = await self._execute_mutation(
            ASSIGN_MUTATION,
            {"input": {"jobId": str(theirs.id), "ambassadorId": str(amb.id)}},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        assert result.data["assignAmbassadorToJob"]["success"] is True
        refreshed = await sync_to_async(models.Job.objects.get)(pk=theirs.pk)
        assert refreshed.lifecycle_status == models.Job.STATUS_FILLED

    # == createBriefingTemplate (tenantId honored -> own) ====================

    @pytest.mark.asyncio
    async def test_client_create_template_pinned_to_own_tenant(self):
        user = await self._client_user_for("ct-x", "ctx@test.com", self.tenant)

        result = await self._execute_mutation(
            CREATE_TEMPLATE_MUTATION,
            {"input": {"name": "Injected",
                       "tenantId": str(self.other_tenant.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        payload = result.data["createBriefingTemplate"]
        assert payload["success"] is True
        assert payload["briefingTemplate"]["tenantId"] == str(self.tenant.id)
        # Nothing landed on the targeted (other) tenant.
        leaked = await sync_to_async(
            models.BriefingTemplate.objects.filter(
                tenant_id=self.other_tenant.id
            ).exists
        )()
        assert leaked is False

    @pytest.mark.asyncio
    async def test_admin_create_template_targets_requested_tenant(self):
        admin = await self._admin_user("ct-a", "cta@test.com")

        result = await self._execute_mutation(
            CREATE_TEMPLATE_MUTATION,
            {"input": {"name": "AdminMade",
                       "tenantId": str(self.other_tenant.id)}},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        payload = result.data["createBriefingTemplate"]
        assert payload["success"] is True
        assert payload["briefingTemplate"]["tenantId"] == str(
            self.other_tenant.id
        )

    # == update / archive briefing template (pk gate) =======================

    @pytest.mark.asyncio
    async def test_client_cannot_update_other_tenant_template(self):
        user = await self._client_user_for("ut-x", "utx@test.com", self.tenant)
        theirs = await self._template(self.other_tenant, name="Original")

        result = await self._execute_mutation(
            UPDATE_TEMPLATE_MUTATION,
            {"input": {"templateId": str(theirs.id), "name": "Hacked"}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["updateBriefingTemplate"]["success"] is False
        refreshed = await sync_to_async(
            models.BriefingTemplate.objects.get
        )(pk=theirs.pk)
        assert refreshed.name == "Original"

    @pytest.mark.asyncio
    async def test_client_cannot_archive_other_tenant_template(self):
        user = await self._client_user_for("at-x", "atx@test.com", self.tenant)
        theirs = await self._template(self.other_tenant)

        result = await self._execute_mutation(
            ARCHIVE_TEMPLATE_MUTATION,
            {"input": {"templateId": str(theirs.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["archiveBriefingTemplate"]["success"] is False
        refreshed = await sync_to_async(
            models.BriefingTemplate.objects.get
        )(pk=theirs.pk)
        assert refreshed.is_archived is False

    @pytest.mark.asyncio
    async def test_client_can_update_own_tenant_template(self):
        user = await self._client_user_for("ut-o", "uto@test.com", self.tenant)
        mine = await self._template(self.tenant, name="MineOrig")

        result = await self._execute_mutation(
            UPDATE_TEMPLATE_MUTATION,
            {"input": {"templateId": str(mine.id), "name": "MineNew"}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["updateBriefingTemplate"]["success"] is True
        refreshed = await sync_to_async(
            models.BriefingTemplate.objects.get
        )(pk=mine.pk)
        assert refreshed.name == "MineNew"

    # == setJobBriefing (pk gate) ===========================================

    @pytest.mark.asyncio
    async def test_client_cannot_brief_other_tenant_job(self):
        user = await self._client_user_for("sb-x", "sbx@test.com", self.tenant)
        theirs = await self._job(self.other_tenant)

        result = await self._execute_mutation(
            SET_BRIEFING_MUTATION,
            {"input": {"jobId": str(theirs.id), "title": "Hacked Brief"}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["setJobBriefing"]["success"] is False
        refreshed = await sync_to_async(models.Job.objects.get)(pk=theirs.pk)
        assert (refreshed.briefing_title or "") != "Hacked Brief"

    @pytest.mark.asyncio
    async def test_client_can_brief_own_tenant_job(self):
        user = await self._client_user_for("sb-o", "sbo@test.com", self.tenant)
        mine = await self._job(self.tenant)

        result = await self._execute_mutation(
            SET_BRIEFING_MUTATION,
            {"input": {"jobId": str(mine.id), "title": "My Brief"}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["setJobBriefing"]["success"] is True
        refreshed = await sync_to_async(models.Job.objects.get)(pk=mine.pk)
        assert refreshed.briefing_title == "My Brief"

    # == applyBriefingTemplate (both job + template must be in scope) ========

    @pytest.mark.asyncio
    async def test_client_cannot_apply_template_to_other_tenant_job(self):
        user = await self._client_user_for("ap-x", "apx@test.com", self.tenant)
        their_job = await self._job(self.other_tenant)
        their_tpl = await self._template(self.other_tenant, body="Secret Body")

        result = await self._execute_mutation(
            APPLY_TEMPLATE_MUTATION,
            {"input": {"jobId": str(their_job.id),
                       "templateId": str(their_tpl.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["applyBriefingTemplate"]["success"] is False
        refreshed = await sync_to_async(models.Job.objects.get)(pk=their_job.pk)
        assert (refreshed.briefing_body or "") != "Secret Body"

    @pytest.mark.asyncio
    async def test_client_cannot_apply_other_tenant_template_to_own_job(self):
        """Even onto their OWN job, a client can't pull in a foreign template's
        body."""
        user = await self._client_user_for("ap-x2", "apx2@test.com", self.tenant)
        my_job = await self._job(self.tenant)
        their_tpl = await self._template(self.other_tenant, body="Secret Body")

        result = await self._execute_mutation(
            APPLY_TEMPLATE_MUTATION,
            {"input": {"jobId": str(my_job.id),
                       "templateId": str(their_tpl.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["applyBriefingTemplate"]["success"] is False
        refreshed = await sync_to_async(models.Job.objects.get)(pk=my_job.pk)
        assert (refreshed.briefing_body or "") != "Secret Body"

    @pytest.mark.asyncio
    async def test_client_can_apply_own_template_to_own_job(self):
        user = await self._client_user_for("ap-o", "apo@test.com", self.tenant)
        my_job = await self._job(self.tenant)
        my_tpl = await self._template(self.tenant, body="Mine Body")

        result = await self._execute_mutation(
            APPLY_TEMPLATE_MUTATION,
            {"input": {"jobId": str(my_job.id),
                       "templateId": str(my_tpl.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["applyBriefingTemplate"]["success"] is True
        refreshed = await sync_to_async(models.Job.objects.get)(pk=my_job.pk)
        assert refreshed.briefing_body == "Mine Body"

    # == briefingTemplates query (tenantId scope) ===========================

    @pytest.mark.asyncio
    async def test_client_templates_pinned_to_own_tenant(self):
        user = await self._client_user_for("lt-x", "ltx@test.com", self.tenant)
        await self._template(self.tenant, name="Mine")
        await self._template(self.other_tenant, name="Theirs")

        # Ask for the OTHER tenant's templates -> scoped to caller's own.
        result = await self._execute_mutation(
            TEMPLATES_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        rows = result.data["briefingTemplates"]
        assert [r["name"] for r in rows] == ["Mine"]
        assert all(r["tenantId"] == str(self.tenant.id) for r in rows)

    @pytest.mark.asyncio
    async def test_admin_templates_target_requested_tenant(self):
        admin = await self._admin_user("lt-a", "lta@test.com")
        await self._template(self.other_tenant, name="Theirs")

        result = await self._execute_mutation(
            TEMPLATES_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        rows = result.data["briefingTemplates"]
        assert [r["name"] for r in rows] == ["Theirs"]

    # == jobBriefing query (pk gate) ========================================

    @pytest.mark.asyncio
    async def test_client_cannot_read_other_tenant_job_briefing(self):
        user = await self._client_user_for("jb-x", "jbx@test.com", self.tenant)
        theirs = await self._job(self.other_tenant)
        theirs.briefing_title = "Secret"
        theirs.briefing_body = "Secret Body"
        await sync_to_async(theirs.save)()

        result = await self._execute_mutation(
            JOB_BRIEFING_QUERY,
            {"jobId": str(theirs.id)},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["jobBriefing"] is None

    @pytest.mark.asyncio
    async def test_client_can_read_own_tenant_job_briefing(self):
        user = await self._client_user_for("jb-o", "jbo@test.com", self.tenant)
        mine = await self._job(self.tenant)
        mine.briefing_title = "Mine"
        mine.briefing_body = "Mine Body"
        await sync_to_async(mine.save)()

        result = await self._execute_mutation(
            JOB_BRIEFING_QUERY,
            {"jobId": str(mine.id)},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["jobBriefing"]["title"] == "Mine"
