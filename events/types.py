import strawberry_django
import strawberry
from typing import List

from . import models


@strawberry_django.type(models.EventType)
class EventType:
    id: strawberry.ID
    uuid: str
    name: str
    is_default: bool
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class EventTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event_type: EventType | None = None


@strawberry_django.type(models.TimeZone)
class TimeZone:
    id: strawberry.ID
    uuid: str
    name: str
    code: str
    offset: int
    created_at: str
    updated_at: str


@strawberry.type
class TimeZoneResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    timezone: TimeZone | None = None


@strawberry_django.type(models.EventStatus)
class EventStatus:
    id: strawberry.ID
    uuid: str
    name: str
    slug: str
    is_default: bool
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class EventStatusDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event_status: EventStatus | None = None


@strawberry_django.type(models.Location)
class Location:
    id: strawberry.ID
    uuid: str
    name: str
    code: str
    zip: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class LocationDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    location: Location | None = None


@strawberry_django.type(models.Client)
class Client:
    id: strawberry.ID
    uuid: str
    name: str
    email: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class ClientDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    client: Client | None = None


@strawberry_django.type(models.Distributor)
class Distributor:
    id: strawberry.ID
    uuid: str
    name: str
    email: str
    tenant_id: strawberry.ID
    location: Location | None = None
    created_at: str
    updated_at: str


@strawberry.type
class DistributorDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    distributor: Distributor | None = None


@strawberry_django.type(models.Retailer)
class Retailer:
    id: strawberry.ID
    uuid: str
    name: str
    address: str
    store_contact: str
    tenant_id: strawberry.ID
    location: Location | None = None
    created_at: str
    updated_at: str


@strawberry.type
class RetailerDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    retailer: Retailer | None = None


@strawberry_django.type(models.ProductType)
class ProductType:
    id: strawberry.ID
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class ProductTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    product_type: ProductType | None = None


@strawberry_django.type(models.Product)
class Product:
    id: strawberry.ID
    uuid: str
    name: str
    product_type: ProductType | None = None
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str
    
    @strawberry.field
    def image(self) -> str | None:
        """Return a signed URL for the product image if it exists."""
        if not self.image:
            return None
        from utils.gcs import generate_download_url
        # The image field contains the path in GCS (e.g., "products/image.jpg")
        return generate_download_url(self.image.name)


@strawberry.type
class ProductDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    product: Product | None = None


@strawberry_django.type(models.RequestType)
class RequestType:
    id: strawberry.ID
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class RequestTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request_type: RequestType | None = None


@strawberry.type
class RequestStatus:
    id: strawberry.ID
    uuid: str
    name: str
    slug: str
    create_event: bool
    is_default: bool
    created_at: str
    updated_at: str


@strawberry.type
class RequestStatusDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request_status: RequestStatus | None = None


@strawberry_django.type(models.Request)
class Request:
    id: strawberry.ID
    uuid: str
    name: str
    date: str
    start_time: str | None = None
    end_time: str | None = None
    address: str
    coordinates: List[float]
    client_name: str | None = None
    client_email: str | None = None
    distributor_name: str | None = None
    distributor_email: str | None = None
    retailer_name: str | None = None
    retailer_address: str | None = None
    retailer_store_contact: str | None = None
    store_manager_name: str | None = None
    store_manager_phone: str | None = None
    timezone: TimeZone | None = None
    client: Client | None = None
    distributor: Distributor | None = None
    retailer: Retailer | None = None
    request_type: RequestType | None = None
    status: RequestStatus | None = None
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry_django.type(models.RequestStoreManager)
class RequestStoreManager:
    id: strawberry.ID
    uuid: str
    name: str
    phone: str
    request_id: strawberry.ID
    request: Request | None = None
    tenant_id: strawberry.ID | None = None
    created_at: str
    updated_at: str


@strawberry.type
class RequestStoreManagerDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request_store_manager: RequestStoreManager | None = None


@strawberry.type
class RequestDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request: Request | None = None


@strawberry.type
class RequestListResponse:
    total_pages: int
    requests: List[Request]


@strawberry_django.type(models.Event)
class Event:
    id: strawberry.ID
    uuid: str
    name: str
    coordinates: List[float]
    start_time: str | None = None
    end_time: str | None = None
    address: str
    is_national: bool
    notes: str | None = None
    request: Request | None = None
    created_at: str
    updated_at: str
    tenant_id: strawberry.ID
    event_type: EventType | None = None
    status: EventStatus | None = None


@strawberry.type
class EventListResponse:
    total_pages: int
    events: List[Event]


@strawberry.type
class EventDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event: Event | None = None


@strawberry.type
class ApproveRequestResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request: Request | None = None
    event: Event | None = None


@strawberry.type
class DeclineRequestResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request: Request | None = None
