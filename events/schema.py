import strawberry
import strawberry_django
from strawberry.types import Info
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from .types import EventType, Event
from . import models
from tenants.models import TenantedUser
from strawberry_django.permissions import (
    IsAuthenticated,
)
from .mutations import EventsAmbassadorsMutation, EventsSparkMutation
from .queries import EventQueries


@strawberry.type
class EventsQuery(EventQueries):
    event_types: list[EventType] = strawberry_django.field(
        extensions=[IsAuthenticated()],)


@strawberry.type
class EventsAmbassadorsMutation(EventsAmbassadorsMutation):
    pass


@strawberry.type
class EventsSparkMutation(EventsSparkMutation):
    pass
