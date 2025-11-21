import strawberry

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
class EventQuerySpark(EventQueryClient):
    pass


@strawberry.type
class EventQueryMobile(
    queries.EventQueries,
    queries.EventTypeQueries,
    queries.EventStatusQueries,
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
    RequestMutations,
):
    pass


@strawberry.type
class EventMutationsAmbassadors(
    PublicRequestMutations,
):
    pass


@strawberry.type
class EventMutationsClient(
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
    RequestMutations,
):
    pass


@strawberry.type
class EventMutationsSpark(
    EventMutationsAmbassadors, EventMutationsClient, EventTypeMutations, EventMutations
):
    pass


@strawberry.type
class EventMutationsMobile(
    PublicRequestMutations,
):
    pass
