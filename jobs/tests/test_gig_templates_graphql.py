"""Tests for the GigTemplate feature (reusable Post-Job-modal defaults).

Structural clone of the BriefingTemplate tests — mirrors
``jobs/tests/test_jobs_tenant_isolation_graphql.py`` (cross-tenant gating)
and ``jobs/tests/test_save_job_briefing_as_template.py`` (snapshot a Job).

Covered (clients schema):
  * createGigTemplate — client pinned to OWN tenant (supplied tenantId
    ignored); admin targets requested tenant.
  * gigTemplates query — returns own-tenant rows only; is_archived filtered
    unless includeArchived.
  * updateGigTemplate / archiveGigTemplate — pk gate: a client can't touch
    another tenant's template ("Template not found."); own-tenant works.
  * saveJobPostAsGigTemplate — snapshots the job's hourly_rate / total_hours
    / uniform_notes, and default_open_to_all == not favorites_only;
    tenant-gated to the job's tenant.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from config.schema_client import schema_clients
from jobs import models
from jobs.tests.base import JobsGraphQLTestCase

User = get_user_model()


CREATE_TEMPLATE_MUTATION = """
mutation CreateTpl($input: CreateGigTemplateInput!) {
  createGigTemplate(input: $input) {
    success message gigTemplate {
      uuid name hourlyRate totalHours uniformNotes defaultOpenToAll tenantId
    }
  }
}
"""

UPDATE_TEMPLATE_MUTATION = """
mutation UpdateTpl($input: UpdateGigTemplateInput!) {
  updateGigTemplate(input: $input) {
    success message gigTemplate { uuid name hourlyRate defaultOpenToAll }
  }
}
"""

ARCHIVE_TEMPLATE_MUTATION = """
mutation ArchiveTpl($input: ArchiveGigTemplateInput!) {
  archiveGigTemplate(input: $input) { success message }
}
"""

SAVE_JOB_MUTATION = """
mutation Save($input: SaveJobPostAsGigTemplateInput!) {
  saveJobPostAsGigTemplate(input: $input) {
    success message gigTemplate {
      uuid name hourlyRate totalHours uniformNotes defaultOpenToAll tenantId
    }
  }
}
"""

TEMPLATES_QUERY = """
query Templates($tenantId: ID, $includeArchived: Boolean!) {
  gigTemplates(tenantId: $tenantId, includeArchived: $includeArchived) {
    uuid name tenantId isArchived
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestGigTemplatesGraphQL(JobsGraphQLTestCase):
    """CRUD + cross-tenant isolation for GigTemplate ops on the clients
    schema."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Gigs Mine")
        self.other_tenant = self.create_tenant(name="Gigs Theirs")

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

    async def _template(self, tenant, *, name="Tpl", **kwargs) -> models.GigTemplate:
        return await sync_to_async(models.GigTemplate.objects.create)(
            tenant=tenant, name=name, **kwargs
        )

    async def _job(self, tenant, *, name="Gig", code="J1", **kwargs) -> models.Job:
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
            **kwargs
        )

    # == createGigTemplate (tenantId honored -> own) =========================

    @pytest.mark.asyncio
    async def test_client_create_template_pinned_to_own_tenant(self):
        user = await self._client_user_for("gct-x", "gctx@test.com", self.tenant)

        result = await self._execute_mutation(
            CREATE_TEMPLATE_MUTATION,
            {"input": {
                "name": "Injected",
                "tenantId": str(self.other_tenant.id),
                "hourlyRate": 25.5,
                "uniformNotes": "Black tee",
                "defaultOpenToAll": True,
            }},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        payload = result.data["createGigTemplate"]
        assert payload["success"] is True
        tpl = payload["gigTemplate"]
        # Pinned to the caller's own tenant, NOT the supplied (other) one.
        assert tpl["tenantId"] == str(self.tenant.id)
        assert tpl["hourlyRate"] == 25.5
        assert tpl["uniformNotes"] == "Black tee"
        assert tpl["defaultOpenToAll"] is True
        # total_hours left blank -> null.
        assert tpl["totalHours"] is None
        # Nothing landed on the targeted (other) tenant.
        leaked = await sync_to_async(
            models.GigTemplate.objects.filter(
                tenant_id=self.other_tenant.id
            ).exists
        )()
        assert leaked is False

    @pytest.mark.asyncio
    async def test_admin_create_template_targets_requested_tenant(self):
        admin = await self._admin_user("gct-a", "gcta@test.com")

        result = await self._execute_mutation(
            CREATE_TEMPLATE_MUTATION,
            {"input": {"name": "AdminMade",
                       "tenantId": str(self.other_tenant.id)}},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        payload = result.data["createGigTemplate"]
        assert payload["success"] is True
        assert payload["gigTemplate"]["tenantId"] == str(self.other_tenant.id)
        # Unset optional defaults: open-to-all defaults False.
        assert payload["gigTemplate"]["defaultOpenToAll"] is False

    # == update / archive gig template (pk gate) ============================

    @pytest.mark.asyncio
    async def test_client_cannot_update_other_tenant_template(self):
        user = await self._client_user_for("gut-x", "gutx@test.com", self.tenant)
        theirs = await self._template(self.other_tenant, name="Original")

        result = await self._execute_mutation(
            UPDATE_TEMPLATE_MUTATION,
            {"input": {"templateId": str(theirs.id), "name": "Hacked"}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["updateGigTemplate"]["success"] is False
        assert result.data["updateGigTemplate"]["message"] == "Template not found."
        refreshed = await sync_to_async(
            models.GigTemplate.objects.get
        )(pk=theirs.pk)
        assert refreshed.name == "Original"

    @pytest.mark.asyncio
    async def test_client_cannot_archive_other_tenant_template(self):
        user = await self._client_user_for("gat-x", "gatx@test.com", self.tenant)
        theirs = await self._template(self.other_tenant)

        result = await self._execute_mutation(
            ARCHIVE_TEMPLATE_MUTATION,
            {"input": {"templateId": str(theirs.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["archiveGigTemplate"]["success"] is False
        refreshed = await sync_to_async(
            models.GigTemplate.objects.get
        )(pk=theirs.pk)
        assert refreshed.is_archived is False

    @pytest.mark.asyncio
    async def test_client_can_update_own_tenant_template(self):
        user = await self._client_user_for("gut-o", "guto@test.com", self.tenant)
        mine = await self._template(
            self.tenant, name="MineOrig", hourly_rate=20, default_open_to_all=False,
        )

        result = await self._execute_mutation(
            UPDATE_TEMPLATE_MUTATION,
            {"input": {
                "templateId": str(mine.id),
                "name": "MineNew",
                "hourlyRate": 30.0,
                "defaultOpenToAll": True,
            }},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["updateGigTemplate"]["success"] is True
        refreshed = await sync_to_async(
            models.GigTemplate.objects.get
        )(pk=mine.pk)
        assert refreshed.name == "MineNew"
        assert float(refreshed.hourly_rate) == 30.0
        assert refreshed.default_open_to_all is True

    @pytest.mark.asyncio
    async def test_client_can_archive_own_tenant_template(self):
        user = await self._client_user_for("gat-o", "gato@test.com", self.tenant)
        mine = await self._template(self.tenant, name="ToArchive")

        result = await self._execute_mutation(
            ARCHIVE_TEMPLATE_MUTATION,
            {"input": {"templateId": str(mine.id)}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        assert result.data["archiveGigTemplate"]["success"] is True
        refreshed = await sync_to_async(
            models.GigTemplate.objects.get
        )(pk=mine.pk)
        assert refreshed.is_archived is True

    # == gigTemplates query (tenantId scope + archived filter) ==============

    @pytest.mark.asyncio
    async def test_client_templates_pinned_to_own_tenant(self):
        user = await self._client_user_for("glt-x", "gltx@test.com", self.tenant)
        await self._template(self.tenant, name="Mine")
        await self._template(self.other_tenant, name="Theirs")

        # Ask for the OTHER tenant's templates -> scoped to caller's own.
        result = await self._execute_mutation(
            TEMPLATES_QUERY,
            {"tenantId": str(self.other_tenant.id), "includeArchived": False},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        rows = result.data["gigTemplates"]
        names = {r["name"] for r in rows}
        assert names == {"Mine"}
        assert all(r["tenantId"] == str(self.tenant.id) for r in rows)

    @pytest.mark.asyncio
    async def test_archived_filtered_unless_requested(self):
        user = await self._client_user_for("glt-a", "glta@test.com", self.tenant)
        await self._template(self.tenant, name="Live")
        await self._template(self.tenant, name="Dead", is_archived=True)

        # Default: archived hidden.
        result = await self._execute_mutation(
            TEMPLATES_QUERY,
            {"tenantId": str(self.tenant.id), "includeArchived": False},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        names = {r["name"] for r in result.data["gigTemplates"]}
        assert names == {"Live"}

        # includeArchived: both returned.
        result2 = await self._execute_mutation(
            TEMPLATES_QUERY,
            {"tenantId": str(self.tenant.id), "includeArchived": True},
            self.endpoint_path, user=user,
        )
        assert result2.errors is None
        names2 = {r["name"] for r in result2.data["gigTemplates"]}
        assert names2 == {"Live", "Dead"}

    # == saveJobPostAsGigTemplate ===========================================

    @pytest.mark.asyncio
    async def test_save_snapshots_job_post_settings(self):
        """favorites_only=True -> default_open_to_all should be False."""
        admin = await self._admin_user("gsj-a", "gsja@test.com")
        job = await self._job(
            self.tenant,
            hourly_rate=22.50,
            total_hours=6.00,
            uniform_notes="All black, closed-toe shoes",
            favorites_only=True,
        )

        result = await self._execute_mutation(
            SAVE_JOB_MUTATION,
            {"input": {"jobId": str(job.id), "name": "Standard sampling gig"}},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None, f"errors: {result.errors}"
        payload = result.data["saveJobPostAsGigTemplate"]
        assert payload["success"] is True
        tpl = payload["gigTemplate"]
        assert tpl["name"] == "Standard sampling gig"
        assert tpl["hourlyRate"] == 22.5
        assert tpl["totalHours"] == 6.0
        assert tpl["uniformNotes"] == "All black, closed-toe shoes"
        # favorites_only True -> NOT open to all.
        assert tpl["defaultOpenToAll"] is False
        assert tpl["tenantId"] == str(self.tenant.id)

        # Persisted under the job's tenant.
        tpl_row = await sync_to_async(
            lambda: models.GigTemplate.objects.get(uuid=tpl["uuid"])
        )()
        assert await sync_to_async(lambda: tpl_row.tenant_id)() == self.tenant.id

    @pytest.mark.asyncio
    async def test_save_open_to_all_is_inverse_of_favorites_only(self):
        """favorites_only=False -> default_open_to_all should be True."""
        admin = await self._admin_user("gsj-o", "gsjo@test.com")
        job = await self._job(
            self.tenant, code="J2", hourly_rate=18, favorites_only=False,
        )

        result = await self._execute_mutation(
            SAVE_JOB_MUTATION,
            {"input": {"jobId": str(job.id), "name": "Open gig"}},
            self.endpoint_path, user=admin,
        )
        assert result.errors is None
        tpl = result.data["saveJobPostAsGigTemplate"]["gigTemplate"]
        assert tpl["defaultOpenToAll"] is True

    @pytest.mark.asyncio
    async def test_client_cannot_save_other_tenant_job(self):
        user = await self._client_user_for("gsj-x", "gsjx@test.com", self.tenant)
        theirs = await self._job(self.other_tenant, code="JX", hourly_rate=40)

        result = await self._execute_mutation(
            SAVE_JOB_MUTATION,
            {"input": {"jobId": str(theirs.id), "name": "Stolen"}},
            self.endpoint_path, user=user,
        )
        assert result.errors is None
        payload = result.data["saveJobPostAsGigTemplate"]
        assert payload["success"] is False
        assert payload["message"] == "Job not found."
        # No template leaked under either tenant.
        leaked = await sync_to_async(
            models.GigTemplate.objects.filter(name="Stolen").exists
        )()
        assert leaked is False
