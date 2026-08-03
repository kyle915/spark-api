"""The LD photo-bucket seeder: reuse before create, and never twice.

Two writes have to agree — a ``FileRecapCategory`` per bucket and the ordered
``Tenant.checkin_photo_buckets`` list the page renders — and both are seeded by
a command that gets re-dispatched every time the brand's line-up changes. The
failure mode is quiet: a second near-identical category ("Table setup" beside
"Table Set Up") splits a bucket in the recap PDF without erroring, which is the
same shape as the receipt that once landed under "Table setup".

The other trap is the upload sentinels. "1"/"2" resolve to a tenant's photos /
receipts category BY NAME, so absorbing one of those rows into a bucket rename
would make the fallback path create a fresh one beside it and split the
brand's history across two categories.
"""

from __future__ import annotations

import pytest

from recaps.management.commands.setup_ld_retail_checkin import (
    PHOTO_BUCKETS,
    SENTINEL_CATEGORY_NAMES,
    Command,
    _norm,
)
from tenants.tests.base import BaseGraphQLTestCase

BUCKET_NAMES = [b["name"] for b in PHOTO_BUCKETS]


class TestBucketSpec:
    def test_the_four_buckets_kyle_asked_for(self):
        assert BUCKET_NAMES == [
            "Table Set Up",
            "Product Display",
            "Consumer Sampling Pictures",
            "Product Receipt",
        ]

    def test_only_consumer_sampling_carries_a_target(self):
        by_name = {b["name"]: b for b in PHOTO_BUCKETS}
        assert by_name["Consumer Sampling Pictures"]["min"] == 8
        assert by_name["Consumer Sampling Pictures"]["helper"] == (
            "please try to upload 8+"
        )
        assert all(
            "min" not in b
            for b in PHOTO_BUCKETS
            if b["name"] != "Consumer Sampling Pictures"
        )

    def test_no_bucket_would_absorb_an_upload_sentinel(self):
        """A bucket that normalises onto "Sampling photos" or "Receipts" would
        rename the row the "1"/"2" sentinels resolve by name, and the fallback
        path would then create a duplicate beside it."""
        sentinels = {_norm(n) for n in SENTINEL_CATEGORY_NAMES}
        assert not sentinels & {_norm(n) for n in BUCKET_NAMES}

    def test_labels_that_differ_only_in_case_or_spacing_are_one_bucket(self):
        assert _norm("Table setup") == _norm("Table Set Up") == "tablesetup"
        assert _norm("table-set-up") == _norm("Table Set Up")
        assert _norm("Product Display") != _norm("Product Receipt")


@pytest.mark.django_db(transaction=True)
class TestBucketSeeding(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="LD Seed")
        self.cmd = Command()

    def _run(self):
        """Plan + write, the way handle() does."""
        plan = self.cmd._plan_photo_buckets(self.tenant)
        config = self.cmd._ensure_photo_buckets(self.tenant, plan)
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

    def test_creates_the_four_and_writes_the_config(self):
        config = self._run()
        assert self._names() == sorted(BUCKET_NAMES)
        assert [c["name"] for c in config] == BUCKET_NAMES
        # Only the one Kyle put a target on carries the BA-facing hints.
        assert config[2] == {
            "name": "Consumer Sampling Pictures",
            "helper": "please try to upload 8+",
            "min": 8,
        }
        assert config[0] == {"name": "Table Set Up"}

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
        assert len(names) == len(BUCKET_NAMES) + len(SENTINEL_CATEGORY_NAMES)

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
