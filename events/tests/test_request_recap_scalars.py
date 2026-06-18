"""
Master Tracker perf scalars on the Request type: recapsFiledCount /
recapEventUuid / eventsCount.

Includes a GraphQL-EXECUTION test (not just calling helpers directly) — the
earlier version only exercised the helper functions and so missed that the
resolvers called methods on `self` (the Django model), which threw
'Request' object has no attribute '_events_from_cache' at query time and
blanked the admin app. This runs the real `requests` query through the
clients schema and asserts the scalars resolve without error.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from events import models as em
from events.types import Request as RequestGQL
from recaps import models as rm
from events.tests.base import EventsGraphQLTestCase


REQUESTS_Q = """
query Reqs($filters: RequestFiltersInput) {
  requests(first: 50, filters: $filters) {
    edges { node { uuid recapsFiledCount recapEventUuid eventsCount } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRequestRecapScalars(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Scalars Tenant")
        self.admin = self.create_user(
            username="ig-admin",
            email="admin@igniteproductions.co",  # Ignite domain → admin access
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.sys = self.get_system_user()
        rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.req = em.Request.objects.create(
            name="WF Burbank", address="1 A St", request_type=rt,
            tenant=self.tenant, created_by=self.sys,
        )
        self.ev_a = self.create_event(name="Day 1", tenant=self.tenant, request=self.req)
        self.create_event(name="Day 2", tenant=self.tenant, request=self.req)
        rm.Recap.objects.create(name="r1", event=self.ev_a, created_by=self.sys, updated_by=self.sys)
        rm.Recap.objects.create(name="r2", event=self.ev_a, created_by=self.sys, updated_by=self.sys)
        et = self.create_event_type("Sampling", self.tenant)
        tpl = rm.CustomRecapTemplate.objects.create(
            name="tpl", event_type=et, tenant=self.tenant, created_by=self.sys
        )
        rm.CustomRecap.objects.create(
            name="cr", event=self.ev_a, tenant=self.tenant,
            custom_recap_template=tpl, created_by=self.sys, updated_by=self.sys,
        )

    @pytest.mark.asyncio
    async def test_requests_query_resolves_scalars_without_error(self):
        res = await self._execute_mutation(
            REQUESTS_Q,
            {"filters": {"tenantId": str(self.tenant.id)}},
            user=self.admin,
        )
        # The whole point: the resolvers must NOT throw (the bug threw
        # AttributeError on self._events_from_cache, returning errors + null).
        assert res.errors is None, res.errors
        nodes = [e["node"] for e in res.data["requests"]["edges"]]
        row = next(n for n in nodes if n["uuid"] == str(self.req.uuid))
        assert row["recapsFiledCount"] == 3  # 2 legacy + 1 custom
        assert row["eventsCount"] == 2
        assert row["recapEventUuid"] == str(self.ev_a.uuid)

    def test_recap_total_no_nplus1_off_prefetch(self):
        rows = list(
            em.Request.objects.filter(tenant=self.tenant).prefetch_related(
                "event_set", "event_set__recaps", "event_set__custom_recap"
            )
        )
        r = rows[0]
        with CaptureQueriesContext(connection) as ctx:
            cached = r._prefetched_objects_cache.get("event_set")
            total = sum(RequestGQL._recap_total(ev) for ev in cached)
        assert len(ctx.captured_queries) == 0, ctx.captured_queries
        assert total == 3
