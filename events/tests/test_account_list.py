"""Uploaded account lists, saved per tenant (Account Map).

Covers: rows are stored on the server and read back by anyone allowed; the
same store is not added twice; a brand user is held to their own brand
(another brand's id is refused on the query and on both mutations) and an
ambassador gets nothing; positions come from the file when it has them,
otherwise from one address lookup per distinct address; nothing is deleted.

The GraphQL operations are, field for field, the ones spark-front-client sends.
No test here reaches Google: ``geocoding.lookup_address`` is patched.
"""

from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from events import account_list, geocoding
from events import models as em
from events.map_points import address_hash
from events.tests.base import EventsGraphQLTestCase

LOOKUP_PATH = "events.account_list.geocoding.lookup_address"

# src/api/queries/AccountListQuery.ts
FE_LIST_QUERY = """
query AccountListQuery($tenantId: ID!) {
  accountListEntries(tenantId: $tenantId) {
    uuid
    name
    address
    city
    state
    zip
    chain
    notes
    lat
    lng
    positionStatus
  }
}
"""

# src/api/mutations/SaveAccountListMutation.ts
FE_SAVE_MUTATION = """
mutation SaveAccountListMutation($tenantId: ID!, $accounts: [AccountListEntryInput!]!) {
  saveAccountList(tenantId: $tenantId, accounts: $accounts) {
    added
    duplicates
    skipped
    total
    awaitingPosition
  }
}
"""

# src/api/mutations/PlaceAccountListEntriesMutation.ts
FE_PLACE_MUTATION = """
mutation PlaceAccountListEntriesMutation($tenantId: ID!, $limit: Int) {
  placeAccountListEntries(tenantId: $tenantId, limit: $limit) {
    placed
    notFound
    remaining
    lookupAvailable
  }
}
"""

CHINO = {
    "name": "Albertsons 524",
    "address": "12013 Central Ave",
    "city": "Chino",
    "state": "CA",
    "zip": "91710",
    "chain": "Albertsons",
}
NYC = {
    "name": "7-Eleven #3201",
    "address": "1608 Broadway St",
    "city": "New York",
    "state": "NY",
    "zip": "10019",
    "chain": "7-Eleven",
}


def _row(**kwargs):
    return account_list.AccountListEntryInput(**kwargs)


