import strawberry_django
import strawberry

from . import models


@strawberry_django.type(models.EventType)
class EventType:
    id: strawberry.ID
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry_django.type(models.Event)
class Event:
    id: strawberry.ID
    uuid: str
    name: str
    created_at: str
    updated_at: str
    tenant_id: strawberry.ID


@strawberry.type
class EventDetailResponse:
    success: bool
    message: str
    event: Event | None = None
