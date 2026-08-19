"""Persist Torch's "How many cases to be shipped?" on Request.

The public /spark-form/keee-torch-thc field maps onto
Request.cases_to_be_shipped (GraphQL: casesToBeShipped).
Other tenants leave it null.
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
      casesToBeShipped
    }
  }
}
"""

REQUEST_Q = """
query Req($uuid: ID!) {
  request(uuid: $uuid) {
    uuid
    casesToBeShipped
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestCasesToBeShipped(EventsGraphQLTestCase):
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
            name="Retail Sampling", tenant=self.tenant
        )
        self.timezone = em.TimeZone.objects.create(
            name="UTC", code="UTC", offset=0, created_by=self.system_user
        )

    def test_model_round_trip(self):
        req = em.Request.objects.create(
            name="Torch demo",
            address="1 A St",
            tenant=self.tenant,
            request_type=self.request_type,
            cases_to_be_shipped="12",
            created_by=self.system_user,
        )
        req.refresh_from_db()
        assert req.cases_to_be_shipped == "12"

        other = em.Request.objects.create(
            name="LD demo",
            address="2 B St",
            tenant=self.tenant,
            request_type=self.request_type,
            created_by=self.system_user,
        )
        other.refresh_from_db()
        assert other.cases_to_be_shipped is None

    @pytest.mark.asyncio
    async def test_create_request_persists_and_returns_field(self):
        variables = {
            "input": {
                "name": "Torch store request",
                "date": "2026-08-20T12:00:00+00:00",
                "startTime": "2026-08-20T12:00:00+00:00",
                "endTime": "2026-08-20T16:00:00+00:00",
                "address": "123 Main St",
                "coordinates": [0.0, 0.0],
                "timezoneId": str(self.timezone.id),
                "requestTypeId": str(self.request_type.id),
                "tenantId": str(self.tenant.id),
                "casesToBeShipped": "4",
                "details": [],
                "products": [],
            }
        }
        result = await self._execute_mutation_authenticated(
            CREATE_REQUEST, variables, user=self.admin
        )
        assert result.errors is None, result.errors
        payload = result.data["createRequest"]
        assert payload["success"] is True, payload["message"]
        assert payload["request"]["casesToBeShipped"] == "4"

        uuid = payload["request"]["uuid"]
        fetched = await self._execute_query_authenticated(
            REQUEST_Q, {"uuid": uuid}, user=self.admin
        )
        assert fetched.errors is None, fetched.errors
        assert fetched.data["request"]["casesToBeShipped"] == "4"

        row = await em.Request.objects.aget(uuid=uuid)
        assert row.cases_to_be_shipped == "4"

    @pytest.mark.asyncio
    async def test_create_request_omits_field_when_unset(self):
        variables = {
            "input": {
                "name": "Feel Free request",
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
            }
        }
        result = await self._execute_mutation_authenticated(
            CREATE_REQUEST, variables, user=self.admin
        )
        assert result.errors is None, result.errors
        payload = result.data["createRequest"]
        assert payload["success"] is True, payload["message"]
        assert payload["request"]["casesToBeShipped"] is None
