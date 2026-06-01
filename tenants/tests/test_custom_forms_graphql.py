"""GraphQL tests for Form Builder definition persistence.

Covers the clients-schema ``tenantForms`` / ``tenantForm`` queries and the
``saveForm`` / ``updateForm`` / ``deleteForm`` mutations backed by
:class:`tenants.models.CustomForm`:

* create -> list round-trips the saved definition (incl. the verbatim schema),
* fetch one by id,
* update mutates only the supplied fields,
* delete removes the row,
* forms are isolated per tenant,
* a client can neither read nor mutate another tenant's forms,
* an admin (spark-admin) may target any tenant via ``tenantId``,
* unauthenticated reads/writes degrade safely (never raise).
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from config.schema_client import schema_clients
from tenants.models import CustomForm
from tenants.tests.base import BaseGraphQLTestCase


User = get_user_model()

TENANT_FORMS_QUERY = """
query TenantForms($tenantId: ID!) {
  tenantForms(tenantId: $tenantId) {
    id
    name
    schema
    isPublished
    createdAt
    updatedAt
  }
}
"""

TENANT_FORM_QUERY = """
query TenantForm($id: ID!) {
  tenantForm(id: $id) {
    id
    name
    schema
    isPublished
  }
}
"""

SAVE_FORM_MUTATION = """
mutation SaveForm($input: SaveFormInput!) {
  saveForm(input: $input) {
    success
    message
    form {
      id
      name
      schema
      isPublished
    }
    clientMutationId
  }
}
"""

UPDATE_FORM_MUTATION = """
mutation UpdateForm($input: UpdateFormInput!) {
  updateForm(input: $input) {
    success
    message
    form {
      id
      name
      schema
      isPublished
    }
    clientMutationId
  }
}
"""

DELETE_FORM_MUTATION = """
mutation DeleteForm($input: DeleteFormInput!) {
  deleteForm(input: $input) {
    success
    message
    deletedId
    clientMutationId
  }
}
"""

# A realistic builder blob (mirrors FormDef in SparkFormBuilder.tsx).
SAMPLE_SCHEMA = {
    "description": "Capture brand-specific intake fields.",
    "internal": True,
    "external": False,
    "fields": [
        {
            "id": "f_1",
            "label": "Location type",
            "kind": "select",
            "required": True,
            "options": ["Retail", "On-premise", "Event"],
        },
        {
            "id": "f_2",
            "label": "Notes",
            "kind": "longtext",
            "required": False,
            "helpText": "Anything else we should know",
        },
    ],
}


@pytest.mark.django_db(transaction=True)
class TestCustomFormsGraphQL(BaseGraphQLTestCase):
    """GraphQL tests for tenant-scoped Form Builder definition persistence."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Forms Tenant")
        self.other_tenant = self.create_tenant(name="Other Tenant")

    async def _client_user_for(self, username, email, tenant) -> User:
        """A client-role user who is a member of ``tenant``."""
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

    @pytest.mark.asyncio
    async def test_save_then_list_round_trip(self):
        """saveForm persists; tenantForms returns the saved definition verbatim."""
        user = await self._client_user_for("forms-rt", "rt@test.com", self.tenant)

        save_result = await self._execute_mutation(
            SAVE_FORM_MUTATION,
            {
                "input": {
                    "name": "Intake form",
                    "schema": SAMPLE_SCHEMA,
                    "isPublished": False,
                    "clientMutationId": "rt-1",
                }
            },
            self.endpoint_path,
            user=user,
        )
        assert save_result.errors is None
        payload = save_result.data["saveForm"]
        assert payload["success"] is True
        assert payload["clientMutationId"] == "rt-1"
        assert payload["form"]["name"] == "Intake form"
        assert payload["form"]["schema"] == SAMPLE_SCHEMA
        assert payload["form"]["isPublished"] is False
        new_id = payload["form"]["id"]

        # Row persisted, scoped to the tenant, with created_by set.
        form = await sync_to_async(CustomForm.objects.get)(pk=int(new_id))
        assert form.tenant_id == self.tenant.id
        assert form.created_by_id == user.id
        assert form.schema == SAMPLE_SCHEMA

        # List reflects it.
        list_result = await self._execute_mutation(
            TENANT_FORMS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.endpoint_path,
            user=user,
        )
        assert list_result.errors is None
        forms = list_result.data["tenantForms"]
        assert len(forms) == 1
        assert forms[0]["id"] == new_id
        assert forms[0]["schema"] == SAMPLE_SCHEMA

    @pytest.mark.asyncio
    async def test_fetch_one_by_id(self):
        """tenantForm returns a single definition the caller can access."""
        user = await self._client_user_for("forms-one", "one@test.com", self.tenant)
        form = await sync_to_async(CustomForm.objects.create)(
            tenant=self.tenant, name="Solo", schema=SAMPLE_SCHEMA, created_by=user
        )

        result = await self._execute_mutation(
            TENANT_FORM_QUERY,
            {"id": str(form.id)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["tenantForm"]["id"] == str(form.id)
        assert result.data["tenantForm"]["name"] == "Solo"
        assert result.data["tenantForm"]["schema"] == SAMPLE_SCHEMA

    @pytest.mark.asyncio
    async def test_update_changes_only_supplied_fields(self):
        """updateForm patches name/schema/isPublished; omitted fields survive."""
        user = await self._client_user_for("forms-upd", "upd@test.com", self.tenant)
        form = await sync_to_async(CustomForm.objects.create)(
            tenant=self.tenant,
            name="Before",
            schema={"description": "old", "fields": []},
            is_published=False,
            created_by=user,
        )

        result = await self._execute_mutation(
            UPDATE_FORM_MUTATION,
            {
                "input": {
                    "id": str(form.id),
                    "name": "After",
                    "isPublished": True,
                }
            },
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        payload = result.data["updateForm"]
        assert payload["success"] is True
        assert payload["form"]["name"] == "After"
        assert payload["form"]["isPublished"] is True
        # schema was not supplied -> unchanged.
        assert payload["form"]["schema"] == {"description": "old", "fields": []}

        refreshed = await sync_to_async(CustomForm.objects.get)(pk=form.id)
        assert refreshed.name == "After"
        assert refreshed.is_published is True
        assert refreshed.schema == {"description": "old", "fields": []}

    @pytest.mark.asyncio
    async def test_delete_removes_row(self):
        """deleteForm removes the definition and echoes the deleted id."""
        user = await self._client_user_for("forms-del", "del@test.com", self.tenant)
        form = await sync_to_async(CustomForm.objects.create)(
            tenant=self.tenant, name="Doomed", created_by=user
        )

        result = await self._execute_mutation(
            DELETE_FORM_MUTATION,
            {"input": {"id": str(form.id)}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        payload = result.data["deleteForm"]
        assert payload["success"] is True
        assert payload["deletedId"] == str(form.id)

        exists = await sync_to_async(
            CustomForm.objects.filter(pk=form.id).exists
        )()
        assert exists is False

    @pytest.mark.asyncio
    async def test_forms_are_isolated_per_tenant(self):
        """A client lists only their own tenant's forms."""
        user = await self._client_user_for("forms-iso", "iso@test.com", self.tenant)
        await sync_to_async(CustomForm.objects.create)(
            tenant=self.tenant, name="Mine", created_by=user
        )
        await sync_to_async(CustomForm.objects.create)(
            tenant=self.other_tenant, name="Theirs"
        )

        result = await self._execute_mutation(
            TENANT_FORMS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.endpoint_path,
            user=user,
        )
        forms = result.data["tenantForms"]
        assert len(forms) == 1
        assert forms[0]["name"] == "Mine"

    @pytest.mark.asyncio
    async def test_client_cannot_list_other_tenant_forms(self):
        """A client passing another tenant's id is pinned to their own tenant."""
        user = await self._client_user_for(
            "forms-x-list", "xlist@test.com", self.tenant
        )
        await sync_to_async(CustomForm.objects.create)(
            tenant=self.tenant, name="Mine", created_by=user
        )
        await sync_to_async(CustomForm.objects.create)(
            tenant=self.other_tenant, name="Theirs"
        )

        # Ask for the OTHER tenant's forms; scoping overrides to own tenant.
        result = await self._execute_mutation(
            TENANT_FORMS_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.endpoint_path,
            user=user,
        )
        forms = result.data["tenantForms"]
        assert [f["name"] for f in forms] == ["Mine"]

    @pytest.mark.asyncio
    async def test_client_cannot_fetch_other_tenant_form(self):
        """tenantForm returns null for a form outside the client's tenant."""
        user = await self._client_user_for(
            "forms-x-get", "xget@test.com", self.tenant
        )
        other = await sync_to_async(CustomForm.objects.create)(
            tenant=self.other_tenant, name="Theirs"
        )

        result = await self._execute_mutation(
            TENANT_FORM_QUERY,
            {"id": str(other.id)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["tenantForm"] is None

    @pytest.mark.asyncio
    async def test_client_cannot_update_other_tenant_form(self):
        """updateForm safely fails for a form outside the client's tenant."""
        user = await self._client_user_for(
            "forms-x-upd", "xupd@test.com", self.tenant
        )
        other = await sync_to_async(CustomForm.objects.create)(
            tenant=self.other_tenant, name="Theirs", is_published=False
        )

        result = await self._execute_mutation(
            UPDATE_FORM_MUTATION,
            {"input": {"id": str(other.id), "name": "Hijacked"}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        payload = result.data["updateForm"]
        assert payload["success"] is False
        assert payload["form"] is None

        # Untouched in the DB.
        refreshed = await sync_to_async(CustomForm.objects.get)(pk=other.id)
        assert refreshed.name == "Theirs"

    @pytest.mark.asyncio
    async def test_client_cannot_delete_other_tenant_form(self):
        """deleteForm safely fails for a form outside the client's tenant."""
        user = await self._client_user_for(
            "forms-x-del", "xdel@test.com", self.tenant
        )
        other = await sync_to_async(CustomForm.objects.create)(
            tenant=self.other_tenant, name="Theirs"
        )

        result = await self._execute_mutation(
            DELETE_FORM_MUTATION,
            {"input": {"id": str(other.id)}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["deleteForm"]["success"] is False

        # Still there.
        exists = await sync_to_async(
            CustomForm.objects.filter(pk=other.id).exists
        )()
        assert exists is True

    @pytest.mark.asyncio
    async def test_admin_can_target_any_tenant(self):
        """A spark-admin may save to / list any tenant via tenantId."""
        admin = await self._admin_user("forms-admin", "admin@test.com")

        save_result = await self._execute_mutation(
            SAVE_FORM_MUTATION,
            {
                "input": {
                    "name": "Admin form",
                    "tenantId": str(self.other_tenant.id),
                    "schema": {"fields": []},
                }
            },
            self.endpoint_path,
            user=admin,
        )
        assert save_result.errors is None
        assert save_result.data["saveForm"]["success"] is True

        form = await sync_to_async(
            CustomForm.objects.get
        )(pk=int(save_result.data["saveForm"]["form"]["id"]))
        assert form.tenant_id == self.other_tenant.id

        list_result = await self._execute_mutation(
            TENANT_FORMS_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.endpoint_path,
            user=admin,
        )
        names = [f["name"] for f in list_result.data["tenantForms"]]
        assert "Admin form" in names

    @pytest.mark.asyncio
    async def test_unauthenticated_ops_are_denied_not_crashed(self):
        """Unauthenticated ops are rejected by StrictIsAuthenticated.

        Mirrors every other tenant-scoped op on the clients schema (e.g. the
        recaps campaign-report queries): the permission denies before the
        resolver runs, so the field resolves to null with an
        "Authentication required." error rather than crashing the request or
        leaking data. (Distinct from ``myPreferences``, which is intentionally
        permission-less because it can return safe defaults to anyone.)
        """
        list_result = await self._execute_mutation(
            TENANT_FORMS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.endpoint_path,
        )
        # tenantForms is non-null ([CustomFormType!]!), so the denied field
        # null-propagates to the whole data payload — but it errors cleanly
        # rather than crashing, and no rows are exposed.
        assert list_result.errors is not None
        assert "authentication required" in str(list_result.errors[0]).lower()
        assert list_result.data is None

        save_result = await self._execute_mutation(
            SAVE_FORM_MUTATION,
            {"input": {"name": "Nope", "schema": {"fields": []}}},
            self.endpoint_path,
        )
        # saveForm returns SaveFormResponse! (non-null), so the denied field
        # null-propagates the whole payload too — still a clean auth error,
        # and nothing was written.
        assert save_result.errors is not None
        assert save_result.data is None
        wrote = await sync_to_async(
            CustomForm.objects.filter(name="Nope").exists
        )()
        assert wrote is False
