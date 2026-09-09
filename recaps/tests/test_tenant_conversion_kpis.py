"""Torch-style conversion: products purchased ÷ consumers sampled / samples given."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_overview import tenant_conversion_kpis


@pytest.mark.django_db(transaction=True)
class TestTenantConversionSampledBase(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch Conv Co")
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
        self.event_type = self.create_request_type(
            "Event Activation", self.tenant
        )
        event_type = event_models.EventType.objects.create(
            name="et torch",
            slug="et-torch-conv",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch Retail",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.sys
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=29)
        self.end = self.today

    def _custom_recap(
        self,
        *,
        request_type,
        fields: list[tuple[str, str]],
        engagements: int = 0,
    ):
        when = timezone.make_aware(
            datetime(self.today.year, self.today.month, self.today.day, 12, 0)
        )
        req = self.create_request(
            name=f"req {request_type.name}",
            date=when,
            address="1 Main",
            client=self.client_row,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=request_type,
            tenant=self.tenant,
            status=self.status,
        )
        event = req.event_set.first() or self.create_event(
            name=f"ev {request_type.name}", tenant=self.tenant
        )
        if event.request_id != req.id:
            event.request = req
            event.save(update_fields=["request"])
        event_models.Event.objects.filter(id=event.id).update(date=when)
        recap = recap_models.CustomRecap.objects.create(
            name=f"recap {request_type.name}",
            approved=True,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            total_engagements=engagements,
            created_by=self.sys,
        )
        for name, value in fields:
            field = recap_models.CustomField.objects.create(
                name=name,
                required=False,
                custom_recap_template=self.template,
                custom_field_type=self.field_type,
                recap_section=self.section,
                created_by=self.sys,
            )
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap,
                custom_field=field,
                value=value,
                created_by=self.sys,
            )
        return recap

    def test_torch_style_sold_over_consumers_sampled(self):
        # Torch logs cans/packs purchased + consumers sampled. Typed
        # total_engagements may be 0 — CONV must still resolve.
        self._custom_recap(
            request_type=self.retail_type,
            engagements=0,
            fields=[
                ("Total number of consumers sampled", "87"),
                ("How many single cans did consumers purchase?", "34"),
                ("How many packs did consumers purchase?", "12"),
            ],
        )
        data = tenant_conversion_kpis(
            self.tenant.id, start=self.start, end=self.end
        )
        assert data["sold"] == 46  # 34 cans + 12 packs
        assert data["engagements"] == 87  # sampled base (GraphQL field name)
        assert data["pct"] == round((46 / 87) * 100, 1)

    def test_samples_given_preferred_over_consumers_sampled(self):
        self._custom_recap(
            request_type=self.retail_type,
            fields=[
                ("Total Samples Given Out", "95"),
                ("Consumers Sampled", "90"),
                ("Cans Sold", "20"),
            ],
        )
        data = tenant_conversion_kpis(
            self.tenant.id, start=self.start, end=self.end
        )
        assert data["sold"] == 20
        assert data["engagements"] == 95
        assert data["pct"] == round((20 / 95) * 100, 1)

    def test_event_activation_excluded(self):
        self._custom_recap(
            request_type=self.event_type,
            fields=[
                ("Total number of consumers sampled", "100"),
                ("How many single cans did consumers purchase?", "50"),
            ],
        )
        data = tenant_conversion_kpis(
            self.tenant.id, start=self.start, end=self.end
        )
        assert data["sold"] == 0
        assert data["engagements"] == 0
        assert data["pct"] is None
