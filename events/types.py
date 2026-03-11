from __future__ import annotations

from typing import List

import strawberry
import strawberry_django
from strawberry.relay import Node

import datetime
from django.utils import timezone
from asgiref.sync import sync_to_async
from tenants.types import SparkUserType, TenantType
from utils.gcs import extract_blob_name_from_url, generate_download_url

from . import models


def _serialize_dt(value, offset_minutes: int = 0):
    """Serialize datetime applying explicit offset (minutes) and no server TZ conversion."""
    if not value:
        return None
    # Normalize to UTC to remove server TZ influence
    if hasattr(value, "tzinfo") and timezone.is_aware(value):
        value = value.astimezone(datetime.timezone.utc)
    # If naive, assume stored in UTC
    if not hasattr(value, "tzinfo") or not timezone.is_aware(value):
        value = value.replace(tzinfo=datetime.timezone.utc)
    # Apply desired offset (event/request timezone)
    value = value + datetime.timedelta(minutes=offset_minutes)
    return value.replace(tzinfo=None).isoformat()


def _get_field(instance, name: str):
    """Safely fetch a model field value, bypassing descriptor overrides."""
    try:
        field = instance._meta.get_field(name)
        return field.value_from_object(instance)
    except Exception:
        return None


def _get_offset_minutes_from_instance(instance) -> int:
    """Return timezone offset in minutes without extra queries."""
    try:
        tz = getattr(instance, "timezone", None)
        return int(tz.offset) if tz and tz.offset is not None else 0
    except Exception:
        return 0


@strawberry_django.type(models.EventType)
class EventType(Node):
    uuid: str
    name: str
    is_default: bool
    slug: str | None
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
class TimeZone(Node):
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
class EventStatus(Node):
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


@strawberry_django.type(models.State)
class State(Node):
    uuid: str
    name: str
    code: str
    created_at: str
    updated_at: str


@strawberry_django.type(models.Location)
class Location(Node):
    uuid: str
    name: str
    code: str
    zip: str
    state: State | None = None
    created_at: str
    updated_at: str


@strawberry.type
class LocationDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    location: Location | None = None


@strawberry_django.type(models.Client)
class Client(Node):
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
class Distributor(Node):
    uuid: str
    name: str
    email: str | None
    tenant_id: strawberry.ID
    location: Location | None = None
    state: State | None = None
    created_at: str
    updated_at: str


@strawberry.type
class DistributorDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    distributor: Distributor | None = None


@strawberry_django.type(models.Retailer)
class Retailer(Node):
    uuid: str
    name: str
    address: str | None
    store_contact: str | None
    is_national: bool
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
class ProductType(Node):
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
class Product(Node):
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
class RequestType(Node):
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


@strawberry_django.type(models.RequestStatus)
class RequestStatus(Node):
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
class Request(Node):
    uuid: str

    @strawberry.field
    def name(self) -> str:
        return _get_field(self, "name") or ""

    # Date/time fields returned as stored, without server TZ conversion
    @strawberry.field
    def date(self) -> str | None:
        return _serialize_dt(_get_field(self, "date"), offset_minutes=0)

    @strawberry.field
    def start_time(self) -> str | None:
        offset = _get_offset_minutes_from_instance(self)
        return _serialize_dt(_get_field(self, "start_time"), offset_minutes=offset)

    @strawberry.field
    def end_time(self) -> str | None:
        offset = _get_offset_minutes_from_instance(self)
        return _serialize_dt(_get_field(self, "end_time"), offset_minutes=offset)

    address: str
    decline_reason: str | None = None
    reviewed: bool
    store_number: str | None = None
    notes: str | None = None
    coordinates: List[float]
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
    timezone: TimeZone | None = None
    client: Client | None = None
    distributor: Distributor | None = None
    retailer: Retailer | None = None

    @strawberry.field
    def store_managers(self) -> List[RequestStoreManager]:
        cached = getattr(self, "_prefetched_objects_cache", {}).get(
            "requests_stores_manager"
        )
        if cached is not None:
            return list(cached)
        return list(self.requests_stores_manager.all())

    @strawberry.field
    async def products(self) -> List[Product]:
        cached = getattr(self, "_prefetched_objects_cache", {}).get("request_product")
        if cached is not None:
            return [item.product for item in cached if item.product]
        items = await sync_to_async(list)(
            self.request_product.select_related("product").all()
        )
        return [item.product for item in items if item.product]

    @strawberry.field
    async def event(self) -> Event | None:
        cached = getattr(self, "_prefetched_objects_cache", {}).get("event_set")
        if cached is not None:
            return cached[0] if cached else None
        return await sync_to_async(self.event_set.first)()

    request_type: RequestType | None = None
    status: RequestStatus | None = None
    tenant_id: strawberry.ID
    tenant: TenantType | None = None
    rmm_asigned: SparkUserType | None = None
    created_by: SparkUserType | None = None
    updated_by: SparkUserType | None = None
    approved_by: SparkUserType | None = None
    created_at: str
    updated_at: str


