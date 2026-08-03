"""Labelled photo buckets on the check-in recap.

The recap step shipped with ONE generic "Photos" grid, so every shot a BA took
— the table, the shelf, the consumers, the receipt — was filed under a single
FileRecapCategory by a hardcoded ``"1"`` sentinel and the recap PDF could not
tell them apart. Liquid Death needs four labelled dropzones instead.

The pieces that can break quietly, and are therefore what these tests pin:

* a brand with NO buckets configured must behave exactly as it did before the
  feature existed — Total Wireless and Feel Free share this code path on a live
  link, and "their photos silently moved" is not a thing anyone would notice
  until a recap PDF looked wrong weeks later;
* a per-file category must be one of the brand's OWN buckets, so neither a
  stale page nor a forged request can file into another tenant's category;
* an unusable bucket (its category row is gone) must not be offered at all,
  because a dropzone whose uploads fall back to the generic pile looks like it
  worked.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

BUCKETS = [
    {"name": "Table Set Up"},
    {"name": "Product Display"},
    {
        "name": "Consumer Sampling Pictures",
        "helper": "please try to upload 8+",
        "min": 8,
    },
    {"name": "Product Receipt"},
]


@pytest.mark.django_db(transaction=True)
class TestCheckinPhotoBuckets(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from recaps.models import (
            CustomRecapFieldType,
            CustomRecapTemplate,
            FileRecapCategory,
            FileType,
            RecapSection,
        )

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Liquid Death Buckets")
        self.actor = self.create_user(
            username="actor-pb@test.com",
            email="actor-pb@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-pb@test.com",
            email="ba-pb@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)

        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="LD-Retail Sampling",
            event_type=etype,
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="HEB Congress",
            tenant=self.tenant,
            address="123 Congress Ave",
            event_type=etype,
            date=timezone.now(),
        )
        # The submit path needs at least one FileType to attach photos to, and
        # a "photos" category for the sentinel fallback to land on.
        FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.actor}
        )
        self.photos_cat = FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.actor
        )
        self.cats = {
            spec["name"]: FileRecapCategory.objects.create(
                name=spec["name"], tenant=self.tenant, created_by=self.actor
            )
            for spec in BUCKETS
        }
        # Referenced by _ensure_products_field-style writes elsewhere; unused
        # here but keeps the template shaped like a real one.
        self.section = RecapSection.objects.create(
            tenant=self.tenant, name="Photos", order=0, created_by=self.actor
        )
        self.field_type, _ = CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )

    # -- helpers -----------------------------------------------------------

    def _enable(self, buckets=None):
        self.tenant.checkin_photo_buckets = (
            BUCKETS if buckets is None else buckets
        )
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        self.event.refresh_from_db()

    def _submit(self, files):
        return checkin_web.submit_checkin_recap(
            event=self.event,
            ambassador=self.ba,
            template=self.template,
            field_values=[],
            files=files,
            total_engagements=None,
        )

    def _blob(self, name: str) -> str:
        return f"recap_files/checkin/{self.event.uuid}/{name}.jpg"

    def _filed(self, recap) -> dict[str, str]:
        """{blob basename: category name} for everything on the recap."""
        from recaps.models import CustomRecapFile

        return {
            str(f.url.name).rsplit("/", 1)[-1]: (
                f.file_recap_category.name if f.file_recap_category else None
            )
            for f in CustomRecapFile.objects.filter(custom_recap=recap)
        }

    # -- brands that never opted in ----------------------------------------

    def test_no_buckets_configured_is_the_old_behaviour(self):
        """Total Wireless / Feel Free: no buckets in the payload, and every
        photo still lands on the "photos" sentinel category."""
        assert checkin_web.serialize_photo_buckets(self.event) == []
        assert checkin_web.build_public_context(self.event)["photoBuckets"] == []

        recap = self._submit([{"blobName": self._blob("a")}])
        assert self._filed(recap) == {"a.jpg": "Sampling photos"}

    def test_a_category_sent_by_a_brand_without_buckets_is_ignored(self):
        """Turning the feature on is an opt-in on the TENANT, not something a
        request can do for itself."""
        recap = self._submit(
            [{"blobName": self._blob("a"), "category": str(self.cats["Product Receipt"].id)}]
        )
        assert self._filed(recap) == {"a.jpg": "Sampling photos"}

    # -- the payload the page renders --------------------------------------

    def test_buckets_carry_their_category_id_helper_and_min(self):
        self._enable()
        buckets = checkin_web.serialize_photo_buckets(self.event)

        assert [b["name"] for b in buckets] == [s["name"] for s in BUCKETS]
        assert [b["id"] for b in buckets] == [
            str(self.cats[s["name"]].id) for s in BUCKETS
        ]
        sampling = buckets[2]
        assert sampling["helper"] == "please try to upload 8+"
        assert sampling["min"] == 8
        # The three without a target carry a falsy one, so the page can render
        # a count only where the brand asked for one.
        assert [b["min"] for b in buckets] == [0, 0, 8, 0]

    def test_a_bucket_whose_category_is_missing_is_not_offered(self):
        self._enable(BUCKETS + [{"name": "Nonexistent Bucket"}])
        names = [b["name"] for b in checkin_web.serialize_photo_buckets(self.event)]
        assert "Nonexistent Bucket" not in names
        assert len(names) == 4

    def test_category_matching_survives_a_relabel(self):
        """"Table setup" (the seeded default) and "Table Set Up" (the brand's
        wording) are one bucket, so a relabel doesn't orphan the dropzone."""
        cat = self.cats["Table Set Up"]
        cat.name = "Table setup"
        cat.save(update_fields=["name"])

        self._enable()
        buckets = checkin_web.serialize_photo_buckets(self.event)
        table = next(b for b in buckets if b["name"] == "Table Set Up")
        assert table["id"] == str(cat.id)

    # -- filing on submit ---------------------------------------------------

    def test_each_photo_lands_in_the_bucket_it_was_dropped_into(self):
        self._enable()
        recap = self._submit(
            [
                {"blobName": self._blob("table"), "category": str(self.cats["Table Set Up"].id)},
                {"blobName": self._blob("shelf"), "category": str(self.cats["Product Display"].id)},
                {
                    "blobName": self._blob("consumer"),
                    "category": str(self.cats["Consumer Sampling Pictures"].id),
                },
                {"blobName": self._blob("receipt"), "category": str(self.cats["Product Receipt"].id)},
            ]
        )
        assert self._filed(recap) == {
            "table.jpg": "Table Set Up",
            "shelf.jpg": "Product Display",
            "consumer.jpg": "Consumer Sampling Pictures",
            "receipt.jpg": "Product Receipt",
        }

    def test_a_file_with_no_category_still_files(self):
        """A page loaded before the buckets shipped must not lose photos."""
        self._enable()
        recap = self._submit([{"blobName": self._blob("legacy")}])
        assert self._filed(recap) == {"legacy.jpg": "Sampling photos"}

    def test_a_foreign_category_falls_back_instead_of_leaking(self):
        from recaps.models import FileRecapCategory

        other = self.create_tenant(name="Someone Else")
        theirs = FileRecapCategory.objects.create(
            name="Product Receipt", tenant=other, created_by=self.actor
        )
        self._enable()
        recap = self._submit(
            [{"blobName": self._blob("x"), "category": str(theirs.id)}]
        )
        # Not their category, and not silently uncategorised either.
        assert self._filed(recap) == {"x.jpg": "Sampling photos"}

    def test_a_junk_category_falls_back(self):
        self._enable()
        recap = self._submit(
            [
                {"blobName": self._blob("a"), "category": "not-an-id"},
                {"blobName": self._blob("b"), "category": "99999999"},
            ]
        )
        assert self._filed(recap) == {
            "a.jpg": "Sampling photos",
            "b.jpg": "Sampling photos",
        }