@pytest.mark.django_db(transaction=True)
class TestAccountList(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="List Tenant")
        self.other = self.create_tenant(name="Somebody Else's Brand")
        self.admin = self.create_user(
            username="list-admin",
            email="list-admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.client_user = self.create_user(
            username="list-client",
            email="list-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

    # ── saving ────────────────────────────────────────────────────────

    def test_rows_are_saved_once_and_the_same_store_is_not_added_twice(self):
        first = account_list.save_entries(
            self.tenant.id, [_row(**CHINO), _row(**NYC)], self.admin
        )
        assert (first.added, first.duplicates, first.total) == (2, 0, 2)
        assert first.awaiting_position == 2

        # Same store, different spacing / case / punctuation, plus one new.
        again = account_list.save_entries(
            self.tenant.id,
            [
                _row(**{**CHINO, "name": "ALBERTSONS  524", "address": "12013 Central Ave."}),
                _row(name="Total Wine", address="1730 E Warner Rd", city="Tempe", state="AZ"),
                _row(name="   "),
            ],
            self.admin,
        )
        assert (again.added, again.duplicates, again.skipped, again.total) == (1, 1, 1, 3)
        assert em.AccountListEntry.objects.filter(tenant=self.tenant).count() == 3
        # The first upload's row is untouched by the second.
        kept = em.AccountListEntry.objects.get(tenant=self.tenant, chain="Albertsons")
        assert kept.name == "Albertsons 524"
        assert kept.created_by_id == self.admin.id

    def test_the_same_store_on_two_brands_lists_is_two_rows(self):
        account_list.save_entries(self.tenant.id, [_row(**CHINO)], self.admin)
        result = account_list.save_entries(self.other.id, [_row(**CHINO)], self.admin)
        assert result.added == 1
        assert em.AccountListEntry.objects.count() == 2

    def test_position_status_at_save(self):
        account_list.save_entries(
            self.tenant.id,
            [
                _row(**CHINO),
                _row(name="Has a pin", address="1 Pin Rd", lat=33.5, lng=-112.1),
                _row(name="Zero zero", address="2 Null Island Way", lat=0, lng=0),
                _row(name="Out of range", address="3 Nowhere St", lat=123.0, lng=-112.1),
                _row(name="No address at all"),
                _row(name="City only", city="Phoenix", state="AZ"),
            ],
            self.admin,
        )
        status = dict(
            em.AccountListEntry.objects.values_list("name", "position_status")
        )
        assert status == {
            "Albertsons 524": "pending",
            "Has a pin": "provided",
            "Zero zero": "pending",
            "Out of range": "pending",
            "No address at all": "no_address",
            "City only": "no_address",
        }
        pinned = em.AccountListEntry.objects.get(name="Has a pin")
        assert (pinned.lat, pinned.lng) == (33.5, -112.1)
        assert em.AccountListEntry.objects.get(name="Zero zero").lat is None

    def test_one_upload_is_capped(self):
        rows = [_row(name=f"Store {i}") for i in range(account_list.MAX_ROWS_PER_SAVE + 1)]
        with pytest.raises(GraphQLError):
            account_list.save_entries(self.tenant.id, rows, self.admin)
        assert not em.AccountListEntry.objects.exists()

    def test_an_unknown_tenant_is_refused(self):
        with pytest.raises(GraphQLError):
            account_list.save_entries(999_999_999, [_row(**CHINO)], self.admin)

    # ── placing on the map ────────────────────────────────────────────

    def test_one_lookup_per_distinct_address_and_not_found_is_remembered(self):
        account_list.save_entries(
            self.tenant.id,
            [
                _row(**CHINO),
                _row(**{**CHINO, "name": "Albertsons Pharmacy 524"}),
                _row(**NYC),
            ],
            self.admin,
        )

        def fake(address):
            if "Chino" in address:
                return geocoding.OK, (34.0122, -117.6889), "found"
            return geocoding.NOT_FOUND, None, "no results"

        with patch(LOOKUP_PATH, side_effect=fake) as lookup:
            result = account_list.place_entries(self.tenant.id)
            assert lookup.call_count == 2
            assert lookup.call_args_list[0].args[0] == "12013 Central Ave, Chino, CA 91710"
            assert (result.placed, result.not_found, result.remaining) == (2, 1, 0)
            assert result.lookup_available is True
            # Nothing left to ask about: a second run costs no lookups.
            account_list.place_entries(self.tenant.id)
            assert lookup.call_count == 2

        rows = {r.name: r for r in em.AccountListEntry.objects.all()}
        assert rows["Albertsons 524"].position_status == "ok"
        assert (rows["Albertsons Pharmacy 524"].lat, rows["Albertsons Pharmacy 524"].lng) == (
            34.0122,
            -117.6889,
        )
        assert rows["7-Eleven #3201"].position_status == "not_found"
        assert rows["7-Eleven #3201"].lat is None

    def test_a_failed_lookup_stores_nothing_and_the_run_stops(self):
        account_list.save_entries(
            self.tenant.id,
            [_row(name=f"Store {i}", address=f"{i + 1} Elm St", city="Mesa", state="AZ") for i in range(6)],
            self.admin,
        )
        with patch(LOOKUP_PATH, return_value=(geocoding.ERROR, None, "HTTP 500")) as lookup:
            result = account_list.place_entries(self.tenant.id)
        assert lookup.call_count == account_list.MAX_CONSECUTIVE_FAILURES
        assert (result.placed, result.not_found, result.remaining) == (0, 0, 6)
        assert result.lookup_available is False
        assert set(
            em.AccountListEntry.objects.values_list("position_status", flat=True)
        ) == {"pending"}

    def test_no_key_stops_at_once(self):
        account_list.save_entries(self.tenant.id, [_row(**CHINO), _row(**NYC)], self.admin)
        with patch(LOOKUP_PATH, return_value=(geocoding.NO_KEY, None, "no key")) as lookup:
            result = account_list.place_entries(self.tenant.id)
        assert lookup.call_count == 1
        assert result.lookup_available is False
        assert result.remaining == 2

    def test_an_address_already_looked_up_for_an_event_is_reused(self):
        account_list.save_entries(self.tenant.id, [_row(**CHINO)], self.admin)
        ev = self.create_event(name="shift", tenant=self.other, address="x")
        em.EventMapPoint.objects.create(
            event=ev,
            lat=34.0122,
            lng=-117.6889,
            address_hash=address_hash("12013 Central Ave, Chino, CA 91710"),
            status=em.EventMapPoint.STATUS_OK,
        )
        with patch(LOOKUP_PATH) as lookup:
            result = account_list.place_entries(self.tenant.id)
        lookup.assert_not_called()
        assert (result.placed, result.remaining) == (1, 0)

    def test_placing_one_brand_leaves_another_brands_rows_alone(self):
        account_list.save_entries(self.tenant.id, [_row(**CHINO)], self.admin)
        account_list.save_entries(self.other.id, [_row(**NYC)], self.admin)
        with patch(LOOKUP_PATH, return_value=(geocoding.OK, (34.0, -117.6), "found")):
            account_list.place_entries(self.tenant.id)
        assert em.AccountListEntry.objects.get(tenant=self.other).position_status == "pending"

    def test_the_batch_limit_is_respected(self):
        account_list.save_entries(
            self.tenant.id,
            [_row(name=f"Store {i}", address=f"{i + 1} Oak St", city="Mesa", state="AZ") for i in range(5)],
            self.admin,
        )
        with patch(LOOKUP_PATH, return_value=(geocoding.OK, (33.4, -111.8), "found")) as lookup:
            result = account_list.place_entries(self.tenant.id, limit=2)
        assert lookup.call_count == 2
        assert (result.placed, result.remaining) == (2, 3)

    # ── through the real schema: who may do what ──────────────────────

    @pytest.mark.asyncio
    async def test_staff_save_and_a_brand_user_on_another_device_reads_it(self):
        saved = await self._execute_mutation(
            FE_SAVE_MUTATION,
            {"tenantId": str(self.tenant.id), "accounts": [CHINO, {**NYC, "lat": 40.7616, "lng": -73.9847}]},
            user=self.admin,
        )
        assert saved.errors is None, saved.errors
        assert saved.data["saveAccountList"] == {
            "added": 2,
            "duplicates": 0,
            "skipped": 0,
            "total": 2,
            "awaitingPosition": 1,
        }

        read = await self._execute_mutation(
            FE_LIST_QUERY, {"tenantId": str(self.tenant.id)}, user=self.client_user
        )
        assert read.errors is None, read.errors
        rows = {r["name"]: r for r in read.data["accountListEntries"]}
        assert set(rows) == {"Albertsons 524", "7-Eleven #3201"}
        assert rows["7-Eleven #3201"]["positionStatus"] == "provided"
        assert rows["7-Eleven #3201"]["lat"] == 40.7616
        assert rows["Albertsons 524"]["lat"] is None
        assert rows["Albertsons 524"]["city"] == "Chino"

        with patch(LOOKUP_PATH, return_value=(geocoding.OK, (34.0122, -117.6889), "found")):
            placed = await self._execute_mutation(
                FE_PLACE_MUTATION,
                {"tenantId": str(self.tenant.id), "limit": 25},
                user=self.client_user,
            )
        assert placed.errors is None, placed.errors
        assert placed.data["placeAccountListEntries"] == {
            "placed": 1,
            "notFound": 0,
            "remaining": 0,
            "lookupAvailable": True,
        }

    @pytest.mark.asyncio
    async def test_a_brand_user_can_add_to_their_own_brands_list(self):
        saved = await self._execute_mutation(
            FE_SAVE_MUTATION,
            {"tenantId": str(self.tenant.id), "accounts": [CHINO]},
            user=self.client_user,
        )
        assert saved.errors is None, saved.errors
        assert saved.data["saveAccountList"]["added"] == 1

    @pytest.mark.asyncio
    async def test_a_brand_user_is_refused_another_brands_list(self):
        await sync_to_async(account_list.save_entries)(
            self.other.id, [_row(**CHINO)], self.admin
        )
        other_id = str(self.other.id)

        read = await self._execute_mutation(
            FE_LIST_QUERY, {"tenantId": other_id}, user=self.client_user
        )
        assert read.errors is not None
        assert "Not allowed" in str(read.errors[0])

        saved = await self._execute_mutation(
            FE_SAVE_MUTATION,
            {"tenantId": other_id, "accounts": [NYC]},
            user=self.client_user,
        )
        assert saved.errors is not None
        assert "Not allowed" in str(saved.errors[0])

        with patch(LOOKUP_PATH) as lookup:
            placed = await self._execute_mutation(
                FE_PLACE_MUTATION, {"tenantId": other_id, "limit": 5}, user=self.client_user
            )
        assert placed.errors is not None
        assert "Not allowed" in str(placed.errors[0])
        lookup.assert_not_called()

        def state():
            rows = em.AccountListEntry.objects.filter(tenant=self.other)
            return rows.count(), rows.first().position_status

        assert await sync_to_async(state)() == (1, "pending")

    @pytest.mark.asyncio
    async def test_an_ambassador_gets_nothing(self):
        def seed():
            ba = self.create_user(
                username="list-ba", email="list-ba@test.com", role=self.roles["ambassador"]
            )
            # Even booked into the brand, an ambassador is not its user.
            self.create_tenanted_user(ba, self.tenant)
            account_list.save_entries(self.tenant.id, [_row(**CHINO)], self.admin)
            return ba

        ba = await sync_to_async(seed)()
        tenant_id = str(self.tenant.id)
        read = await self._execute_mutation(FE_LIST_QUERY, {"tenantId": tenant_id}, user=ba)
        assert read.errors is not None
        saved = await self._execute_mutation(
            FE_SAVE_MUTATION, {"tenantId": tenant_id, "accounts": [NYC]}, user=ba
        )
        assert saved.errors is not None
        assert await sync_to_async(em.AccountListEntry.objects.count)() == 1
