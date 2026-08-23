"""Recap approval audit fields (approved_by / approved_at)."""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.mutations import _stamp_recap_approval

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


@pytest.mark.django_db(transaction=True)
class TestRecapApprovalAudit(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.spark_admin = self.create_user(
            username="admin-recap-audit",
            email="admin-recap-audit@test.com",
            role=self.roles["spark_admin"],
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

    def _make_recap(self, approved=False):
        return recap_models.Recap.objects.create(
            name="Legacy recap",
            approved=approved,
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_custom_recap(self, approved=False):
        return recap_models.CustomRecap.objects.create(
            name="Custom recap",
            approved=approved,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_approve_recap_stamps_approved_by(self):
        recap = await sync_to_async(self._make_recap)(approved=False)
        result = await self._execute_mutation_authenticated(
            APPROVE_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        assert result.data["approveRecap"]["success"] is True
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(id=recap.id)
        assert refreshed.approved is True
        assert refreshed.approved_by_id == self.spark_admin.id
        assert refreshed.approved_at is not None

    @pytest.mark.asyncio
    async def test_decline_recap_clears_approved_by(self):
        recap = await sync_to_async(self._make_recap)(approved=True)
        recap.approved_by = self.spark_admin
        await sync_to_async(recap.save)(update_fields=["approved_by"])
        result = await self._execute_mutation_authenticated(
            APPROVE_RECAP_MUTATION,
            {"id": str(recap.id), "approved": False},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(id=recap.id)
        assert refreshed.approved is False
        assert refreshed.approved_by_id is None
        assert refreshed.approved_at is None

    @pytest.mark.asyncio
    async def test_approve_custom_recap_stamps_approved_by(self):
        recap = await sync_to_async(self._make_custom_recap)(approved=False)
        result = await self._execute_mutation_authenticated(
            APPROVE_CUSTOM_RECAP_MUTATION,
            {"id": str(recap.id), "approved": True},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        assert result.data["approveCustomRecap"]["success"] is True
        refreshed = await sync_to_async(recap_models.CustomRecap.objects.get)(
            id=recap.id
        )
        assert refreshed.approved is True
        assert refreshed.approved_by_id == self.spark_admin.id
        assert refreshed.approved_at is not None

    def test_stamp_recap_approval_helper(self):
        recap = self._make_recap(approved=False)
        _stamp_recap_approval(recap, approved=True, actor=self.spark_admin)
        assert recap.approved is True
        assert recap.approved_by_id == self.spark_admin.id
        assert recap.approved_at is not None
        _stamp_recap_approval(recap, approved=False, actor=self.spark_admin)
        assert recap.approved is False
        assert recap.approved_by_id is None
        assert recap.approved_at is None
