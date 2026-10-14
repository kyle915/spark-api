"""`setup_brand_group` keeps each brand's catalog and Retail form to what it sells.

Wook Water sells 12oz cans and Alcohol Armor 2oz shots — no packs — so their
pack SKUs leave every SKU picker and their Retail recap drops the packs
question. Units sold then come from the singles field alone.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import EventType, Product, ProductType, Request, RequestProduct, RequestType
from recaps.models import CustomField, CustomFieldValue, CustomRecap, CustomRecapTemplate
from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD
from tenants.brand_group_demo import DEMO_DOMAIN
from tenants.models import Tenant

PACKS = "How many packs did consumers purchase?"


def _run(apply: bool = True) -> str:
    out = io.StringIO()
    call_command("setup_brand_group", apply=apply, skip_images=True, stdout=out)
    return out.getvalue()


@pytest.mark.django_db(transaction=True)
class TestBrandGroupCatalog(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        _run()
        self.famus = Tenant.objects.get(slug="famus-energy")
        self.aa = Tenant.objects.get(slug="alcohol-armor")
        self.ww = Tenant.objects.get(slug="wook-water")

    def _retail(self, tenant) -> CustomRecapTemplate:
        return CustomRecapTemplate.objects.get(tenant=tenant, event_type__name="Retail Sampling")

    def _skus(self, tenant) -> list[str]:
        return sorted(Product.objects.filter(tenant=tenant).values_list("name", flat=True))

    def _old_pack(self, tenant, name: str) -> Product:
        return Product.objects.create(
            tenant=tenant,
            name=name,
            product_type=ProductType.objects.filter(tenant=tenant).first(),
            created_by=self.sys,
        )

    def _recap(self, tenant, email: str) -> CustomRecap:
        user = self.create_user(username=email, email=email, role=self.roles["ambassador"])
        event = self.create_event(
            name=f"Shift {email}",
            tenant=tenant,
            address="100 Main St, Charlotte, NC 28202",
            event_type=EventType.objects.get(tenant=tenant, name="Retail Sampling"),
            date=timezone.now(),
        )
        return CustomRecap.objects.create(
            name=f"Recap {email}",
            event=event,
            ambassador=self.create_ambassador(user),
            custom_recap_template=self._retail(tenant),
            tenant=tenant,
            created_by=self.sys,
        )

    def test_catalog_and_retail_form_match_what_each_brand_sells(self):
        assert self._skus(self.ww) == [
            "Berry Groovy 12oz Can",
            "Laser Lime 12oz Can",
            "Pineapple Peak 12oz Can",
        ]
        assert self._skus(self.aa) == ["Boujee Berry 2oz Shot", "Slayberry 2oz Shot"]
        assert len(self._skus(self.famus)) == 2

        def field_names(tenant):
            return set(
                CustomField.objects.filter(custom_recap_template=self._retail(tenant)).values_list(
                    "name", flat=True
                )
            )

        assert PACKS not in field_names(self.ww)
        assert PACKS not in field_names(self.aa)
        assert PACKS in field_names(self.famus)
        assert "How many single cans / bottles / shots did consumers purchase?" in field_names(self.ww)

    def test_rerun_retires_discontinued_pack_skus(self):
        packs = [
            self._old_pack(self.ww, "Laser Lime 4-Pack"),
            self._old_pack(self.ww, "Berry Groovy 4-Pack"),
            self._old_pack(self.aa, "Slayberry 12-Pack"),
        ]
        dry = _run(apply=False)
        assert "- retire SKU" in dry and "'Laser Lime 4-Pack'" in dry
        assert Product.objects.filter(id__in=[p.id for p in packs]).count() == 3

        _run()
        assert not Product.objects.filter(id__in=[p.id for p in packs]).exists()
        options = CustomField.objects.get(
            custom_recap_template=self._retail(self.ww), name__iexact=PRODUCTS_SAMPLED_FIELD
        ).options
        assert options and not [o for o in options if "Pack" in o]
        assert "- retire SKU" not in _run(apply=False)

    def test_pack_sku_on_a_request_is_kept(self):
        pack = self._old_pack(self.ww, "Pineapple Peak 4-Pack")
        request = Request.objects.create(
            name="Publix demo",
            address="1 Main St, Atlanta, GA 30324",
            request_type=RequestType.objects.filter(tenant=self.ww).first(),
            tenant=self.ww,
            created_by=self.sys,
        )
        RequestProduct.objects.create(request=request, product=pack, tenant=self.ww)
        log = _run()
        assert "kept retired SKU" in log and "1 request SKU picks" in log
        assert Product.objects.filter(id=pack.id).exists()

    def test_dropped_packs_field_goes_with_demo_answers_only(self):
        template = self._retail(self.ww)
        singles = CustomField.objects.get(custom_recap_template=template, name__icontains="single cans")

        def packs_field():
            return CustomField.objects.create(
                custom_recap_template=template,
                recap_section=singles.recap_section,
                custom_field_type=singles.custom_field_type,
                name=PACKS,
                created_by=self.sys,
            )

        field = packs_field()
        demo = self._recap(self.ww, f"ba.sam@{DEMO_DOMAIN}")
        CustomFieldValue.objects.create(custom_recap=demo, custom_field=field, value="9", created_by=self.sys)
        dry = _run(apply=False)
        assert f"- drop field {PACKS!r} (+1 demo answer(s))" in dry
        assert CustomField.objects.filter(id=field.id).exists()

        _run()
        assert not CustomField.objects.filter(id=field.id).exists()
        assert not CustomFieldValue.objects.filter(custom_recap=demo, value="9", created_by=self.sys).exists()

        field = packs_field()
        real = self._recap(self.ww, "ba.real@example.com")
        CustomFieldValue.objects.create(custom_recap=real, custom_field=field, value="2", created_by=self.sys)
        log = _run()
        assert f"kept dropped field {PACKS!r} (1 submitted answer(s))" in log
        assert CustomFieldValue.objects.filter(custom_recap=real, custom_field=field).exists()
