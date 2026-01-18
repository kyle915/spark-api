from typing import List

import strawberry
import strawberry_django
from strawberry.relay import Node

import datetime
from django.utils import timezone
from tenants.types import TenantType
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


def _get_offset_minutes_from_timezone(tz_obj) -> int:
    """Return timezone offset in minutes from a TimeZone model (default 0)."""
    try:
        offset = getattr(tz_obj, "offset", None)
        return int(offset) if offset is not None else 0
    except Exception:
        return 0


def _get_related_from_cache(instance, field_name: str):
    """Return related object if already fetched, avoiding sync DB hits in async resolvers."""
    return instance.__dict__.get(field_name)


@strawberry_django.type(models.EventType)
class EventType(Node):
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


@strawberry_django.type(models.Location)
class Location(Node):
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
    name: str

    # Date/time fields returned as stored, without server TZ conversion
    @strawberry.field
    def date(self) -> str | None:
        offset = _get_offset_minutes_from_timezone(
            _get_related_from_cache(self, "timezone")
        )
        return _serialize_dt(_get_field(self, "date"), offset_minutes=offset)

    @strawberry.field
    def start_time(self) -> str | None:
        offset = _get_offset_minutes_from_timezone(
            _get_related_from_cache(self, "timezone")
        )
        return _serialize_dt(_get_field(self, "start_time"), offset_minutes=offset)

    @strawberry.field
    def end_time(self) -> str | None:
        offset = _get_offset_minutes_from_timezone(
            _get_related_from_cache(self, "timezone")
        )
        return _serialize_dt(_get_field(self, "end_time"), offset_minutes=offset)

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

    @strawberry.field
    def store_managers(self) -> List["RequestStoreManager"]:
        cached = getattr(self, "_prefetched_objects_cache", {}).get(
            "requests_stores_manager"
        )
        if cached is not None:
            return list(cached)
        return list(self.requests_stores_manager.all())

    request_type: RequestType | None = None
    status: RequestStatus | None = None
    tenant_id: strawberry.ID
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
class RequestListResponse:
    total_pages: int
    requests: List[Request]


@strawberry_django.type(models.Event)
class Event(Node):
    uuid: str
    name: str
    coordinates: List[float] | None = None
    address: str
    is_national: bool
    notes: str | None = None
    request: Request | None = None
    created_at: str
    updated_at: str
    tenant_id: strawberry.ID
    tenant: TenantType | None = None
    event_type: EventType | None = None
    status: EventStatus | None = None
    timezone: TimeZone | None = None

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
    def date(self) -> str | None:
        tz = _get_related_from_cache(self, "timezone")
        if not tz:
            req = _get_related_from_cache(self, "request")
            tz = _get_related_from_cache(req, "timezone") if req else None
        offset = _get_offset_minutes_from_timezone(tz)
        return _serialize_dt(_get_field(self, "date"), offset_minutes=offset)

    @strawberry.field
    def start_time(self) -> str | None:
        tz = _get_related_from_cache(self, "timezone")
        if not tz:
            req = _get_related_from_cache(self, "request")
            tz = _get_related_from_cache(req, "timezone") if req else None
        offset = _get_offset_minutes_from_timezone(tz)
        return _serialize_dt(_get_field(self, "start_time"), offset_minutes=offset)

    @strawberry.field
    def end_time(self) -> str | None:
        tz = _get_related_from_cache(self, "timezone")
        if not tz:
            req = _get_related_from_cache(self, "request")
            tz = _get_related_from_cache(req, "timezone") if req else None
        offset = _get_offset_minutes_from_timezone(tz)
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
