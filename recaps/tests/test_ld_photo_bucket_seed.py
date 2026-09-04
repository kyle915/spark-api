"""The LD photo-bucket seeder: reuse before create, and never twice.

Two writes have to agree — a ``FileRecapCategory`` per bucket and the
``Tenant.checkin_photo_buckets`` lists the page renders — and both are seeded by
a command that gets re-dispatched every time the brand's line-up changes. The
failure mode is quiet: a second near-identical category ("Table setup" beside
"Table Set Up") splits a bucket in the recap PDF without erroring, which is the
same shape as the receipt that once landed under "Table setup".

The other trap is the upload sentinels. "1"/"2" resolve to a tenant's photos /
receipts category BY NAME, so absorbing one of those rows into a bucket rename
would make the fallback path create a fresh one beside it and split the
brand's history across two categories.

Since the link serves two programs, the config is keyed by event type name and
the shot lists differ. A bucket BOTH programs want must resolve to ONE category
row — created once, referenced twice — or the brand's consumer-sampling photos
fragment across two rows for no reader's benefit.
"""

from __future__ import annotations

import pytest

from recaps.management.commands.setup_ld_retail_checkin import (
    PROGRAMS,
    SENTINEL_CATEGORY_NAMES,
    Command,
    _norm,
)
from tenants.tests.base import BaseGraphQLTestCase

RETAIL_BUCKETS = [b["name"] for b in PROGRAMS[0]["photos"]]
ACTIVATION_BUCKETS = [b["name"] for b in PROGRAMS[1]["photos"]]
SEEDING_BUCKETS = [b["name"] for b in PROGRAMS[2]["photos"]]
# Every distinct bucket across all programs — the row set the seeder must own.
ALL_BUCKETS = list(
    dict.fromkeys(RETAIL_BUCKETS + ACTIVATION_BUCKETS + SEEDING_BUCKETS)
)


class TestBucketSpec:
    def test_the_buckets_kyle_asked_for(self):
        assert RETAIL_BUCKETS == [
            "Table Set Up",
            "Product Display",
            "Consumer Sampling Pictures",
            "Product Receipt",
        ]
        assert ACTIVATION_BUCKETS == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        assert SEEDING_BUCKETS == [
            "Drop-off Placement",
        ]

    def test_consumer_sampling_is_the_same_bucket_in_both_sampling_programs(self):
        """One row, two lists. Two rows would split the brand's consumer shots
        across categories that read identically in the PDF."""
        assert _norm(RETAIL_BUCKETS[2]) == _norm(ACTIVATION_BUCKETS[1])

    def test_only_consumer_sampling_carries_a_target(self):
        every = (
            PROGRAMS[0]["photos"]
            + PROGRAMS[1]["photos"]
            + PROGRAMS[2]["photos"]
        )
        by_name = {b["name"]: b for b in every}
        assert by_name["Consumer Sampling Pictures"]["min"] == 8
        assert by_name["Consumer Sampling Pictures"]["helper"] == (
            "please try to upload 8+"
        )
        assert all(
            "min" not in b for b in every if b["name"] != "Consumer Sampling Pictures"
        )

    def test_no_bucket_would_absorb_an_upload_sentinel(self):
        """A bucket that normalises onto "Sampling photos" or "Receipts" would
        rename the row the "1"/"2" sentinels resolve by name, and the fallback
        path would then create a duplicate beside it."""
        sentinels = {_norm(n) for n in SENTINEL_CATEGORY_NAMES}
        assert not sentinels & {_norm(n) for n in ALL_BUCKETS}

    def test_labels_that_differ_only_in_case_or_spacing_are_one_bucket(self):
        assert _norm("Table setup") == _norm("Table Set Up") == "tablesetup"
        assert _norm("table-set-up") == _norm("Table Set Up")
        assert _norm("Product Display") != _norm("Product Receipt")

    def test_retail_leads_so_the_unasked_fallback_is_the_busier_program(self):
        assert PROGRAMS[0]["event_type"] == "retail sampling"


