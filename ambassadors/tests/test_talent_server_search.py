"""/talent search runs on the server across every BA in scope.

The roster asked for 200 BAs, the API caps a page at 50, and the search box
filtered only the loaded page — so a BA past the first 50 couldn't be found.
The box is now ``filters.talentSearch`` and the page pages with "Load more".
Runs the web app's TalentRosterQuery, field for field, through the real
client schema (outage lesson).
"""
import uuid
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ambassadors.models import Ambassador
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

# TalentRosterQuery, field for field.
QUERY = """
query TalentRosterQuery($filters: AmbassadorFiltersInput, $first: Int, $after: String) {
  ambassadors(filters: $filters, first: $first, after: $after) {
    edges {
      node {
        id uuid rating address isActive college inCollege headshotUrl
        phone lastWorkedMarket lastWorkedTenant
        user { firstName lastName email }
      }
    }
    pageInfo { hasNextPage endCursor }
    totalCount
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestTalentServerSearch(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Roster Brand")
        self.other = self.create_tenant(name="Other Roster Brand")
        uid = str(uuid.uuid4())[:8]
        self.admin = self.create_user(
            username=f"admin_{uid}@test.com", email=f"admin_{uid}@test.com",
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(self.admin, self.tenant)

    def _ba(self, *, first="", last="", email=None, phone=None, tenant=None, age_days=0):
        uid = str(uuid.uuid4())[:8]
        email = email or f"ba_{uid}@example.com"
        u = self.create_user(
            username=email, email=email, role=self.roles["ambassador"],
            first_name=first, last_name=last,
        )
        self.create_tenanted_user(u, tenant or self.tenant)
        ba = self.create_ambassador(u, is_active=True, phone=phone)
        Ambassador.objects.filter(id=ba.id).update(
            created_at=timezone.now() - timedelta(days=age_days)
        )
        return ba

    async def _run(self, search=None, *, user=None, first=50, after=None, tenant=None):
        filters = {"tenantId": str((tenant or self.tenant).id), "isActive": True}
        if search is not None:
            filters["talentSearch"] = search
        result = await self._execute_mutation(
            QUERY, {"filters": filters, "first": first, "after": after},
            self.endpoint_path, user=user or self.admin,
        )
        assert result.errors is None, result.errors
        return result.data["ambassadors"]

    @pytest.mark.asyncio
    async def test_finds_a_ba_beyond_the_first_page(self):
        def seed():
            target = self._ba(first="Zelda", last="Quill", age_days=400)
            for i in range(55):
                self._ba(first=f"Filler{i}", last="Person", age_days=i)
            return target

        target = await sync_to_async(seed)()
        page = await self._run()
        assert len(page["edges"]) == 50 and page["pageInfo"]["hasNextPage"]
        assert str(target.uuid) not in {e["node"]["uuid"] for e in page["edges"]}

        hit = await self._run("zelda quill")
        assert [e["node"]["uuid"] for e in hit["edges"]] == [str(target.uuid)]
        assert hit["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_load_more_pages_without_repeats(self):
        def seed():
            # Same created_at for all: ordering must still be stable.
            ids = [self._ba(first=f"Same{i}") for i in range(60)]
            Ambassador.objects.filter(id__in=[b.id for b in ids]).update(
                created_at=timezone.now()
            )

        await sync_to_async(seed)()
        first = await self._run(first=50)
        second = await self._run(first=50, after=first["pageInfo"]["endCursor"])
        uuids = [e["node"]["uuid"] for e in first["edges"] + second["edges"]]
        assert len(uuids) == len(set(uuids)) == first["totalCount"]
        assert second["pageInfo"]["hasNextPage"] is False

    @pytest.mark.asyncio
    async def test_phone_in_any_format_including_walkup_identities(self):
        def seed():
            stored = self._ba(first="Pat", phone="(312) 771-9621")
            walkup = self._ba(email="checkin-5551234567@walkup.spark")
            return stored, walkup

        stored, walkup = await sync_to_async(seed)()
        for q in ("312-771", "3127719621", "+1 312 771 9621", "(312) 771"):
            hit = await self._run(q)
            assert [e["node"]["uuid"] for e in hit["edges"]] == [str(stored.uuid)], q
        hit = await self._run("555 123 4567")
        assert [e["node"]["uuid"] for e in hit["edges"]] == [str(walkup.uuid)]

    @pytest.mark.asyncio
    async def test_never_matches_the_synthetic_walkup_address(self):
        await sync_to_async(self._ba)(email="checkin-5551234567@walkup.spark")
        for q in ("walkup", "checkin", "walkup.spark", "checkin-555"):
            hit = await self._run(q)
            assert hit["totalCount"] == 0, q

    @pytest.mark.asyncio
    async def test_real_email_matches(self):
        ba = await sync_to_async(self._ba)(email="dana.reyes@example.com")
        hit = await self._run("dana.reyes@")
        assert [e["node"]["uuid"] for e in hit["edges"]] == [str(ba.uuid)]

    @pytest.mark.asyncio
    async def test_brand_user_searches_only_their_own_bas(self):
        def seed():
            client = self.create_user(
                username="roster-client@test.com", email="roster-client@test.com",
                role=self.roles["client"],
            )
            self.create_tenanted_user(client, self.tenant)
            mine = self._ba(first="Shared", last="Name")
            self._ba(first="Shared", last="Name", tenant=self.other)
            return client, mine

        client, mine = await sync_to_async(seed)()

        async def ask(tenant):
            return await self._execute_mutation(
                QUERY,
                {"filters": {"tenantId": str(tenant.id), "talentSearch": "shared"}, "first": 50},
                self.endpoint_path, user=client,
            )

        own = await ask(self.tenant)
        assert own.errors is None, own.errors
        assert {e["node"]["uuid"] for e in own.data["ambassadors"]["edges"]} == {str(mine.uuid)}
        # Naming the other brand is refused outright.
        foreign = await ask(self.other)
        assert foreign.errors and foreign.data is None

    @pytest.mark.asyncio
    async def test_search_adds_no_per_row_queries(self):
        await sync_to_async(lambda: [self._ba(first=f"Count{i}", phone=f"55500000{i:02d}") for i in range(8)])()

        def count(search):
            filters = {"tenantId": str(self.tenant.id), "isActive": True}
            if search:
                filters["talentSearch"] = search
            with CaptureQueriesContext(connection) as ctx:
                async_to_sync(self._execute_mutation)(
                    QUERY, {"filters": filters, "first": 50}, self.endpoint_path, user=self.admin
                )
            return len(ctx.captured_queries)

        await sync_to_async(count)(None)  # warm per-request caches
        plain = await sync_to_async(count)(None)
        assert await sync_to_async(count)("count") == plain
        assert await sync_to_async(count)("555-000") == plain
