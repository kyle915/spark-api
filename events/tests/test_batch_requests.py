import pytest

from events.batch_requests import _parse_row
from events.models import EventType, Location, State, TimeZone
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestBatchRequestNames(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant()

        self.timezone = TimeZone.objects.create(
            name="Central Standard Time",
            code="CST",
            offset=-6,
            created_by=self.system_user,
        )
        self.state = State.objects.create(
            name="Texas",
            code="TX",
            created_by=self.system_user,
        )
        self.location = Location.objects.create(
            name="Austin",
            code="AUS",
            zip="78701",
            state=self.state,
            created_by=self.system_user,
        )
        self.request_type = self.create_request_type(
            name="Sampling",
            tenant=self.tenant,
        )
        self.event_type = self.create_event_type(
            name="In Store",
            tenant=self.tenant,
        )
        self.retailer = self.create_retailer(
            name="Target",
            address="123 Main St",
            store_contact="Manager",
            location=self.location,
            tenant=self.tenant,
        )

    def test_parse_row_builds_name_with_excel_name_retailer_date_and_store_number(self):
        parsed = _parse_row(
            row={
                "name": "PromoRequest",
                "date": "02/20/2026",
                "start_time": "10:00",
                "end_time": "14:00",
                "address": "123 Main St",
                "store_number": "102",
                "retailer_name": self.retailer.name,
                "city": self.location.name,
                "state": self.state.code,
                "timezone_code": self.timezone.code,
                "request_type_id": self.request_type.id,
                "event_type_id": self.event_type.id,
            },
            tenant_id=self.tenant.id,
            tenant_name=self.tenant.name,
            default_timezone_id=None,
            default_request_type_id=None,
        )

        assert parsed["name"] == "PromoRequest-Target-02/20/2026-102"

    def test_parse_row_omits_retailer_segment_when_retailer_and_distributor_are_missing(self):
        parsed = _parse_row(
            row={
                "name": "PromoRequest",
                "date": "03/15/2026",
                "start_time": "10:00",
                "end_time": "14:00",
                "address": "123 Main St",
                "store_number": "102",
                "city": self.location.name,
                "state": self.state.code,
                "timezone_code": self.timezone.code,
                "request_type_id": self.request_type.id,
                "event_type_id": self.event_type.id,
            },
            tenant_id=self.tenant.id,
            tenant_name=self.tenant.name,
            default_timezone_id=None,
            default_request_type_id=None,
        )

        assert parsed["name"] == "PromoRequest-03/15/2026-102"
