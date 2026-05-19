"""
Tests for the Academy app: model + queries + mutations across the
three schemas (Spark admin, Client, Mobile).

Coverage focuses on the BA-facing security contract: drafts must
never leak to mobile, even if the caller passes published=False on
the filter.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from academy.models import AcademyModule
from tenants.tests.base import BaseGraphQLTestCase
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestAcademyModule(BaseGraphQLTestCase):
    """Model + admin-side query + mutation coverage."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.tenant = self.create_tenant(name="Academy Test Tenant")

    async def create_user_async(self, **kwargs):
        return await sync_to_async(self.create_user)(**kwargs)

    # -----------------------------------------------------------------
    # Model
    # -----------------------------------------------------------------

    def test_module_defaults(self):
        m = AcademyModule.objects.create(
            tenant=self.tenant,
            title="Brand 101",
        )
        assert m.uuid is not None
        assert m.kind == "training"
        assert m.body == ""
        assert m.published is False
        assert m.order == 0

    def test_module_ordering(self):
        AcademyModule.objects.create(
            tenant=self.tenant, title="Second", order=20
        )
        AcademyModule.objects.create(
            tenant=self.tenant, title="First", order=10
        )
        rows = list(AcademyModule.objects.filter(tenant=self.tenant))
        # Meta.ordering = ["order", "-updated_at"]
        assert rows[0].title == "First"
        assert rows[1].title == "Second"

    # -----------------------------------------------------------------
    # academy_modules_admin (Spark + Client schemas)
    # -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_academy_modules_admin_returns_drafts(self):
        await sync_to_async(AcademyModule.objects.create)(
            tenant=self.tenant, title="Draft", published=False
        )
        await sync_to_async(AcademyModule.objects.create)(
            tenant=self.tenant, title="Live", published=True
        )

        user = await self.create_user_async(
            username="admin1",
            email="admin1@test.com",
            role=self.roles["spark_admin"],
        )

        query = """
        query AdminAcademy($filters: AcademyModuleFiltersInput) {
          academyModulesAdmin(filters: $filters) {
            uuid title published
          }
        }
        """
        variables = {"filters": {"tenantId": str(self.tenant.id)}}

        result = await self._execute_mutation(
            query, variables, self.endpoint_path, user=user
        )

        assert result.errors is None, f"query errored: {result.errors}"
        rows = result.data["academyModulesAdmin"]
        titles = sorted(r["title"] for r in rows)
        assert titles == ["Draft", "Live"]

    # -----------------------------------------------------------------
    # academy_modules (Mobile schema) — hard-filter to published=True
    # -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_academy_modules_mobile_drops_drafts(self):
        from config.schema_mobile import schema_mobile

        await sync_to_async(AcademyModule.objects.create)(
            tenant=self.tenant, title="Draft", published=False
        )
        await sync_to_async(AcademyModule.objects.create)(
            tenant=self.tenant, title="Live", published=True
        )

        ba_user = await self.create_user_async(
            username="ba1",
            email="ba1@test.com",
            role=self.roles["ambassador"],
        )

        query = """
        query MobileAcademy($filters: AcademyModuleFiltersInput) {
          academyModules(filters: $filters) {
            uuid title published
          }
        }
        """
        variables = {"filters": {"tenantId": str(self.tenant.id)}}

        # Temporarily override the schema for the mobile call
        original = self.schema
        original_path = self.endpoint_path
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        try:
            result = await self._execute_mutation(
                query, variables, self.endpoint_path, user=ba_user
            )
        finally:
            self.schema = original
            self.endpoint_path = original_path

        assert result.errors is None, f"query errored: {result.errors}"
        rows = result.data["academyModules"]
        # Drafts must not leak — even though filter didn't ask for it.
        assert [r["title"] for r in rows] == ["Live"]
        assert all(r["published"] is True for r in rows)

    @pytest.mark.asyncio
    async def test_academy_modules_mobile_ignores_published_false_filter(
        self,
    ):
        """Even if a BA-side caller passes published=false on the filter
        (e.g. a malicious mobile client), drafts must not be returned."""
        from config.schema_mobile import schema_mobile

        await sync_to_async(AcademyModule.objects.create)(
            tenant=self.tenant, title="Hidden", published=False
        )

        ba_user = await self.create_user_async(
            username="ba2",
            email="ba2@test.com",
            role=self.roles["ambassador"],
        )

        query = """
        query MobileAcademy($filters: AcademyModuleFiltersInput) {
          academyModules(filters: $filters) {
            uuid title published
          }
        }
        """
        variables = {
            "filters": {
                "tenantId": str(self.tenant.id),
                "published": False,  # malicious — must be ignored
            }
        }

        original = self.schema
        original_path = self.endpoint_path
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        try:
            result = await self._execute_mutation(
                query, variables, self.endpoint_path, user=ba_user
            )
        finally:
            self.schema = original
            self.endpoint_path = original_path

        assert result.errors is None, f"query errored: {result.errors}"
        assert result.data["academyModules"] == []

    # -----------------------------------------------------------------
    # Mutations
    # -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_create_academy_module(self):
        user = await self.create_user_async(
            username="admin2",
            email="admin2@test.com",
            role=self.roles["spark_admin"],
        )

        mutation = """
        mutation Create($input: CreateAcademyModuleInput!) {
          createAcademyModule(input: $input) {
            success message
            academyModule { uuid title kind published }
          }
        }
        """
        variables = {
            "input": {
                "title": "Brand 101",
                "kind": "brand",
                "body": "# Welcome",
                "order": 5,
                "published": True,
                "tenantId": str(self.tenant.id),
            }
        }
        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=user
        )

        assert result.errors is None, f"mutation errored: {result.errors}"
        payload = result.data["createAcademyModule"]
        assert payload["success"] is True
        assert payload["academyModule"]["title"] == "Brand 101"
        assert payload["academyModule"]["kind"] == "brand"
        assert payload["academyModule"]["published"] is True

        m = await sync_to_async(AcademyModule.objects.get)(
            uuid=payload["academyModule"]["uuid"]
        )
        assert m.title == "Brand 101"
        assert m.created_by_id == user.id

    @pytest.mark.asyncio
    async def test_update_academy_module(self):
        m = await sync_to_async(AcademyModule.objects.create)(
            tenant=self.tenant, title="Old", published=False
        )

        user = await self.create_user_async(
            username="admin3",
            email="admin3@test.com",
            role=self.roles["spark_admin"],
        )

        mutation = """
        mutation Update($input: UpdateAcademyModuleInput!) {
          updateAcademyModule(input: $input) {
            success
            academyModule { title published }
          }
        }
        """
        variables = {
            "input": {
                "uuid": str(m.uuid),
                "title": "New",
                "published": True,
            }
        }
        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=user
        )

        assert result.errors is None, f"mutation errored: {result.errors}"
        payload = result.data["updateAcademyModule"]
        assert payload["success"] is True
        assert payload["academyModule"]["title"] == "New"
        assert payload["academyModule"]["published"] is True

    @pytest.mark.asyncio
    async def test_delete_academy_module(self):
        m = await sync_to_async(AcademyModule.objects.create)(
            tenant=self.tenant, title="Bye"
        )

        user = await self.create_user_async(
            username="admin4",
            email="admin4@test.com",
            role=self.roles["spark_admin"],
        )

        mutation = """
        mutation Delete($input: DeleteAcademyModuleInput!) {
          deleteAcademyModule(input: $input) {
            success message
          }
        }
        """
        variables = {"input": {"uuid": str(m.uuid)}}
        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=user
        )

        assert result.errors is None, f"mutation errored: {result.errors}"
        assert result.data["deleteAcademyModule"]["success"] is True

        exists = await sync_to_async(
            AcademyModule.objects.filter(uuid=m.uuid).exists
        )()
        assert exists is False
