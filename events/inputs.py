import strawberry
from typing import List
from enum import Enum

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class BaseTenantInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None


@strawberry.input
class EventTypeFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class EventStatusFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.enum
class DistanceUnit(str, Enum):
    """Distance unit for coordinate-based queries."""

    KILOMETERS = "km"
    MILES = "mi"


@strawberry.input
class CoordinatesFilterInput:
    coordinates: List[float]
    range: float
    unit: DistanceUnit = DistanceUnit.KILOMETERS


@strawberry.enum
class EventStatusFilterEnum(str, Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"
    PENDING = "pending"


@strawberry.input
class EventFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None
    rmm_asigned: strawberry.ID | None = None
    event_type_id: strawberry.ID | None = None
    event_status: EventStatusFilterEnum | None = None
    request_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    distributor_id: strawberry.ID | None = None
    retailer_state_id: strawberry.ID | None = None
    distributor_state_id: strawberry.ID | None = None
    date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    coordinates: CoordinatesFilterInput | None = None
    edited: bool | None = None


@strawberry.input
class AmbassadorEventFiltersInput:
    start_date: str | None = None
    end_date: str | None = None


@strawberry.input
class RequestFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None
    rmm_asigned: strawberry.ID | None = None
    status_id: strawberry.ID | None = None
    client_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    distributor_id: strawberry.ID | None = None
    request_type_id: strawberry.ID | None = None
    retailer_state_id: strawberry.ID | None = None
    distributor_state_id: strawberry.ID | None = None
    date: str | None = None
    edited: bool | None = None


@strawberry.input
class RequestStoreManagerFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class ClientFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class LocationFiltersInput(SparkGraphQLInput):
    state_id: strawberry.ID | None = None


@strawberry.input
class DistributorFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None


@strawberry.input
class RetailerFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None
    is_national: bool | None = None


@strawberry.input
class RequestTypeFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class RequestStatusFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class ProductTypeFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class ProductFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None
    product_type_id: strawberry.ID | None = None


@strawberry.input
class BaseNameableInput(BaseTenantInput):
    name: str


@strawberry.input
class BaseNameOnlyInput(SparkGraphQLInput):
    name: str


@strawberry.input
class CreateEventInput(BaseNameableInput):
    event_type_id: strawberry.ID
    request_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    distributor_id: strawberry.ID | None = None
    rmm_asigned_id: strawberry.ID | None = None
    timezone_id: strawberry.ID | None = None
    date: str
    address: str
    notes: str
    is_national: bool = False
    coordinates: List[float] | None = None
    start_time: str
    end_time: str


@strawberry.input
class UpdateEventInput(CreateEventInput):
    id: strawberry.ID


@strawberry.input
class SuspendEventInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class ArchiveEventInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class ApproveEventInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class DeclineEventInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class CreateEventTypeInput(BaseNameableInput):
    is_default: bool = False


@strawberry.input
class UpdateEventTypeInput(CreateEventTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateEventStatusInput(BaseNameableInput):
    is_default: bool = False


@strawberry.input
class UpdateEventStatusInput(CreateEventStatusInput):
    id: strawberry.ID


@strawberry.input
class CreateLocationInput(BaseNameOnlyInput):
    code: str
    zip: str
    state_id: strawberry.ID | None = None


@strawberry.input
class UpdateLocationInput(CreateLocationInput):
    id: strawberry.ID


@strawberry.input
class CreateClientInput(BaseNameableInput):
    email: str


@strawberry.input
class UpdateClientInput(CreateClientInput):
    id: strawberry.ID


@strawberry.input
class CreateDistributorInput(BaseNameableInput):
    email: str
    location_id: strawberry.ID


@strawberry.input
class UpdateDistributorInput(CreateDistributorInput):
    id: strawberry.ID


@strawberry.input
class CreateRetailerInput(BaseNameableInput):
    address: str
    store_contact: str
    location_id: strawberry.ID
    is_national: bool = False


@strawberry.input
class UpdateRetailerInput(CreateRetailerInput):
    id: strawberry.ID


@strawberry.input
class CreateProductTypeInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateProductTypeInput(CreateProductTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateProductInput(BaseNameableInput):
    product_type_id: strawberry.ID
    image: str | None = None


@strawberry.input
class UpdateProductInput(CreateProductInput):
    id: strawberry.ID


@strawberry.input
class DeleteProductInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class CreateRequestStatusInput(BaseNameableInput):
    create_event: bool = False
    is_default: bool = False


@strawberry.input
class UpdateRequestStatusInput(CreateRequestStatusInput):
    id: strawberry.ID


@strawberry.input
class CreateRequestTypeInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateRequestTypeInput(CreateRequestTypeInput):
    id: strawberry.ID


@strawberry.input
class ApproveRequestInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class DeclineRequestInput(SparkGraphQLInput):
    id: strawberry.ID
    decline_reason: str | None = None


@strawberry.input
class CreateRequestDetailInput(SparkGraphQLInput):
    is_table_needed: bool = False
    table_size: int | None = None


@strawberry.input
class CreateRequestProductInput(SparkGraphQLInput):
    product_id: strawberry.ID


@strawberry.input
class CreateRequestInput(BaseNameableInput):
    date: str
    start_time: str
    end_time: str
    address: str
    notes: str | None = None
    coordinates: List[float]
    timezone_id: strawberry.ID
    request_type_id: strawberry.ID
    distributor_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    store_manager_id: strawberry.ID | None = None
    rmm_asigned_id: strawberry.ID | None = None
    requestor_email: str | None = None
    client_name: str | None = None
    client_email: str | None = None
    distributor_name: str | None = None
    distributor_email: str | None = None
    retailer_name: str | None = None
    retailer_address: str | None = None
    retailer_store_contact: str | None = None
    store_manager_name: str | None = None
    store_manager_phone: str | None = None
    details: List[CreateRequestDetailInput]
    products: List[CreateRequestProductInput]


@strawberry.input
class UpdateRequestInput(BaseNameableInput):
    id: strawberry.ID
    date: str
    start_time: str
    end_time: str
    address: str
    notes: str | None = None
    coordinates: List[float]
    timezone_id: strawberry.ID
    request_type_id: strawberry.ID
    distributor_id: strawberry.ID
    retailer_id: strawberry.ID
    rmm_asigned_id: strawberry.ID | None = None
    requestor_email: str | None = None
    store_manager_name: str
    store_manager_phone: str
    details: List[CreateRequestDetailInput]
    products: List[CreateRequestProductInput]


@strawberry.input
class CreateRequestWithDependenciesInput(BaseNameableInput):
    date: str
    start_time: str
    end_time: str
    address: str
    notes: str | None = None
    coordinates: List[float]
    timezone_id: strawberry.ID
    request_type_id: strawberry.ID
    distributor_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    requestor_email: str | None = None
    client_name: str
    client_email: str
    distributor_name: str
    distributor_email: str
    retailer_name: str
    retailer_address: str
    retailer_store_contact: str
    store_manager_name: str
    store_manager_phone: str
    details: List[CreateRequestDetailInput]
    products: List[CreateRequestProductInput]


@strawberry.input
class ImportRequestsBatchInput(BaseTenantInput):
    file: str
    default_timezone_id: strawberry.ID | None = None
    default_request_type_id: strawberry.ID | None = None
    sheet_name: str = "0"
    dry_run: bool = False
    rollback_on_error: bool = True


@strawberry.input
class RequestBatchTemplateInput(BaseTenantInput):
    pass


@strawberry.input
class CreateRequestStoreManagerInput(BaseTenantInput):
    name: str
    phone: str
    request_id: strawberry.ID | None = None


@strawberry.input
class UpdateRequestStoreManagerInput(CreateRequestStoreManagerInput):
    id: strawberry.ID


@strawberry.input
class CreateTimeZoneInput(BaseNameableInput):
    code: str
    offset: int


@strawberry.input
class UpdateTimeZoneInput(CreateTimeZoneInput):
    id: strawberry.ID
