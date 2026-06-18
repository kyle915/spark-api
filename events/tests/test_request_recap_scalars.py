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


# Mirrors the REAL Master Tracker selection (src/api/queries/RequestsQuery.ts),
# not just the 3 scalars — so the test exercises every field the lightweight
# list queryset must still serve (products / openShifts / event + its
# ambassador counts / retailer→location→state) against the dropped prefetches.
# A resolver that throws here is exactly the blank-screen failure mode.
REQUESTS_Q = """
query Reqs($filters: RequestFiltersInput) {
  requests(first: 50, filters: $filters) {
    totalCount
    edges {
      node {
        id
        uuid
        name
        date
        address
        schedulingStatus
        state { code }
        location { name }
        retailer { name location { name state { code } } }
        requestType { name }
        products { id name }
        openShifts { uuid status releasedByName claimedByName }
        status { slug name }
        event { id uuid name assignedAmbassadorsCount confirmedAmbassadorsCount }
        recapsFiledCount
        recapEventUuid
        eventsCount
      }
    }
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

    def test_list_vs_detail_queryset_split(self):
        """The list path drops the heavy recaps prefetches in favor of count
        annotations; the detail path keeps the full recaps prefetch the Field
        Reports panel renders from."""
        from events.queries import RequestQueriesService

        detail = RequestQueriesService()  # list_mode defaults False
        listsvc = RequestQueriesService()
        listsvc.list_mode = True

        detail_pf = set(detail.get_queryset()._prefetch_related_lookups)
        list_pf = set(listsvc.get_queryset()._prefetch_related_lookups)

        # Detail keeps the full recap prefetches (Field Reports needs them).
        assert "event_set__recaps" in detail_pf
        assert "event_set__custom_recap" in detail_pf
        # List drops them — replaced by subquery annotations — but keeps the
        # small bounded prefetches the row chips still read from.
        assert "event_set__recaps" not in list_pf
        assert "event_set__custom_recap" not in list_pf
        assert "event_set__open_shifts" in list_pf
        assert "event_set__ambassadors_events" in list_pf

        # The three rollup annotations live ONLY on the list queryset.
        ann = {"_events_count_ann", "_recaps_filed_count_ann", "_recap_event_uuid_ann"}
        list_ann = set(listsvc.get_queryset().query.annotations)
        detail_ann = set(detail.get_queryset().query.annotations)
        assert ann <= list_ann, list_ann
        assert not (ann & detail_ann), detail_ann

    def test_list_queryset_is_constant_query_count(self):
        """The list path must not issue per-row recap queries: adding a second
        request (with its own events + recaps) must NOT increase the number of
        queries to load the page + read all three scalar annotations. This is
        the regression guard against re-introducing the per-event recap N+1
        that the subquery annotations replaced."""
        from events.queries import RequestQueriesService

        svc = RequestQueriesService()
        svc.list_mode = True

        def fetch():
            qs = svc.get_queryset().filter(tenant=self.tenant).order_by("id")
            rows = list(qs)  # 1 main query (annotations inline) + bounded prefetch
            return {
                str(r.uuid): (
                    int(r._recaps_filed_count_ann or 0),
                    int(r._events_count_ann or 0),
                    str(r._recap_event_uuid_ann) if r._recap_event_uuid_ann else None,
                )
                for r in rows
            }

        with CaptureQueriesContext(connection) as ctx1:
            data1 = fetch()
        # Seed row: 3 filed (2 legacy + 1 custom) across 2 events, recap on ev_a.
        assert data1[str(self.req.uuid)] == (3, 2, str(self.ev_a.uuid))
        n1 = len(ctx1.captured_queries)

        # Add a SECOND request with its own event + two recaps.
        req2 = em.Request.objects.create(
            name="WF Glendale", address="2 B St",
            request_type=self.req.request_type, tenant=self.tenant,
            created_by=self.sys,
        )
        ev2 = self.create_event(name="R2 Day 1", tenant=self.tenant, request=req2)
        rm.Recap.objects.create(name="r3", event=ev2, created_by=self.sys, updated_by=self.sys)
        rm.Recap.objects.create(name="r4", event=ev2, created_by=self.sys, updated_by=self.sys)

        with CaptureQueriesContext(connection) as ctx2:
            data2 = fetch()
        n2 = len(ctx2.captured_queries)

        # Correctness for the new row + unchanged seed row.
        assert data2[str(req2.uuid)] == (2, 1, str(ev2.uuid))
        assert data2[str(self.req.uuid)] == (3, 2, str(self.ev_a.uuid))
        # The point: query count did NOT grow with the extra request.
        assert n1 == n2, (n1, n2, [q["sql"] for q in ctx2.captured_queries])
