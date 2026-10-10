"""Torch client logins can run their own plans; other brands can't touch them."""

import base64

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import MultipleObjectsReturned

from events import models as em
from events.field_marketing import (
    FIELD_MARKETING_SKU_NAMES,
    FieldMarketingError,
    _active_tenant_for_user,
    build_board,
    delete_event,
    log_results,
    plan_event,
    submit_event,
    update_event,
)
from events.tests.base import EventsGraphQLTestCase
from tenants.models import TenantedUser

PLAN = """
mutation PlanFieldMarketing($input: PlanFieldMarketingInput!) {
  planFieldMarketing(input: $input) { success message event { id status requestCode } }
}
"""

BOARD = """
query FieldMarketingBoard($tenantId: ID) {
  fieldMarketing(tenantId: $tenantId, includeDeleted: false) { available skus events { name } }
}
"""


@pytest.mark.django_db(transaction=True)
class TestFieldMarketingClientAccess(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.torch = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.other = self.create_tenant(name="Other Brand", slug="other-brand")
        line = em.ProductType.objects.create(
            tenant=self.torch, name="Field marketing", created_by=self.sys
        )
        for name in FIELD_MARKETING_SKU_NAMES:
            em.Product.objects.create(
                tenant=self.torch, product_type=line, name=name, created_by=self.sys
            )
        em.RequestType.objects.create(
            tenant=self.torch, name="Event Activation", created_by=self.sys
        )
        self.rmm = self.create_user(
            username="rmm", email="rmm@torch.example", role=self.roles["client"]
        )
        self.create_tenanted_user(self.rmm, self.torch)
        self.outsider = self.create_user(
            username="outsider", email="outsider@other.example", role=self.roles["client"]
        )
        self.create_tenanted_user(self.outsider, self.other)

    def _payload(self, **overrides):
        payload = {
            "market": "houston",
            "activity": "guerilla",
            "name": "Montrose night market",
            "starts_on": "2026-11-21",
            "address": "1000 Westheimer Rd, Houston, TX",
            "sampling_format": "full_can",
            "sku_names": ["Black Cherry 10mg"],
            "needs_field_support": True,
            "ambassador_count": 2,
            "start_time": "18:00",
            "end_time": "22:00",
        }
        payload.update(overrides)
        return payload

    def _run_full_lifecycle(self, user):
        tid = self.torch.id
        event = plan_event(user=user, payload=self._payload(), submit=False, tenant_id=tid)
        assert event.tenant_id == tid
        assert event.created_by_id == user.id

        event = update_event(
            user=user,
            event_id=str(event.uuid),
            payload=self._payload(name="Montrose night market (moved)", starts_on="2026-12-05"),
            tenant_id=tid,
        )
        assert event.name == "Montrose night market (moved)"

        event = submit_event(user=user, event_id=str(event.uuid), tenant_id=tid)
        event.refresh_from_db()
        assert event.status == em.FieldMarketingEvent.STATUS_SUBMITTED
        assert event.request is not None

        seeding = plan_event(
            user=user,
            payload=self._payload(
                activity="product_seeding", name="Heights bar seeding", sampling_format=""
            ),
            submit=True,
            tenant_id=tid,
        )
        seeding.refresh_from_db()
        assert seeding.status == em.FieldMarketingEvent.STATUS_SUBMITTED
        assert seeding.request_id is None
        logged = log_results(
            user=user, event_id=str(seeding.uuid), payload={"logged_cases": 6}, tenant_id=tid
        )
        assert logged.logged_cases == 6

        draft = plan_event(user=user, payload=self._payload(name="Scrap me"), submit=False, tenant_id=tid)
        outcome = delete_event(user=user, event_id=str(draft.uuid), tenant_id=tid)
        assert outcome.event.deleted_at is not None
        return event

    def test_client_runs_own_brand_plans_end_to_end(self):
        self._run_full_lifecycle(self.rmm)

    def test_client_with_duplicate_membership_rows_still_plans(self):
        """Some (user, tenant) pairs are duplicated in prod; .get() used to crash."""
        self.create_tenanted_user(self.rmm, self.torch)
        assert TenantedUser.objects.filter(user=self.rmm, tenant=self.torch).count() == 2

        assert _active_tenant_for_user(self.rmm, self.torch.id) == self.torch
        assert _active_tenant_for_user(self.rmm) == self.torch
        self._run_full_lifecycle(self.rmm)

    def test_shared_tenant_lookup_tolerates_duplicate_rows(self):
        self.create_tenanted_user(self.rmm, self.torch)
        assert self.rmm.tenant == self.torch
        assert self.rmm.get_tenant(tenant_id=self.torch.id) == self.torch
        assert self.rmm.get_tenant(tenant_uuid=str(self.torch.uuid)) == self.torch

        self.create_tenanted_user(self.rmm, self.other)
        with pytest.raises(MultipleObjectsReturned):
            _ = self.rmm.tenant
        assert self.rmm.get_tenant(tenant_id=self.other.id) == self.other

    def test_other_brand_client_is_blocked_even_naming_torch(self):
        event = plan_event(user=self.rmm, payload=self._payload(), submit=False, tenant_id=self.torch.id)
        eid = str(event.uuid)
        tid = self.torch.id
        attempts = [
            lambda: plan_event(user=self.outsider, payload=self._payload(), submit=False, tenant_id=tid),
            lambda: update_event(user=self.outsider, event_id=eid, payload=self._payload(), tenant_id=tid),
            lambda: submit_event(user=self.outsider, event_id=eid, tenant_id=tid),
            lambda: log_results(user=self.outsider, event_id=eid, payload={"logged_emails": 1}, tenant_id=tid),
            lambda: delete_event(user=self.outsider, event_id=eid, tenant_id=tid),
        ]
        for attempt in attempts:
            with pytest.raises(FieldMarketingError, match="Switch to Torch"):
                attempt()
        event.refresh_from_db()
        assert event.status == em.FieldMarketingEvent.STATUS_PLANNED
        assert event.deleted_at is None
        assert event.logged_emails is None

    def test_inactive_membership_is_blocked(self):
        TenantedUser.objects.filter(user=self.rmm).update(is_active=False)
        with pytest.raises(FieldMarketingError, match="Switch to Torch"):
            plan_event(user=self.rmm, payload=self._payload(), submit=False, tenant_id=self.torch.id)

    def test_ambassador_login_is_told_why(self):
        ba = self.create_user(username="ba", email="ba@example.com", role=self.roles["ambassador"])
        self.create_tenanted_user(ba, self.torch)
        with pytest.raises(FieldMarketingError, match="brand team"):
            plan_event(user=ba, payload=self._payload(), submit=False, tenant_id=self.torch.id)

    def test_client_board_hides_nothing_needed_to_plan(self):
        board = build_board(self.torch, None)
        assert board["available"] is True
        assert board["skus"] == list(FIELD_MARKETING_SKU_NAMES)

    @pytest.mark.asyncio
    async def test_clients_schema_add_to_plan_with_duplicate_membership(self):
        """The exact request the Plans page sends, as a Torch client login."""
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        await sync_to_async(self.create_tenanted_user)(self.rmm, self.torch)
        relay_id = base64.b64encode(f"TenantType:{self.torch.id}".encode()).decode()

        board = await self._execute_query_authenticated(
            BOARD, {"tenantId": relay_id}, user=self.rmm
        )
        assert board.errors is None, board.errors
        assert board.data["fieldMarketing"]["available"] is True

        for activity, extra in (
            ("guerilla", {"samplingFormat": "full_can", "skuNames": ["Black Cherry 10mg"]}),
            ("product_seeding", {"samplingFormat": "", "skuNames": ["Nonactive"]}),
        ):
            result = await self._execute_mutation_authenticated(
                PLAN,
                {
                    "input": {
                        "market": "houston",
                        "activity": activity,
                        "name": f"Future {activity}",
                        "startsOn": "2027-01-16",
                        "days": 1,
                        "address": "1000 Westheimer Rd, Houston, TX",
                        "submit": False,
                        "tenantId": relay_id,
                        **extra,
                    }
                },
                user=self.rmm,
            )
            assert result.errors is None, result.errors
            assert result.data["planFieldMarketing"]["success"] is True

        blocked = await self._execute_mutation_authenticated(
            PLAN,
            {
                "input": {
                    "market": "houston",
                    "activity": "guerilla",
                    "name": "Not my brand",
                    "startsOn": "2027-01-16",
                    "samplingFormat": "full_can",
                    "skuNames": ["Black Cherry 10mg"],
                    "submit": False,
                    "tenantId": relay_id,
                }
            },
            user=self.outsider,
        )
        assert blocked.errors and "Switch to Torch" in blocked.errors[0].message
