import pytest

from events.batch_requests import _parse_row
from events.models import BillingEntity, EventType, Location, State, TimeZone
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
        self.billing_entity = BillingEntity.objects.create(
            name="Acme Billing",
            state=self.state,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def test_parse_row_uses_excel_name_verbatim_when_present(self):
        # As of #578 the client's "name" column is used verbatim — their import
        # files already contain a fully-composed name. Retailer / date / store
        # are NOT appended when a name is present.
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
                "scheduling_status": "already_scheduled",
            },
            tenant_id=self.tenant.id,
            tenant_name=self.tenant.name,
            default_timezone_id=None,
            default_request_type_id=None,
        )

        assert parsed["name"] == "PromoRequest"

    def test_parse_row_composes_name_with_retailer_when_name_blank(self):
        # Only when the name column is blank do we compose one from the
        # identifying fields: "Request-<retailer>-<date>-<store>".
        parsed = _parse_row(
            row={
                "name": "",
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
                "scheduling_status": "already_scheduled",
            },
            tenant_id=self.tenant.id,
            tenant_name=self.tenant.name,
            default_timezone_id=None,
            default_request_type_id=None,
        )

        assert parsed["name"] == "Request-Target-02/20/2026-102"

    def test_parse_row_composed_name_omits_retailer_segment_when_missing(self):
        # Composed (blank name) + no retailer/distributor → retailer segment is
        # omitted: "Request-<date>-<store>".
        parsed = _parse_row(
            row={
                "name": "",
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
                "scheduling_status": "already_scheduled",
            },
            tenant_id=self.tenant.id,
            tenant_name=self.tenant.name,
            default_timezone_id=None,
            default_request_type_id=None,
        )

        assert parsed["name"] == "Request-03/15/2026-102"

    def test_parse_row_accepts_optional_billing_entity_id(self):
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
                "scheduling_status": "already_scheduled",
                "billing_entity_id": self.billing_entity.id,
            },
            tenant_id=self.tenant.id,
            tenant_name=self.tenant.name,
            default_timezone_id=None,
            default_request_type_id=None,
        )

        assert parsed["billing_entity_id"] == self.billing_entity.id

    def test_parse_row_rejects_billing_entity_from_another_tenant(self):
        other_tenant = self.create_tenant(name="Other tenant")
        other_billing_entity = BillingEntity.objects.create(
            name="Other Billing",
            state=self.state,
            tenant=other_tenant,
            created_by=self.system_user,
        )

        with pytest.raises(
            ValueError,
            match=(
                f"billing_entity_id does not exist for tenant '{self.tenant.name}': "
                f"{other_billing_entity.id}"
            ),
        ):
            _parse_row(
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
                    "scheduling_status": "already_scheduled",
                    "billing_entity_id": other_billing_entity.id,
                },
                tenant_id=self.tenant.id,
                tenant_name=self.tenant.name,
                default_timezone_id=None,
                default_request_type_id=None,
            )
