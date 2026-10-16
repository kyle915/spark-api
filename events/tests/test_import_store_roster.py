"""`import_store_roster` preloads a client store roster as Retailer accounts.

Each store lands on every brand the roster lists (Retailer is tenant-scoped),
as a regional account on the city Location the shared address parser picks,
so the request-form store picker (state / city filtered) and the walk-up
store list show it. Re-runs never duplicate.
"""

import csv
from io import StringIO

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import RestrictedError

from ambassadors import checkin_web
from config.schema_client import schema_clients
from events import models as em
from events.management.commands import import_store_roster
from events.market import parse_address_geo
from events.tests.base import EventsGraphQLTestCase

FIELDS = ["roster_no", "store_name", "address", "city", "state", "zip", "brands", "maps_match"]
ROWS = [
    ["1", "Shell", "1303 S Main St, Bluffton, IN 46714", "Bluffton", "IN", "46714",
     "famus-energy|wook-water", "Verified"],
    ["2", "Circle K", "720 N 3rd St, Boonville, IN 47601", "Boonville", "IN", "47601",
     "alcohol-armor", "Verified"],
    ["3", "BP", "2926 Nashville Rd, Bowling Green, KY 42103", "Bowling Green", "KY", "42103",
     "famus-energy|alcohol-armor|wook-water", "Probable - confirm"],
    ["4", "Smokers Outlet", "240 Eastbrooke Pkwy #105, Mt Washington, KY 40047", "Mt Washington",
     "KY", "40047", "famus-energy", "Verified"],
]


def test_committed_roster_has_store_names_and_parseable_addresses():
    rows = import_store_roster.read_roster(import_store_roster.DEFAULT_FILE)
    legal = [
        r["store_name"] for r in rows
        if import_store_roster.is_placeholder_name(r["store_name"])
        and "(name to confirm)" not in r["store_name"]
    ]
    assert legal == []
    keys = [checkin_web.normalize_place(r["address"]) for r in rows]
    assert len(keys) == len(set(keys))
    for r in rows:
        geo = parse_address_geo(r["address"])
        assert (geo.city, geo.state_code, geo.zip) == (r["city"], r["state"], r["zip"]), r["roster_no"]


