"""Liquid Death Product Seeding template seeder."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestSeedLdProductSeeding(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death")

    def _run(self, **kw):
        out = io.StringIO()
        call_command(
            "seed_ld_product_seeding_recap_template",
            tenant="liquid death",
            stdout=out,
            **kw,
        )
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        from events.models import EventType
        from recaps.models import CustomRecapTemplate

        log = self._run()
        assert "DRY RUN" in log
        assert not EventType.objects.filter(
            tenant=self.tenant, name="Product Seeding"
        ).exists()
        assert not CustomRecapTemplate.objects.filter(
            tenant=self.tenant, name="Liquid Death-Product Seeding"
        ).exists()

    def test_apply_creates_template_and_fields(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate
        from recaps.tenant_overview import _activation_bucket_for_type_name

        log = self._run(apply=True)
        assert "Seeded" in log

        et = EventType.objects.get(tenant=self.tenant, name="Product Seeding")
        tpl = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name="Liquid Death-Product Seeding"
        )
        assert tpl.event_type_id == et.id
        assert tpl.product_samples is True

        names = set(
            CustomField.objects.filter(custom_recap_template=tpl).values_list(
                "name", flat=True
            )
        )
        assert names == {
            "Location product dropped",
            "Total cases dropped",
            "Total mileage",
            "Cases Dropped by SKU",
        }

        # Classifies as seeding (Recaps chip); never Retail / Event / CONV.
        key, _ = _activation_bucket_for_type_name(tpl.name)
        assert key == "seeding"
        key2, _ = _activation_bucket_for_type_name(et.name)
        assert key2 == "seeding"

    def test_apply_is_idempotent(self):
        from recaps.models import CustomField, CustomRecapTemplate

        self._run(apply=True)
        tpl = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name="Liquid Death-Product Seeding"
        )
        field_count = CustomField.objects.filter(
            custom_recap_template=tpl
        ).count()
        self._run(apply=True)
        assert (
            CustomRecapTemplate.objects.filter(
                tenant=self.tenant, name="Liquid Death-Product Seeding"
            ).count()
            == 1
        )
        assert (
            CustomField.objects.filter(custom_recap_template=tpl).count()
            == field_count
        )
