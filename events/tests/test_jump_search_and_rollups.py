"""Tests for composite Jump search (``jumpSearch``) and server request rollups
(``tenantRequestsRollup``)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.models import Ambassador
from config.schema_client import schema_clients
from events import jump_search
from events import models as em
from events.tests.base import EventsGraphQLTestCase
from recaps import models as rm
from tenants.models import TenantedUser
from utils.graphql.mixins import resolve_id_to_int

JUMP_Q = """
query JumpSearch($q: String!, $tenantId: ID, $first: Int) {
  jumpSearch(q: $q, tenantId: $tenantId, first: $first) {
    requests {
      uuid
      name
      date
      retailerName
    }
    events {
      uuid
      name
      date
      address
      retailerName
    }
    stores {
      key
      storeKey
      name
      address
      city
      state
    }
    recaps {
      uuid
      kind
      name
      approved
      eventDate
    }
    ambassadors {
      uuid
      name
      email
    }
    clients {
      uuid
      name
      email
      tenantId
      tenantUuid
      tenantName
      slug
      checkinCode
    }
  }
}
"""

ROLLUP_Q = """
query Rollup($tenantId: ID, $startDate: Date, $endDate: Date, $dateField: String!) {
  tenantRequestsRollup(
    tenantId: $tenantId
    startDate: $startDate
    endDate: $endDate
    dateField: $dateField
  ) {
    totalCount
    byDay {
      date
      count
    }
    byRequestType {
      name
      count
    }
    topRetailers {
      name
      count
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestJumpSearchAndRequestRollups(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        jump_search.clear_jump_store_cache()
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()

        self.tenant = self.create_tenant(
            name="Sprouts Brand",
            slug="sprouts-brand",
            checkin_code="SP-TEST12",
        )
        self.other_tenant = self.create_tenant(name="Rival Brand")

        self.admin = self.create_user(
            username="jump-admin",
            email="ops@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.client_user = self.create_user(
            username="brand-client",
            email="buyer@sproutsbrand.com",
            role=self.roles["client"],
        )
        TenantedUser.objects.create(
            user=self.client_user,
            tenant=self.tenant,
            is_active=True,
        )

        ba_user = self.create_user(
            username="ba-sprouts",
            email="maya.sprouts@example.com",
            first_name="Maya",
            last_name="Sprouts",
            role=self.roles["ambassador"],
        )
        self.ambassador = Ambassador.objects.create(
            user=ba_user,
            created_by=self.sys,
            updated_by=self.sys,
        )

        self.client_contact = em.Client.objects.create(
            name="Sprouts Marketing Lead",
            email="lead@sproutsbrand.com",
            tenant=self.tenant,
            created_by=self.sys,
        )

        self.state = em.State.objects.create(
            name="Texas", code="TX", created_by=self.sys
        )
        self.location = em.Location.objects.create(
            name="Austin",
            code="AUS",
            zip="78704",
            state=self.state,
            created_by=self.sys,
        )
        self.retail_type = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.event_type = em.RequestType.objects.create(
            name="Event Activation", tenant=self.tenant, created_by=self.sys
        )
        self.approved_status = em.RequestStatus.objects.create(
            name="Approved", tenant=self.tenant, created_by=self.sys
        )

        self.retailer = em.Retailer.objects.create(
            name="Sprouts South Lamar",
            address="4006 S Lamar Blvd, Austin, TX 78704",
            store_contact="Mgr",
            location=self.location,
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.catalog_only_retailer = em.Retailer.objects.create(
            name="Sprouts Mueller Catalog",
            address="1201 Barbara Jordan Blvd, Austin, TX 78723",
            store_contact="Mgr",
            location=self.location,
            tenant=self.tenant,
            created_by=self.sys,
        )

        self.now = timezone.now().replace(hour=15, minute=0, second=0, microsecond=0)
        self.tomorrow = self.now + timedelta(days=1)
        self.in_three_days = self.now + timedelta(days=3)

        self.req1 = em.Request.objects.create(
            name="Sprouts South Lamar Demo",
            address=self.retailer.address,
            request_type=self.retail_type,
            tenant=self.tenant,
            created_by=self.sys,
            status=self.approved_status,
            retailer=self.retailer,
            location=self.location,
            state=self.state,
            date=self.tomorrow,
            coordinates=[30.2365, -97.7934],
        )
        self.req2 = em.Request.objects.create(
            name="Sprouts Festival Booth",
            address=self.retailer.address,
            request_type=self.event_type,
            tenant=self.tenant,
            created_by=self.sys,
            status=self.approved_status,
            retailer=self.retailer,
            location=self.location,
            state=self.state,
            date=self.in_three_days,
            coordinates=[30.2365, -97.7934],
        )
        # Soft-deleted request must be excluded from both search and rollups.
        em.Request.objects.create(
            name="Sprouts Deleted Demo",
            address=self.retailer.address,
            request_type=self.retail_type,
            tenant=self.tenant,
            created_by=self.sys,
            status=self.approved_status,
            retailer=self.retailer,
            date=self.tomorrow,
            deleted_at=self.now,
        )

        # Rival tenant data must never leak into brand client results.
        other_status = em.RequestStatus.objects.create(
            name="Approved", tenant=self.other_tenant, created_by=self.sys
        )
        other_rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.other_tenant, created_by=self.sys
        )
        self.other_req = em.Request.objects.create(
            name="Sprouts Rival Secret Demo",
            address="999 Rival Way, Dallas, TX 75201",
            request_type=other_rt,
            tenant=self.other_tenant,
            created_by=self.sys,
            status=other_status,
            date=self.tomorrow,
        )

        self.ev_approved = self.create_event(
            name="Sprouts Approved Shift",
            tenant=self.tenant,
            request=self.req1,
            retailer=self.retailer,
            date=self.tomorrow,
            address=self.retailer.address,
        )
        self.ev_draft = self.create_event(
            name="Sprouts Unapproved Shift",
            tenant=self.tenant,
            request=self.req2,
            retailer=self.retailer,
            date=self.in_three_days,
            address=self.retailer.address,
        )
        self.recap_approved = rm.Recap.objects.create(
            name="Approved Recap",
            event=self.ev_approved,
            approved=True,
            created_by=self.sys,
            updated_by=self.sys,
        )
        self.recap_draft = rm.Recap.objects.create(
            name="Draft Recap",
            event=self.ev_draft,
            approved=False,
            created_by=self.sys,
            updated_by=self.sys,
        )

    @pytest.mark.asyncio
    async def test_admin_jump_search_returns_all_six_buckets_and_client_link_fields(self):
        res = await self._execute_mutation(
            JUMP_Q,
            {"q": "Sprouts", "tenantId": str(self.tenant.id), "first": 6},
            user=self.admin,
        )
        assert res.errors is None, res.errors
        payload = res.data["jumpSearch"]

        req_names = {r["name"] for r in payload["requests"]}
        assert "Sprouts South Lamar Demo" in req_names
        assert "Sprouts Deleted Demo" not in req_names
        assert "Sprouts Rival Secret Demo" not in req_names

        assert len(payload["events"]) >= 2

        stores_by_name = {s["name"]: s for s in payload["stores"]}
        assert "Sprouts South Lamar" in stores_by_name
        assert stores_by_name["Sprouts South Lamar"]["storeKey"] == f"s{self.req1.pk}"
        assert "Sprouts Mueller Catalog" in stores_by_name
        assert stores_by_name["Sprouts Mueller Catalog"]["storeKey"] is None

        recap_uuids = {r["uuid"] for r in payload["recaps"]}
        assert str(self.recap_approved.uuid) in recap_uuids
        assert str(self.recap_draft.uuid) in recap_uuids

        assert any(a["uuid"] == str(self.ambassador.uuid) for a in payload["ambassadors"])
        matched_client = next(
            (c for c in payload["clients"] if c["uuid"] == str(self.client_contact.uuid)),
            None,
        )
        assert matched_client is not None
        assert resolve_id_to_int(matched_client["tenantId"]) == self.tenant.id
        assert matched_client["tenantUuid"] == str(self.tenant.uuid)
        assert matched_client["slug"] == "sprouts-brand"
        assert matched_client["checkinCode"] == "SP-TEST12"

    @pytest.mark.asyncio
    async def test_store_directory_cached_across_keystrokes_and_invalidated_on_write(self):
        jump_search.clear_jump_store_cache()
        real_build = jump_search._build_tenant_store_directory
        with patch.object(
            jump_search,
            "_build_tenant_store_directory",
            wraps=real_build,
        ) as spy:
            for term in ("Sp", "Spr", "Sprout"):
                res = await self._execute_mutation(
                    JUMP_Q,
                    {"q": term, "tenantId": str(self.tenant.id), "first": 6},
                    user=self.admin,
                )
                assert res.errors is None, res.errors
                assert any(
                    s["storeKey"] == f"s{self.req1.pk}"
                    for s in res.data["jumpSearch"]["stores"]
                )
            assert spy.call_count == 1

            # Creating a new request at a new address bumps the watermark and rebuilds once.
            new_req = await sync_to_async(em.Request.objects.create)(
                name="Sprouts Burnet Road Demo",
                address="6920 Burnet Rd, Austin, TX 78757",
                retailer_name="Sprouts Burnet Rd",
                request_type=self.retail_type,
                tenant=self.tenant,
                created_by=self.sys,
                status=self.approved_status,
                location=self.location,
                state=self.state,
                date=self.tomorrow,
            )
            res_after = await self._execute_mutation(
                JUMP_Q,
                {"q": "Burnet", "tenantId": str(self.tenant.id), "first": 6},
                user=self.admin,
            )
            assert res_after.errors is None, res_after.errors
            assert spy.call_count == 2
            assert any(
                s["storeKey"] == f"s{new_req.pk}"
                for s in res_after.data["jumpSearch"]["stores"]
            )

    @pytest.mark.asyncio
    async def test_client_jump_search_pins_tenant_hides_draft_recaps_and_ba_pii(self):
        # Attempt to pass rival tenantId — client must still be pinned to self.tenant.
        res = await self._execute_mutation(
            JUMP_Q,
            {"q": "Sprouts", "tenantId": str(self.other_tenant.id), "first": 6},
            user=self.client_user,
        )
        assert res.errors is None, res.errors
        payload = res.data["jumpSearch"]

        req_names = {r["name"] for r in payload["requests"]}
        assert "Sprouts South Lamar Demo" in req_names
        assert "Sprouts Rival Secret Demo" not in req_names

        recap_uuids = {r["uuid"] for r in payload["recaps"]}
        assert str(self.recap_approved.uuid) in recap_uuids
        assert str(self.recap_draft.uuid) not in recap_uuids

        assert payload["ambassadors"] == []
        assert payload["clients"] == []
        assert any(s["storeKey"] == f"s{self.req1.pk}" for s in payload["stores"])

    @pytest.mark.asyncio
    async def test_tenant_requests_rollup_scheduled_and_created_modes(self):
        start_iso = self.now.date().isoformat()
        end_iso = (self.now.date() + timedelta(days=7)).isoformat()

        sched = await self._execute_mutation(
            ROLLUP_Q,
            {
                "tenantId": str(self.other_tenant.id),
                "startDate": start_iso,
                "endDate": end_iso,
                "dateField": "date",
            },
            user=self.client_user,
        )
        assert sched.errors is None, sched.errors
        sched_data = sched.data["tenantRequestsRollup"]
        assert sched_data["totalCount"] == 2
        assert sum(b["count"] for b in sched_data["byDay"]) == 2
        assert sched_data["topRetailers"] == [
            {"name": "Sprouts South Lamar", "count": 2}
        ]

        created = await self._execute_mutation(
            ROLLUP_Q,
            {
                "tenantId": str(self.tenant.id),
                "startDate": start_iso,
                "endDate": end_iso,
                "dateField": "created_at",
            },
            user=self.admin,
        )
        assert created.errors is None, created.errors
        created_data = created.data["tenantRequestsRollup"]
        assert created_data["totalCount"] == 2
        by_type = {b["name"]: b["count"] for b in created_data["byRequestType"]}
        assert by_type == {"Retail Sampling": 1, "Event Activation": 1}
