import strawberry
from typing import List

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class BaseTenantInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None


@strawberry.input
class BaseNameableInput(BaseTenantInput):
    name: str


@strawberry.input
class CreateEventInput(BaseNameableInput):
    event_type_id: strawberry.ID
    status_id: strawberry.ID


@strawberry.input
class UpdateEventInput(CreateEventInput):
    id: strawberry.ID


@strawberry.input
class CreateEventTypeInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateEventTypeInput(CreateEventTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateEventStatusInput(BaseNameableInput):
    pass


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
    create_event: bool
    is_default: bool


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
    address: str
    coordinates: List[float]
    client_id: strawberry.ID
    distributor_id: strawberry.ID
    retailer_id: strawberry.ID
    request_type_id: strawberry.ID


@strawberry.input
class UpdateRequestInput(CreateRequestInput):
    id: strawberry.ID
