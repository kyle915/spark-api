"""/talent roster: phone-keyed walk-up identities are shown by phone and
market, never by their synthetic ``checkin-<phone>@walkup.spark`` email.

The roster asks for ``phone``, ``lastWorkedMarket`` and ``lastWorkedTenant``.
The last-worked pair is annotated onto the list only when selected (no
per-row queries), and a brand caller never learns another brand's name.
Runs the web app's AmbassadorsListQuery field set through the real client
schema (outage lesson).
"""
import uuid
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models

# AmbassadorsListQuery, field for field, plus the new roster fields.
QUERY = """
query AmbassadorsListQuery($filters: AmbassadorFiltersInput, $first: Int) {
  ambassadors(filters: $filters, first: $first) {
    edges {
      cursor
      node {
        id uuid rating address isActive isFavorited college inCollege headshotUrl
        phone lastWorkedMarket lastWorkedTenant
        user { firstName lastName email }
      }
    }
    pageInfo { hasNextPage hasPreviousPage startCursor endCursor }
    totalCount
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestTalentLastWorked(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Home Brand")
        self.other = self.create_tenant(name="Other Brand")
        uid = str(uuid.uuid4())[:8]
        self.admin = self.create_user(
            username=f"admin_{uid}@test.com", email=f"admin_{uid}@test.com",
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(self.admin, self.tenant)
        tx = event_models.State.objects.create(name="Texas", code="TX", created_by=self.sys)
        self.austin = event_models.Location.objects.create(
            name="Austin", code="AUS", zip="78701", state=tx, created_by=self.sys
        )

    def _walkup_ba(self, digits="3127719621"):
        u = self.create_user(
            username=f"checkin-{digits}@walkup.spark",
            email=f"checkin-{digits}@walkup.spark",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(u, self.tenant)
        return self.create_ambassador(u, is_active=True, phone=digits)

    def _shift(self, ba, tenant, days_ago, location=None):
        when = timezone.now() - timedelta(days=days_ago)
        event = self.create_event(
            name=f"Shift {days_ago}", tenant=tenant, date=when, start_time=when,
        )
        if location is not None:
            event_models.Event.objects.filter(id=event.id).update(location=location)
        AmbassadorEvent.objects.create(
            ambassador=ba, event=event, tenant=tenant, is_approved=True,
            created_by=self.sys,
        )

    async def _run(self, user, filters=None):
        result = await self._execute_mutation(
            QUERY,
            {"filters": {"tenantId": str(self.tenant.id), **(filters or {})}, "first": 50},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None, result.errors
        return [e["node"] for e in result.data["ambassadors"]["edges"]]

    @pytest.mark.asyncio
    async def test_last_worked_market_and_brand_for_staff(self):
        def seed():
            ba = self._walkup_ba()
            self._shift(ba, self.other, 10)
            self._shift(ba, self.tenant, 2, location=self.austin)
            self._shift(ba, self.other, -3)  # future: not "worked" yet
            return ba

        ba = await sync_to_async(seed)()
        rows = await self._run(self.admin)
        row = next(r for r in rows if r["uuid"] == str(ba.uuid))
        assert row["phone"] == "3127719621"
        assert row["lastWorkedMarket"] == "Austin, TX"
        assert row["lastWorkedTenant"] == "Home Brand"

    @pytest.mark.asyncio
    async def test_search_finds_a_walkup_by_phone(self):
        ba = await sync_to_async(self._walkup_ba)("5551234567")
        rows = await self._run(self.admin, {"search": "5551234567"})
        assert [r["uuid"] for r in rows] == [str(ba.uuid)]

    @pytest.mark.asyncio
    async def test_selection_adds_no_per_row_queries(self):
        def seed():
            for i in range(6):
                ba = self._walkup_ba(f"55500000{i:02d}")
                self._shift(ba, self.tenant, 1 + i, location=self.austin)

        await sync_to_async(seed)()

        def count(query_text):
            from asgiref.sync import async_to_sync

            with CaptureQueriesContext(connection) as ctx:
                async_to_sync(self._execute_mutation)(
                    query_text,
                    {"filters": {"tenantId": str(self.tenant.id)}, "first": 50},
                    self.endpoint_path,
                    user=self.admin,
                )
            return len(ctx.captured_queries)

        plain = QUERY.replace("phone lastWorkedMarket lastWorkedTenant", "phone")
        await sync_to_async(count)(plain)  # warm per-request caches
        without = await sync_to_async(count)(plain)
        with_fields = await sync_to_async(count)(QUERY)
        assert with_fields == without

    @pytest.mark.asyncio
    async def test_brand_caller_never_sees_another_brand(self):
        def seed():
            client = self.create_user(
                username="brand-client@test.com", email="brand-client@test.com",
                role=self.roles["client"],
            )
            self.create_tenanted_user(client, self.tenant)
            ba = self._walkup_ba()
            self._shift(ba, self.tenant, 9, location=self.austin)
            self._shift(ba, self.other, 1)  # more recent, but another brand
            return client, ba

        client, ba = await sync_to_async(seed)()
        result = await self._execute_mutation(
            QUERY, {"first": 50}, self.endpoint_path, user=client
        )
        assert result.errors is None, result.errors
        for node in (e["node"] for e in result.data["ambassadors"]["edges"]):
            assert node["lastWorkedTenant"] in (None, "Home Brand")
            if node["uuid"] == str(ba.uuid):
                assert node["lastWorkedMarket"] == "Austin, TX"