@strawberry_django.type(models.RequestStoreManager)
class RequestStoreManager(Node):
    uuid: str
    name: str
    phone: str
    request_id: strawberry.ID | None = None
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
class RequestBatchRowResult:
    row_number: int
    success: bool
    message: str
    request_id: strawberry.ID | None = None
    request_uuid: str | None = None


@strawberry.type
class RequestBatchImportResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    total_rows: int
    success_count: int
    failed_count: int
    rolled_back: bool
    errors: List[str]
    rows: List[RequestBatchRowResult]


@strawberry.type
class RequestBatchTemplateResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    file_url: str | None = None


@strawberry.type
class RequestListResponse:
    total_pages: int
    requests: List[Request]


@strawberry_django.type(models.Event)
class Event(Node):
    uuid: str
    coordinates: List[float] | None = None
    address: str
    is_national: bool
    notes: str | None = None
    request: Request | None = None
    retailer: Retailer | None = None
    distributor: Distributor | None = None
    created_at: str
    updated_at: str
    tenant_id: strawberry.ID
    tenant: TenantType | None = None
    event_type: EventType | None = None
    status: EventStatus | None = None
    timezone: TimeZone | None = None
    rmm_asigned: SparkUserType | None = None

    @strawberry.field
    def tenant_image(self) -> str | None:
        """Return a signed URL for the tenant image if it exists."""
        if not self.tenant or not self.tenant.image:
            return None

        blob_name = extract_blob_name_from_url(self.tenant.image.name)
        if not blob_name:
            return None

        return generate_download_url(blob_name)

    @strawberry.field
    def name(self) -> str:
        event_name = _get_field(self, "name") or ""

        retailer_name = None
        fields_cache = getattr(self._state, "fields_cache", {})
        retailer = fields_cache.get("retailer")
        request = fields_cache.get("request")

        if retailer and getattr(retailer, "name", None):
            retailer_name = retailer.name
        elif request and getattr(request, "retailer_name", None):
            retailer_name = request.retailer_name
        elif request:
            request_fields_cache = getattr(request._state, "fields_cache", {})
            request_retailer = request_fields_cache.get("retailer")
            if request_retailer and getattr(request_retailer, "name", None):
                retailer_name = request_retailer.name

        if retailer_name:
            return f"{event_name} - {retailer_name}".strip()
        return event_name

    @strawberry.field
    def date(self) -> str | None:
        return _serialize_dt(_get_field(self, "date"), offset_minutes=0)

    @strawberry.field
    def start_time(self) -> str | None:
        offset = _get_offset_minutes_from_instance(self)
        return _serialize_dt(_get_field(self, "start_time"), offset_minutes=offset)

    @strawberry.field
    def end_time(self) -> str | None:
        offset = _get_offset_minutes_from_instance(self)
        return _serialize_dt(_get_field(self, "end_time"), offset_minutes=offset)


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
