"""Rename a product or product type and relabel filed Products Sampled answers."""

from __future__ import annotations

import json

import pytest
from graphql import GraphQLError

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.rename_catalog_labels import rename_catalog_label


@pytest.mark.django_db(transaction=True)
class TestRenameCatalogLabels(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Label Brew", slug="label-brew")
        self.event_type = self.create_event_type(
            name="Retail Sampling", tenant=self.tenant
        )
        recap_models.CustomRecapFieldType.objects.get_or_create(
            name="multiselect", defaults={"created_by": self.system_user}
        )
        self.field_type = recap_models.CustomRecapFieldType.objects.get(
            name="multiselect"
        )
        self.product_type = event_models.ProductType.objects.create(
            name="Kombucha",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.classic = event_models.Product.objects.create(
            name="Classic",
            product_type=self.product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.peach = event_models.Product.objects.create(
            name="Peach",
            product_type=self.product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Retail recap",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.section = recap_models.RecapSection.objects.create(
            tenant=self.tenant, name="Products", created_by=self.system_user
        )
        self.sampled = recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=self.section,
            name="Products Sampled",
            custom_field_type=self.field_type,
            options=["Kombucha — Classic", "Kombucha — Peach", "Classic"],
            created_by=self.system_user,
        )
        self.notes = recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=self.section,
            name="Feedback",
            custom_field_type=self.field_type,
            created_by=self.system_user,
        )
        self.event = event_models.Event.objects.create(
            name="Store",
            tenant=self.tenant,
            address="1 Main",
            created_by=self.system_user,
        )
        self.recap = recap_models.CustomRecap.objects.create(
            name="Filed",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
        )
        self.sampled_value = recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap,
            custom_field=self.sampled,
            value=json.dumps(
                [
                    "Kombucha — Classic",
                    "Kombucha – Peach",
                    "Classic",
                    {"sku": "Kombucha - Classic", "qty": 4},
                ]
            ),
            created_by=self.system_user,
        )
        self.notes_value = recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap,
            custom_field=self.notes,
            value="Kombucha — Classic",
            created_by=self.system_user,
        )

    def test_product_rename_rewrites_labels_and_keeps_qty(self):
        changed = rename_catalog_label(
            user=self.system_user,
            product_id=self.classic.id,
            product_type_id=None,
            name="Iced Tea Classic",
        )
        assert changed == 1
        self.classic.refresh_from_db()
        assert self.classic.name == "Iced Tea Classic"
        self.peach.refresh_from_db()
        assert self.peach.name == "Peach"
        self.sampled_value.refresh_from_db()
        stored = json.loads(self.sampled_value.value)
        assert stored[0] == "Kombucha — Iced Tea Classic"
        assert stored[1] == "Kombucha – Peach"
        assert stored[2] == "Iced Tea Classic"
        assert stored[3] == {"sku": "Kombucha - Iced Tea Classic", "qty": 4}
        self.notes_value.refresh_from_db()
        assert self.notes_value.value == "Kombucha — Classic"
        self.sampled.refresh_from_db()
        assert "Kombucha — Iced Tea Classic" in self.sampled.options
        assert "Classic" not in self.sampled.options

    def test_type_rename_rewrites_prefix_only(self):
        changed = rename_catalog_label(
            user=self.system_user,
            product_id=None,
            product_type_id=self.product_type.id,
            name="Brew Dr Kombucha Iced Tea",
        )
        assert changed == 1
        self.product_type.refresh_from_db()
        assert self.product_type.name == "Brew Dr Kombucha Iced Tea"
        self.classic.refresh_from_db()
        assert self.classic.name == "Classic"
        self.sampled_value.refresh_from_db()
        stored = json.loads(self.sampled_value.value)
        assert stored[0] == "Brew Dr Kombucha Iced Tea — Classic"
        assert stored[1] == "Brew Dr Kombucha Iced Tea – Peach"
        assert stored[2] == "Classic"
        assert stored[3]["qty"] == 4
        assert stored[3]["sku"] == "Brew Dr Kombucha Iced Tea - Classic"

    def test_rejects_a_second_sku(self):
        with pytest.raises(GraphQLError):
            rename_catalog_label(
                user=self.system_user,
                product_id=self.classic.id,
                product_type_id=self.product_type.id,
                name="Nope",
            )
        self.classic.refresh_from_db()
        assert self.classic.name == "Classic"
