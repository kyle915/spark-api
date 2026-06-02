"""
Cross-tenant write isolation for the recap MUTATION cluster (clients schema).

Regression coverage for the recap-mutation IDOR: the recap read resolvers
(`recaps` / `customRecaps`) were already tenant-scoped, but the *write* path
(`approveRecap`, `approveCustomRecap`, `declineCustomRecap`, `addRecapFile`,
`addCustomRecapFile`, `removeRecapFile`, `updateCustomRecap`, ...) loaded the
recap by raw global id gated only by `StrictIsAuthenticated`. That let ANY
authenticated user (a client of another brand, or a BA) approve / decline /
edit / add-file-to another tenant's recap by guessing its id.

These run end to end against the real `schema_clients` GraphQL surface and
assert, for BOTH legacy `Recap` and `CustomRecap`, that:

  * a client/user of tenant A CANNOT approve / decline / edit / add-file-to a
    tenant B recap (success=False, "authorized", and NOTHING is mutated), and
  * a same-tenant client AND a spark-admin still CAN.

Mirrors the fixture style of test_delete_custom_recap_file /
test_recap_delete_and_multi.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import sync_to_async

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


# ── Mutations under test ──────────────────────────────────────────────

APPROVE_RECAP_MUTATION = """
mutation ApproveRecap($id: ID!, $approved: Boolean!) {
  approveRecap(input: { id: $id, approved: $approved }) {
    success
    message
    recap { uuid approved }
  }
}
"""

APPROVE_CUSTOM_RECAP_MUTATION = """
mutation ApproveCustomRecap($id: ID!, $approved: Boolean!) {
  approveCustomRecap(input: { id: $id, approved: $approved }) {
    success
    message
    customRecap { uuid approved }
  }
}
"""

DECLINE_CUSTOM_RECAP_MUTATION = """
mutation DeclineCustomRecap($id: ID!) {
  declineCustomRecap(input: { id: $id }) {
    success
    message
    customRecap { uuid approved }
  }
}
"""

ADD_RECAP_FILE_MUTATION = """
mutation AddRecapFile($recapId: ID!, $file: String!) {
  addRecapFile(input: { recapId: $recapId, file: $file }) {
    success
    message
    recap { uuid }
  }
}
"""

ADD_CUSTOM_RECAP_FILE_MUTATION = """
mutation AddCustomRecapFile($customRecapId: ID!, $file: String!) {
  addCustomRecapFile(input: { customRecapId: $customRecapId, file: $file }) {
    success
    message
    customRecap { uuid }
  }
}
"""

REMOVE_RECAP_FILE_MUTATION = """
mutation RemoveRecapFile($id: ID!) {
  removeRecapFile(input: { id: $id }) {
    success
    message
    recap { uuid }
  }
}
"""

UPDATE_CUSTOM_RECAP_MUTATION = """
mutation UpdateCustomRecap(
  $id: ID!
  $eventId: ID!
  $templateId: ID!
  $name: String!
) {
  updateCustomRecap(
    input: {
      id: $id
      eventId: $eventId
      customRecapTemplateId: $templateId
      name: $name
    }
  ) {
    success
    message
    customRecap { uuid name }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapMutationTenantIsolation(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        # Tenant A (the caller's tenant) and tenant B (the victim).
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-recap-iso",
            email="admin-recap-iso@test.com",
            role=self.roles["spark_admin"],
        )
        # Client belongs to tenant A only.
        self.client_user = self.create_user(
            username="client-recap-iso",
            email="client-recap-iso@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        # Ambassador (role 1) — must be blocked from approve/decline.
        self.ba_user = self.create_user(
            username="ba-recap-iso",
            email="ba-recap-iso@test.com",
            role=self.roles["ambassador"],
        )

        now = datetime.now(_tz.utc)
        # Tenant A event + supporting rows.
        self.event = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        # Tenant B event + supporting rows (the foreign recaps live here).
        self.other_event = self.create_event(
            name="Foreign event",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.other_event_type = self.create_event_type(
            name="Sampling B", tenant=self.other_tenant
        )
        self.other_template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.other_event_type,
            tenant=self.other_tenant,
            created_by=self.system_user,
        )
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )

    # ── builders ──────────────────────────────────────────────────────

    def _make_recap(self, event, approved=False):
        return recap_models.Recap.objects.create(
            name="Legacy recap",
            approved=approved,
            event=event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_custom_recap(self, event, tenant, template, approved=False):
        return recap_models.CustomRecap.objects.create(
            name="Custom recap",
            approved=approved,
            event=event,
            tenant=tenant,
            custom_recap_template=template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_recap_file(self, recap, name="photo.jpg"):
        return recap_models.RecapFile.objects.create(
            name=name,
            file="recaps/abc/123-photo.jpg",
            file_type=self.file_type,
            recap=recap,
            approved=False,
            created_by=self.system_user,
        )

    async def _refresh_recap(self, recap):
        return await sync_to_async(recap_models.Recap.objects.get)(id=recap.id)

    async def _refresh_custom(self, recap):
        return await sync_to_async(recap_models.CustomRecap.objects.get)(
            id=recap.id
        )

    # ════════════════════════════════════════════════════════════════
    # approveRecap (legacy Recap)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_approve_other_tenant_recap(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            APPROVE_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["approveRecap"]
        assert payload["success"] is False, payload
        assert "authorized" in payload["message"].lower()
        # NOT mutated.
        refreshed = await self._refresh_recap(recap)
        assert refreshed.approved is False

    @pytest.mark.asyncio
    async def test_ambassador_cannot_approve_recap(self):
        recap = await sync_to_async(self._make_recap)(self.event)

        result = await self._execute_mutation_authenticated(
            APPROVE_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["approveRecap"]
        assert payload["success"] is False, payload
        assert "authorized" in payload["message"].lower()
        refreshed = await self._refresh_recap(recap)
        assert refreshed.approved is False

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_approve_recap(self):
        recap = await sync_to_async(self._make_recap)(self.event)

        result = await self._execute_mutation_authenticated(
            APPROVE_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["approveRecap"]
        assert payload["success"] is True, payload
        refreshed = await self._refresh_recap(recap)
        assert refreshed.approved is True

    @pytest.mark.asyncio
    async def test_spark_admin_can_approve_other_tenant_recap(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            APPROVE_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["approveRecap"]
        assert payload["success"] is True, payload
        refreshed = await self._refresh_recap(recap)
        assert refreshed.approved is True

    # ════════════════════════════════════════════════════════════════
    # approveCustomRecap / declineCustomRecap (CustomRecap)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_approve_other_tenant_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            APPROVE_CUSTOM_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["approveCustomRecap"]
        assert payload["success"] is False, payload
        assert "authorized" in payload["message"].lower()
        refreshed = await self._refresh_custom(recap)
        assert refreshed.approved is False

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_approve_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.event, self.tenant, self.template
        )

        result = await self._execute_mutation_authenticated(
            APPROVE_CUSTOM_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["approveCustomRecap"]
        assert payload["success"] is True, payload
        refreshed = await self._refresh_custom(recap)
        assert refreshed.approved is True

    @pytest.mark.asyncio
    async def test_spark_admin_can_approve_other_tenant_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            APPROVE_CUSTOM_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["approveCustomRecap"]
        assert payload["success"] is True, payload
        refreshed = await self._refresh_custom(recap)
        assert refreshed.approved is True

    @pytest.mark.asyncio
    async def test_client_cannot_decline_other_tenant_custom_recap(self):
        # Start approved so a successful decline would be observable.
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template,
            approved=True,
        )

        result = await self._execute_mutation_authenticated(
            DECLINE_CUSTOM_RECAP_MUTATION,
            {"id": str(recap.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["declineCustomRecap"]
        assert payload["success"] is False, payload
        assert "authorized" in payload["message"].lower()
        refreshed = await self._refresh_custom(recap)
        assert refreshed.approved is True  # untouched

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_decline_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.event, self.tenant, self.template, approved=True
        )

        result = await self._execute_mutation_authenticated(
            DECLINE_CUSTOM_RECAP_MUTATION,
            {"id": str(recap.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["declineCustomRecap"]
        assert payload["success"] is True, payload
        refreshed = await self._refresh_custom(recap)
        assert refreshed.approved is False

    # ════════════════════════════════════════════════════════════════
    # addRecapFile / addCustomRecapFile (file attach)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_add_file_to_other_tenant_recap(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            ADD_RECAP_FILE_MUTATION,
            {"recapId": str(recap.id), "file": "recaps/x/1-photo.jpg"},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["addRecapFile"]
        assert payload["success"] is False, payload
        assert "authorized" in payload["message"].lower()
        # No file row created for the foreign recap.
        count = await sync_to_async(
            recap_models.RecapFile.objects.filter(recap=recap).count
        )()
        assert count == 0

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_add_recap_file(self):
        recap = await sync_to_async(self._make_recap)(self.event)

        result = await self._execute_mutation_authenticated(
            ADD_RECAP_FILE_MUTATION,
            {"recapId": str(recap.id), "file": "recaps/x/1-photo.jpg"},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["addRecapFile"]
        assert payload["success"] is True, payload
        count = await sync_to_async(
            recap_models.RecapFile.objects.filter(recap=recap).count
        )()
        assert count == 1

    @pytest.mark.asyncio
    async def test_client_cannot_add_file_to_other_tenant_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            ADD_CUSTOM_RECAP_FILE_MUTATION,
            {"customRecapId": str(recap.id), "file": "recaps/x/1-photo.jpg"},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["addCustomRecapFile"]
        assert payload["success"] is False, payload
        assert "authorized" in payload["message"].lower()
        count = await sync_to_async(
            recap_models.CustomRecapFile.objects.filter(
                custom_recap=recap
            ).count
        )()
        assert count == 0

    # ════════════════════════════════════════════════════════════════
    # removeRecapFile (file delete + blob)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_remove_file_from_other_tenant_recap(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)
        rec_file = await sync_to_async(self._make_recap_file)(recap)

        result = await self._execute_mutation_authenticated(
            REMOVE_RECAP_FILE_MUTATION,
            {"id": str(rec_file.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["removeRecapFile"]
        assert payload["success"] is False, payload
        assert "authorized" in payload["message"].lower()
        # File still present.
        still = await sync_to_async(
            recap_models.RecapFile.objects.filter(id=rec_file.id).exists
        )()
        assert still is True

    # ════════════════════════════════════════════════════════════════
    # updateCustomRecap (value-edit)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_update_other_tenant_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            UPDATE_CUSTOM_RECAP_MUTATION,
            {
                "id": str(recap.id),
                "eventId": str(self.other_event.id),
                "templateId": str(self.other_template.id),
                "name": "HACKED NAME",
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["updateCustomRecap"]
        assert payload["success"] is False, payload
        assert "authorized" in payload["message"].lower()
        refreshed = await self._refresh_custom(recap)
        assert refreshed.name == "Custom recap"  # untouched

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_update_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.event, self.tenant, self.template
        )

        result = await self._execute_mutation_authenticated(
            UPDATE_CUSTOM_RECAP_MUTATION,
            {
                "id": str(recap.id),
                "eventId": str(self.event.id),
                "templateId": str(self.template.id),
                "name": "Renamed by owner",
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["updateCustomRecap"]
        assert payload["success"] is True, payload
        refreshed = await self._refresh_custom(recap)
        assert refreshed.name == "Renamed by owner"
