"""Sidebar badge scalars + skinny Account Map pins.

Guards the perf contract: admin chrome must not download 2,000 fat
Request rows just to count tracker / approvals / recaps-due, and Account
Map must not prefetch Master Tracker products / open shifts / recaps.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from events import models as em
from events.admin_payloads import (
    compute_sidebar_request_counts,
    list_account_map_pins,
    scoped_requests,
)
from events.tests.base import EventsGraphQLTestCase
from recaps import models as rm


COUNTS_Q = """
query Counts($tenantId: ID) {
  sidebarRequestCounts(tenantId: $tenantId) {
    tracker
    approvals
    approvalsSlaBreach
    upcoming
    done30d
    recapsDue
  }
  sidebarAlertCandidates(tenantId: $tenantId) {
    id
    createdAt
    updatedAt
    statusSlug
  }
  pendingWalkupCount(tenantId: $tenantId)
}
"""

MAP_Q = """
query Map($tenantId: ID) {
  accountMapPins(tenantId: $tenantId, first: 2000) {
    id
    name
    address
    lat
    lng
    statusSlug
    date
    retailerName
    locationName
    stateCode
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestSidebarAndAccountMapPayloads(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Payload Tenant")
        self.admin = self.create_user(
            username="payload-admin",
            email="admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.pending = em.RequestStatus.objects.create(
            name="Pending", tenant=self.tenant, created_by=self.sys
        )
        self.approved = em.RequestStatus.objects.create(
            name="Approved", tenant=self.tenant, created_by=self.sys
        )
        self.done = em.RequestStatus.objects.create(
            name="Done", tenant=self.tenant, created_by=self.sys
        )
        self.state = em.State.objects.create(
            name="Illinois", code="IL", created_by=self.sys
        )
        self.location = em.Location.objects.create(
            name="Chicago",
            code="CHI",
            zip="60601",
            state=self.state,
            created_by=self.sys,
        )
        self.retailer = em.Retailer.objects.create(
            name="Binny's Lincoln Park",
            address="1720 N Marcey St, Chicago, IL 60614",
            store_contact="Mgr",
            location=self.location,
            tenant=self.tenant,
            created_by=self.sys,
        )
        now = timezone.now()
        yesterday = now - timedelta(days=1)
        last_week = now - timedelta(days=7)

        self.due = em.Request.objects.create(
            name="Due recap",
            address=self.retailer.address,
            request_type=self.rt,
            tenant=self.tenant,
            created_by=self.sys,
            status=self.approved,
            retailer=self.retailer,
            location=self.location,
            state=self.state,
            date=yesterday,
            coordinates=[41.9103, -87.6533],
        )
        self.create_event(
            name="Yesterday shift",
            tenant=self.tenant,
            address=self.retailer.address,
            request=self.due,
            date=yesterday,
        )
        # Empty clock-out stub — must NOT clear recapsDue.
        rm.Recap.objects.create(
            name="stub",
            event=self.due.event_set.first(),
            created_by=self.sys,
            updated_by=self.sys,
        )

        self.pending_req = em.Request.objects.create(
            name="Needs approval",
            address="1 Pending St",
            request_type=self.rt,
            tenant=self.tenant,
            created_by=self.sys,
            status=self.pending,
            date=now + timedelta(days=3),
            coordinates=[0, 0],
            created_at=now - timedelta(hours=80),
        )
        # created_at is auto_now_add — bump it for the SLA window.
        em.Request.objects.filter(pk=self.pending_req.pk).update(
            created_at=now - timedelta(hours=80)
        )

        self.done_req = em.Request.objects.create(
            name="Finished",
            address="2 Done St",
            request_type=self.rt,
            tenant=self.tenant,
            created_by=self.sys,
            status=self.done,
            date=last_week,
            coordinates=[40.0, -90.0],
        )

        self.future = em.Request.objects.create(
            name="Next month",
            address="3 Future St",
            request_type=self.rt,
            tenant=self.tenant,
            created_by=self.sys,
            status=self.approved,
            date=now + timedelta(days=40),
            coordinates=[34.05, -118.24],
        )
        self.create_event(
            name="Future shift",
            tenant=self.tenant,
            request=self.future,
            date=now + timedelta(days=40),
        )

    def test_compute_counts_match_sidebar_rules(self):
        data = compute_sidebar_request_counts(
            scoped_requests(self.tenant.id)
        )
        # pending + approved-due + approved-future (done is excluded)
        assert data.tracker == 3
        assert data.approvals == 1
        assert data.approvals_sla_breach == 1
        assert data.recaps_due == 1
        assert data.done_30d == 1
        assert data.upcoming == 0  # 3d pending is not approved/scheduled

    def test_account_map_skips_zero_zero_and_keeps_plottable(self):
        pins = list_account_map_pins(scoped_requests(self.tenant.id))
        coords = {(round(p.lat, 4), round(p.lng, 4)) for p in pins}
        assert (41.9103, -87.6533) in coords
        assert (0.0, 0.0) not in coords
        binnys = next(p for p in pins if "Binny" in p.name)
        assert binnys.retailer_name == "Binny's Lincoln Park"
        assert binnys.state_code == "IL"
        assert binnys.location_name == "Chicago"
        assert "product" not in binnys.__dataclass_fields__

    def test_account_map_sql_skips_tracker_prefetches(self):
        qs = scoped_requests(self.tenant.id)
        with CaptureQueriesContext(connection) as ctx:
            list(list_account_map_pins(qs))
        sql = " ".join(q["sql"].lower() for q in ctx.captured_queries)
        assert "request_product" not in sql
        assert "open_shift" not in sql

    @pytest.mark.asyncio
    async def test_graphql_scalars_and_pins(self):
        counts = await self._execute_mutation(
            COUNTS_Q,
            {"tenantId": str(self.tenant.id)},
            user=self.admin,
        )
        assert counts.errors is None, counts.errors
        body = counts.data["sidebarRequestCounts"]
        assert body["tracker"] == 3
        assert body["approvals"] == 1
        assert body["approvalsSlaBreach"] == 1
        assert body["recapsDue"] == 1
        assert counts.data["pendingWalkupCount"] == 0

        mapped = await self._execute_mutation(
            MAP_Q,
            {"tenantId": str(self.tenant.id)},
            user=self.admin,
        )
        assert mapped.errors is None, mapped.errors
        pins = mapped.data["accountMapPins"]
        assert any(p["name"] == "Binny's Lincoln Park" for p in pins)
        assert all(not (p["lat"] == 0 and p["lng"] == 0) for p in pins)
        assert "products" not in pins[0]
        assert "openShifts" not in pins[0]