@pytest.mark.django_db(transaction=True)
class TestBucketSeeding(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="LD Seed")
        self.retail = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.system_user
        )
        self.activation = EventType.objects.create(
            name="Event Activation", tenant=self.tenant, created_by=self.system_user
        )
        self.seeding = EventType.objects.create(
            name="Product Seeding", tenant=self.tenant, created_by=self.system_user
        )
        self.programs = [
            {"type": self.retail, "template": None, "photos": PROGRAMS[0]["photos"]},
            {
                "type": self.activation,
                "template": None,
                "photos": PROGRAMS[1]["photos"],
            },
            {
                "type": self.seeding,
                "template": None,
                "photos": PROGRAMS[2]["photos"],
            },
        ]
        self.cmd = Command()

    def _run(self):
        """Plan + write, the way handle() does."""
        plan = self.cmd._plan_photo_buckets(self.tenant, self.programs)
        config = self.cmd._ensure_photo_buckets(self.tenant, self.programs, plan)
        self.tenant.checkin_photo_buckets = config
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        return config

    def _names(self):
        from recaps.models import FileRecapCategory

        return sorted(
            FileRecapCategory.objects.filter(tenant=self.tenant).values_list(
                "name", flat=True
            )
        )

    def test_creates_every_bucket_and_writes_a_config_per_program(self):
        config = self._run()
        assert self._names() == sorted(ALL_BUCKETS)
        assert [c["name"] for c in config["Retail Sampling"]] == RETAIL_BUCKETS
        assert [c["name"] for c in config["Event Activation"]] == ACTIVATION_BUCKETS
        assert [c["name"] for c in config["Product Seeding"]] == SEEDING_BUCKETS
        # Only the one Kyle put a target on carries the BA-facing hints.
        assert config["Retail Sampling"][2] == {
            "name": "Consumer Sampling Pictures",
            "helper": "please try to upload 8+",
            "min": 8,
        }
        assert config["Retail Sampling"][0] == {"name": "Table Set Up"}
        assert config["Product Seeding"][0] == {"name": "Drop-off Placement"}

    def test_a_shared_bucket_is_one_row_not_two(self):
        """Both programs list "Consumer Sampling Pictures" — one category."""
        from recaps.models import FileRecapCategory

        self._run()
        assert (
            FileRecapCategory.objects.filter(
                tenant=self.tenant, name="Consumer Sampling Pictures"
            ).count()
            == 1
        )

    def test_re_running_creates_nothing_new(self):
        first = self._run()
        before = self._names()
        assert self._run() == first
        assert self._names() == before

    def test_an_existing_seeded_default_is_relabelled_not_duplicated(self):
        """LD already has the seeded "Table setup". The bucket must reuse that
        row — a second one would split the brand's table shots in the PDF."""
        from recaps.models import FileRecapCategory

        existing = FileRecapCategory.objects.create(
            name="Table setup", tenant=self.tenant
        )
        self._run()

        existing.refresh_from_db()
        assert existing.name == "Table Set Up"
        assert self._names().count("Table Set Up") == 1
        assert "Table setup" not in self._names()

    def test_the_sentinel_categories_are_left_alone(self):
        from recaps.models import FileRecapCategory

        for name in SENTINEL_CATEGORY_NAMES:
            FileRecapCategory.objects.create(name=name, tenant=self.tenant)
        self._run()

        names = self._names()
        for name in SENTINEL_CATEGORY_NAMES:
            assert name in names, f"{name} must survive untouched"
        assert len(names) == len(ALL_BUCKETS) + len(SENTINEL_CATEGORY_NAMES)

    def test_a_pre_existing_duplicate_pair_resolves_to_the_older_row(self):
        """Whichever row history is already filed against keeps the bucket."""
        from recaps.models import FileRecapCategory

        older = FileRecapCategory.objects.create(
            name="Table setup", tenant=self.tenant
        )
        newer = FileRecapCategory.objects.create(
            name="TABLE SET UP", tenant=self.tenant
        )
        self._run()

        older.refresh_from_db()
        newer.refresh_from_db()
        assert older.name == "Table Set Up"
        # The stray is reported, not silently folded in or deleted.
        assert newer.name == "TABLE SET UP"

    def test_another_tenants_categories_are_never_touched(self):
        from recaps.models import FileRecapCategory

        other = self.create_tenant(name="Not LD")
        theirs = FileRecapCategory.objects.create(
            name="Table setup", tenant=other
        )
        self._run()

        theirs.refresh_from_db()
        assert theirs.name == "Table setup"
        assert FileRecapCategory.objects.filter(tenant=other).count() == 1

    def test_the_written_config_is_what_the_page_will_be_served(self):
        """The seeder's two writes have to agree. This is the join: what it
        stored, read back through the resolver the page actually uses."""
        from ambassadors.checkin_web import serialize_photo_buckets
        from events.models import Event

        self._run()
        self.tenant.refresh_from_db()

        retail = serialize_photo_buckets(
            Event(tenant=self.tenant, event_type=self.retail)
        )
        activation = serialize_photo_buckets(
            Event(tenant=self.tenant, event_type=self.activation)
        )
        assert [b["name"] for b in retail] == RETAIL_BUCKETS
        assert [b["name"] for b in activation] == ACTIVATION_BUCKETS
        assert retail[2]["min"] == 8
        # Same shared row on both sides, so a photo filed from either program
        # lands in one category.
        assert retail[2]["id"] == activation[1]["id"]
        # Every offered bucket has a real category id the submit path accepts.
        assert all(b["id"].isdigit() for b in retail + activation)
