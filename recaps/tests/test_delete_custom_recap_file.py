"""
Coverage for the deleteCustomRecapFile mutation (clients schema).

An admin must be able to remove a single misfiled file (e.g. a receipt
that landed under "Table setup") from a CustomRecap's Evidences &
Attachments gallery. The mutation:

  * hard-deletes the CustomRecapFile row,
  * LEAVES the GCS blob in place (audit / recoverability),
  * returns the parent custom recap with the refreshed file list,
  * is tenant-scoped + admin-only (mirrors deleteCustomRecap):
      - ambassadors are blocked,
      - a client-role user can only delete inside its own tenant,
      - a spark-admin can delete anywhere.

Runs end to end against the real schema_clients GraphQL surface.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import sync_to_async

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


DELETE_CUSTOM_RECAP_FILE_MUTATION = """
mutation DeleteCustomRecapFile($id: ID!) {
  deleteCustomRecapFile(input: { id: $id }) {
    success
    message
    customRecap {
      uuid
      customRecapFiles { id name }
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestDeleteCustomRecapFile(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-file-delete",
            email="admin-file-delete@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-file-delete",
            email="client-file-delete@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.ba_user = self.create_user(
            username="ba-file-delete",
            email="ba-file-delete@test.com",
            role=self.roles["ambassador"],
        )

        now = datetime.now(_tz.utc)
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
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )

    def _make_recap(self, tenant, event):
        return recap_models.CustomRecap.objects.create(
            name="GB recap",
            approved=False,
            event=event,
            tenant=tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_file(self, recap, name="misfiled-receipt.jpg"):
        return recap_models.CustomRecapFile.objects.create(
            name=name,
            url="recaps/receipts/abc/123-receipt.jpg",
            file_type=self.file_type,
            custom_recap=recap,
            approved=False,
            created_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_spark_admin_deletes_custom_recap_file(self):
        recap = await sync_to_async(self._make_recap)(self.tenant, self.event)
        rec_file = await sync_to_async(self._make_file)(recap)
        keep = await sync_to_async(self._make_file)(recap, name="keep.jpg")

        result = await self._execute_mutation_authenticated(
            DELETE_CUSTOM_RECAP_FILE_MUTATION,
            {"id": str(rec_file.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteCustomRecapFile"]
        assert payload["success"] is True, payload

        # Row gone; sibling file still present in the returned list.
        gone = not await sync_to_async(
            recap_models.CustomRecapFile.objects.filter(id=rec_file.id).exists
        )()
        assert gone
        remaining_ids = {
            f["name"] for f in payload["customRecap"]["customRecapFiles"]
        }
        assert "keep.jpg" in remaining_ids
        assert "misfiled-receipt.jpg" not in remaining_ids
        still_keep = await sync_to_async(
            recap_models.CustomRecapFile.objects.filter(id=keep.id).exists
        )()
        assert still_keep

    @pytest.mark.asyncio
    async def test_ambassador_cannot_delete_custom_recap_file(self):
        recap = await sync_to_async(self._make_recap)(self.tenant, self.event)
        rec_file = await sync_to_async(self._make_file)(recap)

        result = await self._execute_mutation_authenticated(
            DELETE_CUSTOM_RECAP_FILE_MUTATION,
            {"id": str(rec_file.id)},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteCustomRecapFile"]
        assert payload["success"] is False
        assert "authorized" in payload["message"].lower()
        still = await sync_to_async(
            recap_models.CustomRecapFile.objects.filter(id=rec_file.id).exists
        )()
        assert still is True

    @pytest.mark.asyncio
    async def test_client_cannot_delete_other_tenant_custom_recap_file(self):
        other_event = await sync_to_async(self.create_event)(
            name="Foreign event",
            tenant=self.other_tenant,
            date=datetime.now(_tz.utc),
        )
        foreign_recap = await sync_to_async(self._make_recap)(
            self.other_tenant, other_event
        )
        rec_file = await sync_to_async(self._make_file)(foreign_recap)

        result = await self._execute_mutation_authenticated(
            DELETE_CUSTOM_RECAP_FILE_MUTATION,
            {"id": str(rec_file.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteCustomRecapFile"]
        assert payload["success"] is False
        assert "authorized" in payload["message"].lower()
        still = await sync_to_async(
            recap_models.CustomRecapFile.objects.filter(id=rec_file.id).exists
        )()
        assert still is True

    @pytest.mark.asyncio
    async def test_delete_missing_custom_recap_file_fails_cleanly(self):
        result = await self._execute_mutation_authenticated(
            DELETE_CUSTOM_RECAP_FILE_MUTATION,
            {"id": "999999999"},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteCustomRecapFile"]
        assert payload["success"] is False
        assert "not found" in payload["message"].lower()
