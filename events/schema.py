import strawberry
import strawberry_django
from .types import EventType
from strawberry_django.permissions import (
    IsAuthenticated,
)

@strawberry.type
class EventsQuery:
    event_types: list[EventType] = strawberry_django.field(extensions=[IsAuthenticated()],)
