"""Program pulse SKU rollup: approved structured samples only."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_overview import tenant_sku_pulse


@pytest.mark.django_db(transaction=True)
class TestTenantSkuPulse(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch SKU Co")
        self.location = self.create_location(
            name="HQ", code="HQ", zip_code="94105", tenant=self.tenant
        )
        self.client_row = self.create_client(
            name="Brand", email="brand@example.com", tenant=self.tenant
        )
        self.distributor = self.create_distributor(
            name="Distro",
            email="distro@example.com",
            location=self.location,
            tenant=self.tenant,
        )
        self.retailer = self.create_retailer(
            name="Total Wine",
            address="1 Main",
            store_contact="mgr",
            location=self.location,
            tenant=self.tenant,
        )
        self.status = self.create_request_status(
            name="Scheduled", tenant=self.tenant, create_event=True
        )
        self.retail_type = self.create_request_type(
            "Retail Sampling", self.tenant
        )
        event_type = event_models.EventType.objects.create(
            name="et torch sku",
            slug="et-torch-sku",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch Retail",
            event_type=event_type,
            tenant=self.tenant,
            product_samples=True,
            created_by=self.sys,
        )
        self.product_type = event_models.ProductType.objects.create(
            name="5MG",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.cola = event_models.Product.objects.create(
            name="Berry Blast",
            product_type=self.product_type,
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.lemon = event_models.Product.objects.create(
            name="Lemon Lime",
            product_type=self.product_type,
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=29)
        self.end = self.today

    def _custom_recap(self, *, approved: bool = True):
        when = timezone.make_aware(
            datetime(self.today.year, self.today.month, self.today.day, 12, 0)
        )
        req = self.create_request(
            name="req retail",
            date=when,
            address="1 Main",
            client=self.client_row,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.retail_type,
            tenant=self.tenant,
            status=self.status,
        )
        event = req.event_set.first() or self.create_event(
            name="ev retail", tenant=self.tenant
        )
        if event.request_id != req.id:
            event.request = req
            event.save(update_fields=["request"])
        event_models.Event.objects.filter(id=event.id).update(date=when)
        return recap_models.CustomRecap.objects.create(
            name="recap retail",
            approved=approved,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            total_engagements=0,
            created_by=self.sys,
        )

    def test_sums_approved_structured_samples_by_catalog_sku(self):
        r1 = self._custom_recap(approved=True)
        r2 = self._custom_recap(approved=True)
        recap_models.CustomRecapProductSample.objects.create(
            custom_recap=r1,
            product=self.cola,
            quantity=40,
            created_by=self.sys,
        )
        recap_models.CustomRecapProductSample.objects.create(
            custom_recap=r1,
            product=self.lemon,
            quantity=10,
            created_by=self.sys,
        )
        recap_models.CustomRecapProductSample.objects.create(
            custom_recap=r2,
            product=self.cola,
            quantity=20,
            created_by=self.sys,
        )

        data = tenant_sku_pulse(self.tenant.id, start=self.start, end=self.end)
        assert data["mode"] == "quantity"
        assert data["total_samples"] == 70
        assert data["items"][0] == {
            "product": "5MG — Berry Blast",
            "samples": 60,
        }
        assert data["items"][1] == {
            "product": "5MG — Lemon Lime",
            "samples": 10,
        }

    def test_excludes_unapproved_recaps(self):
        draft = self._custom_recap(approved=False)
        recap_models.CustomRecapProductSample.objects.create(
            custom_recap=draft,
            product=self.cola,
            quantity=99,
            created_by=self.sys,
        )
        data = tenant_sku_pulse(self.tenant.id, start=self.start, end=self.end)
        assert data["mode"] == "none"
        assert data["items"] == []
        assert data["total_samples"] == 0

    def test_none_when_no_structured_samples(self):
        self._custom_recap(approved=True)
        data = tenant_sku_pulse(self.tenant.id, start=self.start, end=self.end)
        assert data == {
            "mode": "none",
            "items": [],
            "total_samples": 0,
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
        }
