"""Standing walk-up product samples: tenant catalog fallback + pills wiring."""

from __future__ import annotations

import pytest

from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestCheckinEventProducts(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import Event, EventStatus, EventType, Product, ProductType
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.tenant = self.create_tenant(name="LD Qty Fixture")
        self.etype = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )
        status = EventStatus.objects.create(
            name="Approved", tenant=self.tenant, created_by=self.user
        )
        self.tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="LD-Retail Sampling",
            event_type=self.etype,
            product_samples=False,
            created_by=self.user,
        )
        multi = CustomRecapFieldType.objects.create(
            name="multiselect", created_by=self.user
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Products Sampled", order=1, created_by=self.user
        )
        CustomField.objects.create(
            custom_recap_template=self.tpl,
            recap_section=section,
            name="Products Sampled",
            custom_field_type=multi,
            required=False,
            options=["Sparkling Water — Mt. Death"],
            order=1,
            created_by=self.user,
        )
        ptype = ProductType.objects.create(
            name="Sparkling Water", tenant=self.tenant, created_by=self.user
        )
        self.product = Product.objects.create(
            name="Mt. Death",
            product_type=ptype,
            tenant=self.tenant,
            created_by=self.user,
        )
        # Standing walk-up: event with no Request → no RequestProduct rows.
        self.event = Event.objects.create(
            name="Walk-up demo",
            tenant=self.tenant,
            event_type=self.etype,
            status=status,
            created_by=self.user,
        )

    def test_event_products_falls_back_to_tenant_catalog(self):
        from ambassadors import checkin_web

        rows = checkin_web._event_products(self.event)
        assert len(rows) == 1
        assert rows[0]["id"] == str(self.product.id)
        assert rows[0]["name"] == "Mt. Death"
        assert rows[0]["category"] == "Sparkling Water"

    def test_serialize_template_includes_products_for_pills(self):
        from ambassadors import checkin_web

        # product_samples flag is still False — Products Sampled multiselect
        # alone must surface the catalog so the BA qty UI can match pills.
        payload = checkin_web.serialize_template(self.event)
        assert payload is not None
        assert payload["productSamples"] is False
        assert len(payload["products"]) == 1
        assert payload["products"][0]["name"] == "Mt. Death"
