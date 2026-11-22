"""GraphQL smoke tests for Ignite staffing overflow + smart BA matching."""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from staffing.models import IgniteStaffingRequest

REQUEST_MUTATION = """
mutation RequestIgniteStaffing($input: RequestIgniteStaffingInput!) {
  requestIgniteStaffing(input: $input) {
    success
    message
    request {
      uuid
      status
      requestedShiftCount
      title
    }
  }
}
"""

UPDATE_MUTATION = """
mutation UpdateIgniteStaffing($input: UpdateIgniteStaffingRequestInput!) {
  updateIgniteStaffingRequestStatus(input: $input) {
    success
    message
    request { uuid status }
  }
}
"""

SMART_MATCH_QUERY = """
query SmartBaMatches($eventId: ID) {
  smartBaMatches(eventId: $eventId, limit: 5) {
    baUuid
    name
    score
    summaryReason
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestIgniteStaffingSchema(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Overflow Brand")

        self.spark_admin = self.create_user(
            username="staff-overflow",
            email="staff-overflow@igniteproductions.co",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-overflow",
            email="client-overflow@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

    @pytest.mark.asyncio
    async def test_client_can_request_ignite_staffing_for_own_tenant(self):
        result = await self._execute_mutation(
            REQUEST_MUTATION,
            {
                "input": {
                    "source": "wave_sheet",
                    "title": "Wave rescue",
                    "requestedShiftCount": 3,
                    "requestedBaCount": 3,
                    "requestedDates": ["2026-12-01"],
                    "requestedStores": ["Sprouts #12"],
                    "notes": "Unstaffed Friday wave",
                }
            },
            user=self.client_user,
        )
        assert result.errors is None, result.errors
        payload = result.data["requestIgniteStaffing"]
        assert payload["success"] is True
        assert "billing" in (payload["message"] or "").lower()
        assert "stripe" not in (payload["message"] or "").lower()
        assert payload["request"]["status"] == "pending"
        assert payload["request"]["requestedShiftCount"] == 3
        count = await sync_to_async(
            IgniteStaffingRequest.objects.filter(tenant=self.tenant).count
        )()
        assert count == 1

    @pytest.mark.asyncio
    async def test_client_cannot_update_status_but_staff_can(self):
        row = await sync_to_async(IgniteStaffingRequest.objects.create)(
            tenant=self.tenant,
            requested_by=self.client_user,
            title="Needs review",
            requested_shift_count=2,
            requested_ba_count=2,
        )
        denied = await self._execute_mutation(
            UPDATE_MUTATION,
            {"input": {"uuid": str(row.uuid), "status": "accepted"}},
            user=self.client_user,
        )
        assert denied.errors is None, denied.errors
        assert denied.data["updateIgniteStaffingRequestStatus"]["success"] is False

        ok = await self._execute_mutation(
            UPDATE_MUTATION,
            {
                "input": {
                    "uuid": str(row.uuid),
                    "status": "accepted",
                    "opsNotes": "Taking it",
                }
            },
            user=self.spark_admin,
        )
        assert ok.errors is None, ok.errors
        payload = ok.data["updateIgniteStaffingRequestStatus"]
        assert payload["success"] is True
        assert payload["request"]["status"] == "accepted"

        def _check():
            row.refresh_from_db()
            return row.accepted_at is not None

        assert await sync_to_async(_check)()

    @pytest.mark.asyncio
    async def test_smart_ba_matches_empty_for_missing_event(self):
        result = await self._execute_mutation(
            SMART_MATCH_QUERY,
            {"eventId": "00000000-0000-0000-0000-000000000000"},
            user=self.client_user,
        )
        assert result.errors is None, result.errors
        assert result.data["smartBaMatches"] == []
