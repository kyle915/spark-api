import strawberry
from typing import List

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


@strawberry.input
class EventFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None
    status_id: strawberry.ID | None = None


@strawberry.input
class RequestFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None
    status_id: strawberry.ID | None = None
    client_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    distributor_id: strawberry.ID | None = None
    date: str | None = None


@strawberry.input
class ClientFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class LocationFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class DistributorFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class RetailerFiltersInput(BaseTenantInput):
    tenant_uuid: strawberry.ID | None = None


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


@strawberry.input
class BaseNameableInput(BaseTenantInput):
    name: str


@strawberry.input
class CreateEventInput(BaseNameableInput):
    event_type_id: strawberry.ID
    status_id: strawberry.ID
    request_id: strawberry.ID
    address: str
    notes: str
    is_national: bool = False
    start_time: str
    end_time: str


@strawberry.input
class UpdateEventInput(CreateEventInput):
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
class CreateLocationInput(BaseNameableInput):
    code: str
    zip: str


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


@strawberry.input
class UpdateProductInput(CreateProductInput):
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
class CreateRequestInput(BaseNameableInput):
    date: str
    start_time: str
    end_time: str
    address: str
    coordinates: List[float]
    client_id: strawberry.ID
    distributor_id: strawberry.ID
    retailer_id: strawberry.ID
    request_type_id: strawberry.ID


@strawberry.input
class UpdateRequestInput(CreateRequestInput):
    id: strawberry.ID


@strawberry.input
class ApproveRequestInput(SparkGraphQLInput):
    id: strawberry.ID
