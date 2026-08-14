"""Bulk approve + mark-shared — same tenant gate as single-row approve."""

from datetime import datetime, timedelta, timezone as _tz

import pytest
from asgiref.sync import sync_to_async

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models

BULK_APPROVE = """
mutation BulkApprove($recapIds: [ID!], $customIds: [ID!], $approved: Boolean!) {
  bulkApproveRecaps(input: {
    recapIds: $recapIds
    customRecapIds: $customIds
    approved: $approved
  }) {
    success
    message
    updatedCount
  }
}
"""

MARK_SHARED = """
mutation MarkShared($recapIds: [ID!], $customIds: [ID!]) {
  markRecapsShared(input: {
    recapIds: $recapIds
    customRecapIds: $customIds
  }) {
    success
    message
    updatedCount
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestBulkApproveRecaps(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer Bulk")
        self.other_tenant = self.create_tenant(name="Other Bulk")
        self.spark_admin = self.create_user(
            username="admin-bulk-recap",
            email="admin-bulk-recap@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-bulk-recap",
            email="client-bulk-recap@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Bulk event",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.other_event = self.create_event(
            name="Foreign bulk event",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )

    def _make_recap(self, event, approved=False):
        return recap_models.Recap.objects.create(
            name="Legacy recap",
            approved=approved,
            event=event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_admin_can_bulk_approve_same_tenant(self):
        recap = await sync_to_async(self._make_recap)(self.event)
        result = await self._execute_mutation_authenticated(
            BULK_APPROVE,
            {
                "recapIds": [str(recap.id)],
                "customIds": [],
                "approved": True,
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["bulkApproveRecaps"]
        assert payload["success"] is True, payload
        assert payload["updatedCount"] == 1
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(id=recap.id)
        assert refreshed.approved is True

    @pytest.mark.asyncio
    async def test_client_cannot_bulk_approve_other_tenant(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)
        result = await self._execute_mutation_authenticated(
            BULK_APPROVE,
            {
                "recapIds": [str(recap.id)],
                "customIds": [],
                "approved": True,
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["bulkApproveRecaps"]
        assert payload["success"] is False, payload
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(id=recap.id)
        assert refreshed.approved is False

    @pytest.mark.asyncio
    async def test_admin_can_mark_shared(self):
        recap = await sync_to_async(self._make_recap)(self.event)
        result = await self._execute_mutation_authenticated(
            MARK_SHARED,
            {"recapIds": [str(recap.id)], "customIds": []},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["markRecapsShared"]
        assert payload["success"] is True, payload
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(id=recap.id)
        assert refreshed.shared_at is not None
