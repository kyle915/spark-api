"""Persist Torch On-Premise / Event Activation extras on Request.

The public /spark-form/keee-torch-thc request-type selector maps onto:
- On-Premise → accountSpendAmount
- Event Activation → eventAssetsNeeded, loadInTime, onsitePoc, additionalTeamDetails
Other tenants leave them null.
"""

import pytest

from events import models as em
from events.tests.base import EventsGraphQLTestCase


CREATE_REQUEST = """
mutation CreateRequest($input: CreateRequestInput!) {
  createRequest(input: $input) {
    success
    message
    request {
      uuid
      accountSpendAmount
      eventAssetsNeeded
      loadInTime
      onsitePoc
      additionalTeamDetails
    }
  }
}
"""

REQUEST_Q = """
query Req($uuid: ID!) {
  request(uuid: $uuid) {
    uuid
    accountSpendAmount
    eventAssetsNeeded
    loadInTime
    onsitePoc
    additionalTeamDetails
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestTorchActivationFields(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Torch", slug="torch", request_url_name="keee-torch-thc"
        )
        self.admin = self.create_user(
            username="ig-admin",
            email="admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.create_tenanted_user(user=self.admin, tenant=self.tenant)
        self.req_pending = self.create_request_status(
            name="Pending", tenant=self.tenant, slug="pending", is_default=True
        )
        self.create_request_status(
            name="Approved", tenant=self.tenant, slug="approved", create_event=True
        )
        self.request_type = self.create_request_type(
            name="On-Premise", tenant=self.tenant
        )
        self.timezone = em.TimeZone.objects.create(
            name="UTC", code="UTC", offset=0, created_by=self.system_user
        )

    def _base_input(self, **extra):
        return {
            "name": "Torch store request",
            "date": "2026-08-20T12:00:00+00:00",
            "startTime": "2026-08-20T12:00:00+00:00",
            "endTime": "2026-08-20T16:00:00+00:00",
            "address": "123 Main St",
            "coordinates": [0.0, 0.0],
            "timezoneId": str(self.timezone.id),
            "requestTypeId": str(self.request_type.id),
            "tenantId": str(self.tenant.id),
            "details": [],
            "products": [],
            **extra,
        }

    def test_model_round_trip(self):
        req = em.Request.objects.create(
            name="Torch on-prem",
            address="1 A St",
            tenant=self.tenant,
            request_type=self.request_type,
            account_spend_amount="$1,200",
            event_assets_needed="10x10, Barrel Cooler",
            load_in_time="8:00 AM",
            onsite_poc="Jordan Lee",
            additional_team_details="Park in the rear lot",
            created_by=self.system_user,
        )
        req.refresh_from_db()
        assert req.account_spend_amount == "$1,200"
        assert req.event_assets_needed == "10x10, Barrel Cooler"
        assert req.load_in_time == "8:00 AM"
        assert req.onsite_poc == "Jordan Lee"
        assert req.additional_team_details == "Park in the rear lot"

        other = em.Request.objects.create(
            name="LD demo",
            address="2 B St",
            tenant=self.tenant,
            request_type=self.request_type,
            created_by=self.system_user,
        )
        other.refresh_from_db()
        assert other.account_spend_amount is None
        assert other.event_assets_needed is None
        assert other.load_in_time is None
        assert other.onsite_poc is None
        assert other.additional_team_details is None

    @pytest.mark.asyncio
    async def test_create_request_persists_on_premise_amount(self):
        variables = {"input": self._base_input(accountSpendAmount="$500")}
        result = await self._execute_mutation_authenticated(
            CREATE_REQUEST, variables, user=self.admin
        )
        assert result.errors is None, result.errors
        payload = result.data["createRequest"]
        assert payload["success"] is True, payload["message"]
        assert payload["request"]["accountSpendAmount"] == "$500"
        assert payload["request"]["eventAssetsNeeded"] is None

        uuid = payload["request"]["uuid"]
        fetched = await self._execute_query_authenticated(
            REQUEST_Q, {"uuid": uuid}, user=self.admin
        )
        assert fetched.errors is None, fetched.errors
        assert fetched.data["request"]["accountSpendAmount"] == "$500"

        row = await em.Request.objects.aget(uuid=uuid)
        assert row.account_spend_amount == "$500"

    @pytest.mark.asyncio
    async def test_create_request_persists_event_activation_fields(self):
        variables = {
            "input": self._base_input(
                eventAssetsNeeded="10x10, Barrel Cooler",
                loadInTime="8:00 AM",
                onsitePoc="Jordan Lee",
                additionalTeamDetails="Park in the rear lot",
            )
        }
        result = await self._execute_mutation_authenticated(
            CREATE_REQUEST, variables, user=self.admin
        )
        assert result.errors is None, result.errors
        payload = result.data["createRequest"]
        assert payload["success"] is True, payload["message"]
        assert payload["request"]["eventAssetsNeeded"] == "10x10, Barrel Cooler"
        assert payload["request"]["loadInTime"] == "8:00 AM"
        assert payload["request"]["onsitePoc"] == "Jordan Lee"
        assert payload["request"]["additionalTeamDetails"] == "Park in the rear lot"
        assert payload["request"]["accountSpendAmount"] is None

        uuid = payload["request"]["uuid"]
        row = await em.Request.objects.aget(uuid=uuid)
        assert row.event_assets_needed == "10x10, Barrel Cooler"
        assert row.load_in_time == "8:00 AM"
        assert row.onsite_poc == "Jordan Lee"
        assert row.additional_team_details == "Park in the rear lot"

    @pytest.mark.asyncio
    async def test_create_request_omits_fields_when_unset(self):
        variables = {"input": self._base_input()}
        result = await self._execute_mutation_authenticated(
            CREATE_REQUEST, variables, user=self.admin
        )
        assert result.errors is None, result.errors
        payload = result.data["createRequest"]
        assert payload["success"] is True, payload["message"]
        assert payload["request"]["accountSpendAmount"] is None
        assert payload["request"]["eventAssetsNeeded"] is None
        assert payload["request"]["loadInTime"] is None
        assert payload["request"]["onsitePoc"] is None
        assert payload["request"]["additionalTeamDetails"] is None
