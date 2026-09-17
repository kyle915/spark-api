"""Activation Plan GraphQL + attach/filter tests."""

import pytest
from django.utils import timezone

from events import models as em
from events.tests.base import EventsGraphQLTestCase

CREATE_PLAN = """
mutation CreatePlan($input: CreateActivationPlanInput!) {
  createActivationPlan(input: $input) {
    success
    message
    activationPlan {
      id
      uuid
      name
      markets
      pipeline { planned approved staffed executed verified }
    }
  }
}
"""

ATTACH = """
mutation Attach($input: AttachRequestsToPlanInput!) {
  attachRequestsToPlan(input: $input) {
    success
    message
    updatedCount
    activationPlan {
      id
      requestCount
      pipeline { planned approved staffed executed verified }
    }
  }
}
"""

PLANS_Q = """
query Plans($tenantId: ID) {
  activationPlans(tenantId: $tenantId) {
    id
    name
    requestCount
    pipeline { planned approved staffed executed verified }
  }
}
"""

REQUESTS_BY_PLAN = """
query Reqs($filters: RequestFiltersInput) {
  requests(first: 50, filters: $filters) {
    totalCount
    edges { node { uuid name activationPlan { id name } } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestActivationPlans(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Plans Tenant")
        self.admin = self.create_user(
            username="plans-admin",
            email="plans-admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.sys = self.get_system_user()
        self.rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.pending = self.create_request_status(
            name="Pending", tenant=self.tenant, slug="pending"
        )
        self.approved = self.create_request_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )
        today = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        self.req_a = em.Request.objects.create(
            name="Plan Req A",
            date=today,
            address="1 Main",
            coordinates=[0.0, 0.0],
            request_type=self.rt,
            status=self.pending,
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.req_b = em.Request.objects.create(
            name="Plan Req B",
            date=today,
            address="2 Main",
            coordinates=[0.0, 0.0],
            request_type=self.rt,
            status=self.approved,
            tenant=self.tenant,
            created_by=self.sys,
        )

    @pytest.mark.asyncio
    async def test_create_attach_filter_pipeline(self):
        create = await self._execute_mutation(
            CREATE_PLAN,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "name": "Q4 Pacific",
                    "conceptBrief": "Retail sampling push",
                    "markets": ["CA", "OR"],
                    "startDate": "2026-10-01",
                    "endDate": "2026-12-31",
                }
            },
            user=self.admin,
        )
        assert create.errors is None, create.errors
        payload = create.data["createActivationPlan"]
        assert payload["success"] is True, payload
        plan = payload["activationPlan"]
        assert plan["name"] == "Q4 Pacific"
        assert plan["markets"] == ["CA", "OR"]
        assert plan["pipeline"]["planned"] == 0

        attach = await self._execute_mutation(
            ATTACH,
            {
                "input": {
                    "activationPlanId": plan["id"],
                    "requestIds": [str(self.req_a.id), str(self.req_b.id)],
                }
            },
            user=self.admin,
        )
        assert attach.errors is None, attach.errors
        ap = attach.data["attachRequestsToPlan"]
        assert ap["success"] is True, ap
        assert ap["updatedCount"] == 2
        assert ap["activationPlan"]["pipeline"]["planned"] == 2
        assert ap["activationPlan"]["pipeline"]["approved"] == 1

        listed = await self._execute_mutation(
            PLANS_Q,
            {"tenantId": str(self.tenant.id)},
            user=self.admin,
        )
        assert listed.errors is None, listed.errors
        names = [p["name"] for p in listed.data["activationPlans"]]
        assert "Q4 Pacific" in names

        filtered = await self._execute_mutation(
            REQUESTS_BY_PLAN,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "activationPlanId": plan["id"],
                }
            },
            user=self.admin,
        )
        assert filtered.errors is None, filtered.errors
        assert filtered.data["requests"]["totalCount"] == 2
