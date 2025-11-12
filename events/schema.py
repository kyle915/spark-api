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
    RequestStatusMutations,
    RetailerMutations,
    ProductTypeMutations,
    ProductMutations,
    RequestTypeMutations,
    RequestMutations,
    PublicRequestMutations,
)
from events import queries


@strawberry.type
class EventQueryAmbassadors(
    queries.EventQueries,
    queries.EventTypeQueries,
    queries.EventStatusQueries,
):
    pass


@strawberry.type
class EventQueryClient(
    queries.EventQueries,
    queries.EventTypeQueries,
    queries.EventStatusQueries,
    queries.ClientQueries,
    queries.DistributorQueries,
    queries.RetailerQueries,
    queries.ProductTypeQueries,
    queries.ProductQueries,
    queries.RequestTypeQueries,
    queries.RequestStatusQueries,
    queries.RequestQueries,
    queries.LocationQueries,
):
    pass


@strawberry.type
class EventQuerySpark(
    EventQueryClient
):
    pass


@strawberry.type
class EventsMutations(
    EventMutations,
    EventTypeMutations,
    PublicRequestMutations,
    EventStatusMutations,
    LocationMutations,
    ClientMutations,
    DistributorMutations,
    RetailerMutations,
    ProductTypeMutations,
    ProductMutations,
    RequestTypeMutations,
    RequestStatusMutations,
    RequestMutations
):
    pass
