"""Martinelli's event check-in: Liquid Death event questions, storefront SKUs.

Event recap only. A blank setup-brand-group run still sets up the FAMUS group
and does not create this tenant or join Martinelli's to that group.
"""

from __future__ import annotations

import io
import re

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from events.event_confirmations import catalog_product_options
from events.models import EventType, Product, ProductType
from recaps.models import CustomField, CustomRecapTemplate
from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD
from tenants.management.commands.setup_brand_group import (
    BRANDS,
    EVENT,
    MARTINELLIS_EVENT_BUCKETS,
    PROGRAMS,
    martinellis_event_spec,
    sections_for,
)
from tenants.models import Tenant
from tenants.tests.base import BaseGraphQLTestCase

TEMPLATE_NAME = "Martinelli's · Event Activation Recap"
CODE_RE = re.compile(r"MN-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}")
CAN_SKUS = (
    "Sparkling Apple 8.4 fl. oz. / 250 mL",
    "Sparkling Apple Mango 8.4 fl. oz. / 250 mL",
    "Sparkling Apple Cranberry 8.4 fl. oz. / 250 mL",
)
# Standing codes that already belong to other brands. Never remint these.
TAKEN_CODES = ("PE-Y7VPYC", "FM-PAG4TX", "AA-RPP85E", "WW-36DJAJ", "TH-RHTGP9")

FIELD_NAMES = [
    "How many consumers would be willing to purchase the product after tasting it?",
    "How many consumers that were engaged with knew about Martinelli's product/brand?",
    "How many consumers had tried a Martinelli's flavor before?",
    "How many TOTAL consumers did you sample?",
    "Demographics",
    "What were the top 5 frequently asked questions you received from consumers?",
    "Helpful feedback",
    PRODUCTS_SAMPLED_FIELD,
]


def _brand():
    return next(brand for brand in BRANDS if brand.slug == "martinellis")


def _run(**kwargs) -> str:
    out = io.StringIO()
    call_command("setup_brand_group", skip_images=True, stdout=out, **kwargs)
    return out.getvalue()


class TestMartinellisSpec:
    def test_event_fields_mirror_liquid_death_with_martinellis_wording(self):
        spec = martinellis_event_spec()
        flat = [(name, kind, required) for _, fields in spec for name, kind, required, _ in fields]
        assert [name for name, _, _ in flat] == FIELD_NAMES
        assert [kind for _, kind, _ in flat] == [
            "number",
            "number",
            "number",
            "number",
            "longtext",
            "longtext",
            "longtext",
            "multiselect",
        ]
        assert all(required for _, _, required in flat)
        assert [section for section, _ in spec] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Products Sampled",
        ]
        blob = str(spec)
        assert "mileage" not in blob.lower()
        assert "tasing" not in blob.lower()
        assert "Liquid Death" not in blob
        assert [program for program, _ in sections_for(_brand())] == [EVENT]

    def test_storefront_skus_include_the_three_cans(self):
        brand = _brand()
        names = [sku.name for sku in brand.skus]
        assert len(names) == 39
        assert len(set(names)) == 39
        for name in names:
            assert len(name) <= 50
        for can in CAN_SKUS:
            sku = next(row for row in brand.skus if row.name == can)
            assert sku.product_type == "Cans"
        assert brand.name == "Martinelli's"
        assert brand.slug == "martinellis"
        assert brand.code_prefix == "MN"
        assert brand.in_group is False
        assert brand.programs == (EVENT,)

    def test_blank_run_stays_the_famus_group(self):
        group = [brand for brand in BRANDS if brand.in_group]
        assert [brand.slug for brand in group] == [
            "famus-energy",
            "alcohol-armor",
            "wook-water",
        ]
        assert all(brand.programs == PROGRAMS for brand in group)


@pytest.mark.django_db(transaction=True)
class TestMartinellisSetup(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def test_dry_run_writes_nothing_and_reports_eight_fields(self):
        log = _run(tenant="martinellis")
        assert "DRY-RUN" in log
        assert "3 sections / 8 fields" in log
        assert TEMPLATE_NAME in log
        assert not Tenant.objects.filter(slug="martinellis").exists()

    def test_loose_slug_is_rejected(self):
        with pytest.raises(CommandError, match="isn't one of"):
            _run(tenant="martinelli")

    def test_default_run_does_not_mention_martinellis(self):
        log = _run()
        assert "Martinelli" not in log
        assert "FAMUS Energy" in log
        assert not Tenant.objects.filter(slug="martinellis").exists()

    def test_apply_creates_event_recap_catalog_and_one_code(self):
        for index, code in enumerate(TAKEN_CODES):
            self.create_tenant(name=f"Taken {index}", slug=f"taken-{index}", checkin_code=code)

        log = _run(tenant="martinellis", apply=True)
        tenant = Tenant.objects.get(slug="martinellis")
        assert tenant.name == "Martinelli's"
        assert tenant.checkin_code not in TAKEN_CODES
        assert CODE_RE.fullmatch(tenant.checkin_code)
        assert f"/checkin/{tenant.checkin_code}" in log
        assert "3 sections / 8 fields" in log
        assert tenant.brand_group_id is None
        assert tenant.auto_approve_requests is False
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS

        programs = list(EventType.objects.filter(tenant=tenant).values_list("name", flat=True))
        assert programs == ["Event Activation"]
        assert tenant.checkin_event_type.name == "Event Activation"
        assert list(tenant.checkin_event_types.values_list("name", flat=True)) == ["Event Activation"]

        templates = list(CustomRecapTemplate.objects.filter(tenant=tenant))
        assert [row.name for row in templates] == [TEMPLATE_NAME]
        template = templates[0]
        assert (template.layout or {}).get("coBrandSections") in (None, [])
        fields = list(
            CustomField.objects.filter(custom_recap_template=template)
            .select_related("recap_section", "custom_field_type")
            .order_by("recap_section__order", "order", "id")
        )
        assert [field.name for field in fields] == FIELD_NAMES
        assert len(fields) == 8
        assert all(field.required for field in fields)
        assert "Mileage" not in {field.name for field in fields}

        options = catalog_product_options(tenant)
        assert len(options) == 39
        for can in CAN_SKUS:
            assert f"Cans — {can}" in options
        sampled = CustomField.objects.get(
            custom_recap_template=template, name__iexact=PRODUCTS_SAMPLED_FIELD
        )
        assert sampled.options == options
        assert Product.objects.filter(tenant=tenant).count() == 39
        assert set(ProductType.objects.filter(tenant=tenant).values_list("name", flat=True)) == {
            "Cans",
            "Sparkling",
            "Still Juices",
        }
        assert [bucket["name"] for bucket in tenant.checkin_photo_buckets[EVENT]] == [
            bucket["name"] for bucket in MARTINELLIS_EVENT_BUCKETS
        ]
        assert "Retail Sampling" not in log

    def test_rerun_keeps_the_standing_code(self):
        _run(tenant="martinellis", apply=True)
        tenant = Tenant.objects.get(slug="martinellis")
        tenant.checkin_code = "MN-KEEPME"
        tenant.save(update_fields=["checkin_code"])
        log = _run(tenant="martinellis", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == "MN-KEEPME"
        assert "MN-KEEPME" in log
        assert "kept" in log
        assert CustomRecapTemplate.objects.filter(tenant=tenant).count() == 1
        assert Product.objects.filter(tenant=tenant).count() == 39
