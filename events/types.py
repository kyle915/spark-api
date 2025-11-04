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
