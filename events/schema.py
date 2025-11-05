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
from .mutations import (
    EventMutations,
    EventTypeMutations,
    EventStatusMutations,
    LocationMutations,
    ClientMutations,
    DistributorMutations,
    RetailerMutations,
    ProductTypeMutations,
    ProductMutations,
    RequestTypeMutations,
    RequestMutations
)
from .queries import EventAmbassadorsQueries, EventSparkQueries, EventClientQueries


@strawberry.type
class EventQueryAmbassadors(EventAmbassadorsQueries):
    pass


@strawberry.type
class EventQuerySpark(EventSparkQueries):
    pass


@strawberry.type
class EventQueryClient(EventClientQueries):
    pass


@strawberry.type
class EventsMutations(
    EventMutations,
    EventTypeMutations,
    EventStatusMutations,
    LocationMutations,
    ClientMutations,
    DistributorMutations,
    RetailerMutations,
    ProductTypeMutations,
    ProductMutations,
    RequestTypeMutations,
    RequestMutations
):
    pass
