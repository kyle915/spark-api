"""Products Sampled options resolve from the tenant Product catalog at read time."""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Product, ProductType
from recaps import models as recap_models
from recaps.products_sampled import (
    is_products_sampled_field,
    products_sampled_options_for_tenant,
    resolve_products_sampled_options,
)


EVENT_Q = """
query Ev($uuid: ID!) {
  event(uuid: $uuid) {
    customRecapTemplate {
      customField { name options }
    }
  }
}
"""


def test_is_products_sampled_field_is_case_insensitive():
    assert is_products_sampled_field("Products Sampled")
    assert is_products_sampled_field("products sampled")
    assert not is_products_sampled_field("Which products were sampled?")
    assert not is_products_sampled_field("Market")


@pytest.mark.django_db(transaction=True)
class TestProductsSampledHelpers(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Catalog Labels Co")

    def test_catalog_labels_are_type_emdash_name(self):
        pt = ProductType.objects.create(
            tenant=self.tenant, name="Sparkling Water", created_by=self.system_user
        )
        Product.objects.create(
            tenant=self.tenant,
            product_type=pt,
            name="Feastables Peanut Butter Cup",
            created_by=self.system_user,
        )
        Product.objects.create(
            tenant=self.tenant,
            product_type=pt,
            name="Mt. Death",
            created_by=self.system_user,
        )
        opts = products_sampled_options_for_tenant(self.tenant)
        assert opts == [
            "Sparkling Water — Feastables Peanut Butter Cup",
            "Sparkling Water — Mt. Death",
        ]

    def test_resolve_prefers_catalog_over_stale_stored(self):
        pt = ProductType.objects.create(
            tenant=self.tenant, name="Seltzer 10mg", created_by=self.system_user
        )
        Product.objects.create(
            tenant=self.tenant,
            product_type=pt,
            name="Watermelon Limeade 10mg 12oz",
            created_by=self.system_user,
        )
        live = resolve_products_sampled_options(
            field_name="Products Sampled",
            tenant=self.tenant,
            stored=["Old SKU Only"],
        )
        assert live == ["Seltzer 10mg — Watermelon Limeade 10mg 12oz"]

    def test_resolve_falls_back_when_catalog_empty(self):
        empty = self.create_tenant(name="No Catalog Co")
        assert resolve_products_sampled_options(
            field_name="Products Sampled",
            tenant=empty,
            stored=["Brew Dr. Ginger Lemon", "Brew Dr. Superberry"],
        ) == ["Brew Dr. Ginger Lemon", "Brew Dr. Superberry"]

    def test_non_sampled_field_keeps_stored(self):
        pt = ProductType.objects.create(
            tenant=self.tenant, name="Line", created_by=self.system_user
        )
        Product.objects.create(
            tenant=self.tenant,
            product_type=pt,
            name="SKU",
            created_by=self.system_user,
        )
        assert resolve_products_sampled_options(
            field_name="Market",
            tenant=self.tenant,
            stored=["Detroit", "Lansing"],
        ) == ["Detroit", "Lansing"]


@pytest.mark.django_db(transaction=True)
class TestProductsSampledOptionsViaSchema(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.system_user = self.get_system_user()
        self.admin = self.create_user(
            username="ps-admin",
            email="ps@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.tenant = self.create_tenant(name="Torch Catalog Pills")
        self.event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.multiselect_type = recap_models.CustomRecapFieldType.objects.create(
            name="multiselect", created_by=self.system_user
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="T",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Products Sampled",
            tenant=self.tenant,
            created_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_products_sampled_options_come_from_catalog(self):
        def _seed():
            pt = ProductType.objects.create(
                tenant=self.tenant,
                name="Seltzer 10mg",
                created_by=self.system_user,
            )
            Product.objects.create(
                tenant=self.tenant,
                product_type=pt,
                name="Black Cherry 10mg 12oz",
                created_by=self.system_user,
            )
            Product.objects.create(
                tenant=self.tenant,
                product_type=pt,
                name="Strawberry Lemonade 10mg 12oz",
                created_by=self.system_user,
            )
            recap_models.CustomField.objects.create(
                name="Products Sampled",
                custom_recap_template=self.template,
                custom_field_type=self.multiselect_type,
                recap_section=self.section,
                created_by=self.system_user,
                # Stale — must NOT be what GraphQL returns.
                options=["Legacy Only SKU"],
            )
            return self.create_event(
                name="Catalog pills event",
                tenant=self.tenant,
                event_type=self.event_type,
            )

        event = await sync_to_async(_seed)()
        res = await self._execute_mutation(
            EVENT_Q, {"uuid": str(event.uuid)}, user=self.admin
        )
        assert res.errors is None, res.errors
        fld = next(
            f
            for f in res.data["event"]["customRecapTemplate"]["customField"]
            if f["name"] == "Products Sampled"
        )
        assert fld["options"] == [
            "Seltzer 10mg — Black Cherry 10mg 12oz",
            "Seltzer 10mg — Strawberry Lemonade 10mg 12oz",
        ]
        assert "Legacy Only SKU" not in fld["options"]

    @pytest.mark.asyncio
    async def test_other_multiselect_still_uses_stored_options(self):
        def _seed():
            recap_models.CustomField.objects.create(
                name="What market is this?",
                custom_recap_template=self.template,
                custom_field_type=self.multiselect_type,
                recap_section=self.section,
                created_by=self.system_user,
                options=["Detroit", "Grand Rapids"],
            )
            return self.create_event(
                name="Market event",
                tenant=self.tenant,
                event_type=self.event_type,
            )

        event = await sync_to_async(_seed)()
        res = await self._execute_mutation(
            EVENT_Q, {"uuid": str(event.uuid)}, user=self.admin
        )
        assert res.errors is None, res.errors
        fld = next(
            f
            for f in res.data["event"]["customRecapTemplate"]["customField"]
            if f["name"] == "What market is this?"
        )
        assert fld["options"] == ["Detroit", "Grand Rapids"]
