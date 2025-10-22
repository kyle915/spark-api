import strawberry
import strawberry_django
from strawberry_django.optimizer import DjangoOptimizerExtension
from .types import EventType
from strawberry_django.permissions import (
    IsAuthenticated,
    HasPerm,
    HasRetvalPerm,
)

@strawberry.type
class EventsQuery:
    event_types: list[EventType] = strawberry_django.field(extensions=[IsAuthenticated()],)