@pytest.mark.django_db(transaction=True)
class TestImportStoreRoster(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, tmp_path, monkeypatch):
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.roles = self.setup_default_roles()
        self.owner = self.create_user(
            username="roster-admin", email="roster-admin@igniteproductions.co",
            role=self.roles["spark_admin"], is_staff=True,
        )
        self.tenants = {}
        for slug in ("famus-energy", "alcohol-armor", "wook-water"):
            self.tenants[slug] = self.create_tenant(name=slug.title(), slug=slug)
        self.indiana, _ = em.State.objects.get_or_create(
            code="IN", defaults={"name": "Indiana", "created_by": self.sys}
        )
        self.kentucky, _ = em.State.objects.get_or_create(
            code="KY", defaults={"name": "Kentucky", "created_by": self.sys}
        )
        self.bluffton = em.Location.objects.create(
            name="Bluffton", code="IN", zip="46714", state=self.indiana, created_by=self.sys
        )
        self.boonville = em.Location.objects.create(
            name="Boonville", code="IN", zip="47601", state=self.indiana, created_by=self.sys
        )
        self.mount_washington = em.Location.objects.create(
            name="Mount Washington", code="KY", zip="40047", state=self.kentucky, created_by=self.sys
        )
        monkeypatch.setattr(import_store_roster, "DATA_DIR", tmp_path)
        with (tmp_path / "roster.csv").open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(FIELDS)
            writer.writerows(ROWS)

    def run_import(self, *extra: str) -> str:
        out = StringIO()
        call_command(
            "import_store_roster", "--file", "roster.csv",
            "--owner-email", self.owner.email, *extra, stdout=out,
        )
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        log = self.run_import()
        assert "DRY-RUN" in log
        assert "famus-energy: created=3 updated=0 unchanged=0" in log
        assert "alcohol-armor: created=2" in log
        assert "wook-water: created=2" in log
        assert "stores on more than one brand: 2" in log
        assert "cities added to Location catalog: 1" in log
        assert em.Retailer.objects.count() == 0
        assert not em.Location.objects.filter(name="Bowling Green").exists()

    def test_apply_creates_one_account_per_brand_per_store(self):
        log = self.run_import("--apply")
        assert "APPLY" in log
        famus = em.Retailer.objects.filter(tenant=self.tenants["famus-energy"])
        assert sorted(famus.values_list("name", flat=True)) == ["BP", "Shell", "Smokers Outlet"]
        assert em.Retailer.objects.filter(tenant=self.tenants["wook-water"]).count() == 2
        assert em.Retailer.objects.filter(tenant=self.tenants["alcohol-armor"]).count() == 2

        shell = famus.get(name="Shell")
        assert shell.address == "1303 S Main St, Bluffton, IN 46714"
        assert shell.is_national is False
        assert shell.location_id == self.bluffton.id
        assert shell.created_by_id == self.owner.id

        bowling_green = em.Location.objects.get(name="Bowling Green")
        assert bowling_green.state_id == self.kentucky.id
        assert bowling_green.zip == "42103"
        assert set(
            em.Retailer.objects.filter(name="BP").values_list("location_id", flat=True)
        ) == {bowling_green.id}
        assert "#3 BP — 2926 Nashville Rd, Bowling Green, KY 42103: Google Maps probable - confirm" in log

    def test_catalog_spelling_variant_matches_by_zip(self):
        log = self.run_import("--apply", "--tenant", "famus-energy")
        outlet = em.Retailer.objects.get(name="Smokers Outlet")
        assert outlet.location_id == self.mount_washington.id
        assert not em.Location.objects.filter(name="Mt Washington").exists()
        assert "-> catalog Mount Washington (same zip)" in log

    def test_rerun_is_idempotent(self):
        self.run_import("--apply")
        log = self.run_import("--apply")
        assert "famus-energy: created=0 updated=0 unchanged=3" in log
        assert "cities added to Location catalog: 0" in log
        assert em.Retailer.objects.count() == 7
        assert em.Location.objects.filter(name="Bowling Green").count() == 1

    def test_existing_account_at_same_address_is_reused(self):
        em.Retailer.objects.create(
            tenant=self.tenants["alcohol-armor"], name="Circle K #720",
            address="720 n 3rd st,  Boonville, IN 47601", created_by=self.sys,
        )
        log = self.run_import("--apply", "--tenant", "alcohol-armor")
        assert "alcohol-armor: created=1 updated=1 unchanged=0" in log
        circle_k = em.Retailer.objects.get(tenant=self.tenants["alcohol-armor"], address__icontains="720")
        assert circle_k.name == "Circle K #720"
        assert circle_k.location_id == self.boonville.id
        assert not em.Retailer.objects.filter(tenant=self.tenants["famus-energy"]).exists()

    def test_placeholder_name_takes_the_trading_name(self):
        legal = em.Retailer.objects.create(
            tenant=self.tenants["famus-energy"], name="Avni Petroleum INC",
            address="1303 S Main St, Bluffton, IN 46714", created_by=self.sys,
        )
        fallback = em.Retailer.objects.create(
            tenant=self.tenants["famus-energy"], name="2926 Nashville Rd (name to confirm)",
            address="2926 Nashville Rd, Bowling Green, KY 42103", created_by=self.sys,
        )
        edited = em.Retailer.objects.create(
            tenant=self.tenants["famus-energy"], name="Smokers Outlet Mt Washington",
            address="240 Eastbrooke Pkwy #105, Mt Washington, KY 40047", created_by=self.sys,
        )
        dry = self.run_import("--tenant", "famus-energy")
        assert "renamed=2" in dry
        legal.refresh_from_db()
        assert legal.name == "Avni Petroleum INC"

        log = self.run_import("--apply", "--tenant", "famus-energy")
        assert "rename 'Avni Petroleum INC' -> 'Shell'" in log
        assert "famus-energy: created=0" in log and "renamed=2" in log
        for row in (legal, fallback, edited):
            row.refresh_from_db()
        assert (legal.name, fallback.name, edited.name) == ("Shell", "BP", "Smokers Outlet Mt Washington")
        assert legal.updated_by_id == self.owner.id
        assert "renamed=0" in self.run_import("--apply", "--tenant", "famus-energy")

    def test_generic_station_name_takes_specific_store_name(self):
        assert import_store_roster.should_rename_account("Marathon Gas", "J&H Foodmart (Marathon)")
        assert import_store_roster.should_rename_account("Shell", "KSP Care First (Shell)")
        assert not import_store_roster.should_rename_account("Circle K #720", "Circle K")
        assert not import_store_roster.should_rename_account("Shell", "Shell")

    def test_roster_name_to_confirm_never_overwrites_a_real_name(self):
        path = import_store_roster.DATA_DIR / "roster.csv"
        with path.open("a", newline="") as fh:
            csv.writer(fh).writerow(
                ["5", "720 N 3rd St (name to confirm)", "720 N 3rd St, Boonville, IN 47601",
                 "Boonville", "IN", "47601", "famus-energy", "Not found"]
            )
        em.Retailer.objects.create(
            tenant=self.tenants["famus-energy"], name="Ace Food Mart LLC",
            address="720 N 3rd St, Boonville, IN 47601", created_by=self.sys,
        )
        self.run_import("--apply", "--tenant", "famus-energy")
        assert em.Retailer.objects.get(address__startswith="720 N").name == "Ace Food Mart LLC"

    def test_not_a_store_row_retires_the_account(self):
        fields = [*FIELDS, "store_type"]
        rows = [[*ROWS[0][:7], "Verified", "Not a store"], [*ROWS[1], "Gas Station"]]
        with (import_store_roster.DATA_DIR / "roster.csv").open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            writer.writerows(rows)
        home = em.Retailer.objects.create(
            tenant=self.tenants["wook-water"], name="Some Holdings LLC",
            address="1303 S Main St, Bluffton, IN 46714", created_by=self.sys,
        )
        dry = self.run_import()
        assert "wook-water: created=0 updated=0 unchanged=0 renamed=0 retired=1" in dry
        assert em.Retailer.objects.filter(pk=home.pk).exists()

        log = self.run_import("--apply")
        assert "famus-energy: created=0" in log and "retired=0" in log
        assert not em.Retailer.objects.filter(address__startswith="1303 S Main").exists()
        assert em.Retailer.objects.filter(tenant=self.tenants["alcohol-armor"]).count() == 1
        assert "retired=0" in self.run_import("--apply").split("wook-water:")[1]

    def test_not_a_store_account_with_requests_is_kept(self, monkeypatch):
        def restricted(self, *args, **kwargs):
            raise RestrictedError("referenced by a request", set())

        monkeypatch.setattr(em.Retailer, "delete", restricted)
        fields = [*FIELDS, "store_type"]
        with (import_store_roster.DATA_DIR / "roster.csv").open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            writer.writerow([*ROWS[1][:7], "Verified", "Not a store"])
        home = em.Retailer.objects.create(
            tenant=self.tenants["alcohol-armor"], name="Some Holdings LLC",
            address="720 N 3rd St, Boonville, IN 47601", created_by=self.sys,
        )
        log = self.run_import("--apply")
        assert "retired=0" in log
        assert "has requests/events" in log
        assert em.Retailer.objects.filter(pk=home.pk).exists()

    def test_missing_tenant_fails_before_writing(self):
        self.tenants["wook-water"].slug = "wook-water-old"
        self.tenants["wook-water"].save(update_fields=["slug"])
        with pytest.raises(CommandError, match="tenant-not-found: wook-water"):
            self.run_import("--apply")
        assert em.Retailer.objects.count() == 0

    def test_file_must_stay_inside_the_data_dir(self):
        with pytest.raises(CommandError, match="file name inside"):
            call_command(
                "import_store_roster", "--file", "../settings.py",
                "--owner-email", self.owner.email, stdout=StringIO(),
            )

    def test_accounts_feed_the_walkup_store_list(self):
        self.run_import("--apply")
        out = checkin_web.recent_checkin_locations(self.tenants["wook-water"])
        assert {x["address"] for x in out} == {
            "1303 S Main St, Bluffton, IN 46714",
            "2926 Nashville Rd, Bowling Green, KY 42103",
        }

    @pytest.mark.asyncio
    async def test_public_retailer_search_matches_address(self):
        famus = self.tenants["famus-energy"]
        famus.request_url_name = "famus-energy"
        await sync_to_async(famus.save)(update_fields=["request_url_name"])
        await sync_to_async(self.run_import)("--apply")
        query = """
        query($q: String, $filters: RetailerFiltersInput) {
          publicRetailers(requestUrlName: "famus-energy", q: $q, filters: $filters) {
            edges { node { name address } }
          }
        }
        """
        res = await self._execute_mutation(
            query, {"q": "bluffton", "filters": {"isNational": False, "stateId": str(self.indiana.id)}}
        )
        assert res.errors is None, res.errors
        assert [e["node"]["name"] for e in res.data["publicRetailers"]["edges"]] == ["Shell"]
