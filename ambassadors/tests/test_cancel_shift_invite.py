"""
Tests for AmbassadorMutations.cancel_shift_invite — the admin-side
mutation that retracts a pending invite or removes an accepted BA
from a shift.

Coverage focuses on:
- Pending invite → row deleted, success
- Accepted invite → row deleted, success (admin can kick a BA)
- Missing row → idempotent success=False, no crash
- Non-admin caller → permission denied
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()

MUTATION = """
mutation Cancel($input: CancelShiftInviteInput!) {
  cancelShiftInvite(input: $input) {
    success
    message
    ambassadorEventUuid
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestCancelShiftInvite(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Cancel Tenant")
        self.admin = self.create_user(
            username="adm-cancel",
            email="adm-cancel@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-cancel",
            email="ba-cancel@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.event = self.create_event(
            name="Cancel Shift", tenant=self.tenant
        )

    async def _make_invite(self, *, approved: bool) -> AmbassadorEvent:
        return await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            is_approved=approved,
            created_by=self.admin,
        )

    @pytest.mark.asyncio
    async def test_cancel_pending_invite(self):
        ae = await self._make_invite(approved=False)
        result = await self._execute_mutation(
            MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["cancelShiftInvite"]
        assert payload["success"] is True
        assert payload["ambassadorEventUuid"] == str(ae.uuid)

        # Row deleted from DB
        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(uuid=ae.uuid).exists
        )()
        assert exists is False

    @pytest.mark.asyncio
    async def test_remove_accepted_ba(self):
        """Admin can also remove an already-accepted BA. Same delete."""
        ae = await self._make_invite(approved=True)
        result = await self._execute_mutation(
            MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None
        assert result.data["cancelShiftInvite"]["success"] is True

        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(uuid=ae.uuid).exists
        )()
        assert exists is False

    @pytest.mark.asyncio
    async def test_missing_row_is_idempotent(self):
        """Cancelling a row that doesn't exist returns success=False
        with a helpful message, not a 500."""
        # Use a well-formed UUID that doesn't match any row.
        result = await self._execute_mutation(
            MUTATION,
            {
                "input": {
                    "ambassadorEventUuid": "00000000-0000-0000-0000-000000000000"
                }
            },
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None
        payload = result.data["cancelShiftInvite"]
        assert payload["success"] is False
        assert "already declined" in payload["message"].lower() or \
               "not found" in payload["message"].lower()

    @pytest.mark.asyncio
    async def test_non_admin_caller_denied(self):
        """A BA can't call cancelShiftInvite — that's the admin's
        path. The BA's own decline uses respondToShiftOffer."""
        ae = await self._make_invite(approved=False)
        result = await self._execute_mutation(
            MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            self.endpoint_path,
            user=self.ba_user,  # BA, not admin
        )
        # Permission denied surfaces as either errors[] or
        # success=False — both are acceptable; the key is that the
        # row stays.
        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(uuid=ae.uuid).exists
        )()
        assert exists is True
