import strawberry_django
import strawberry
from typing import List

from . import models


@strawberry_django.type(models.EventType)
class EventType:
    id: strawberry.ID
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class EventTypeDetailResponse:
    success: bool
    message: str
    event_type: EventType | None = None


@strawberry_django.type(models.EventStatus)
class EventStatus:
    id: strawberry.ID
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class EventStatusDetailResponse:
    success: bool
    message: str
    event_status: EventStatus | None = None


@strawberry_django.type(models.Event)
class Event:
    id: strawberry.ID
    uuid: str
    name: str
    created_at: str
    updated_at: str
    tenant_id: strawberry.ID
    event_type: EventType | None = None
    status: EventStatus | None = None


@strawberry.type
class EventDetailResponse:
    success: bool
    message: str
    event: Event | None = None


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


@strawberry.type
class ProductDetailResponse:
    success: bool
    message: str
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
    request_type: RequestType | None = None


@strawberry.type
class RequestStatus:
    id: strawberry.ID
    uuid: str
    name: str
    create_event: bool
    is_default: bool
    created_at: str
    updated_at: str


@strawberry.type
class RequestStatusDetailResponse:
    success: bool
    message: str
    request_status: RequestStatus | None = None


@strawberry_django.type(models.Request)
class Request:
    id: strawberry.ID
    uuid: str
    name: str
    date: str
    address: str
    coordinates: List[float]
    client: Client | None = None
    distributor: Distributor | None = None
    retailer: Retailer | None = None
    request_type: RequestType | None = None
    status: RequestStatus | None = None
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class RequestDetailResponse:
    success: bool
    message: str
    request: Request | None = None
