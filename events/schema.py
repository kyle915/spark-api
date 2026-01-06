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
    RequestStoreManagerMutations,
    TimeZoneMutations,
)
from events import queries


@strawberry.type
class EventQueryAmbassadors(
    queries.EventQueries,
    queries.EventTypeQueries,
    queries.EventStatusQueries,
    queries.TimeZoneQueries,
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
    queries.RequestStoreManagerQueries,
    queries.LocationQueries,
    queries.TimeZoneQueries,
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
    queries.RetailerQueries,
    queries.ProductTypeQueries,
    queries.ProductQueries,
    queries.TimeZoneQueries,
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
    RequestStoreManagerMutations,
):
    pass


@strawberry.type
class EventMutationsAmbassadors(
    PublicRequestMutations,
):
    pass


@strawberry.type
class EventMutationsClient(
    EventMutations,
    PublicRequestMutations,
    EventTypeMutations,
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
    RequestStoreManagerMutations,
    TimeZoneMutations,
):
    pass


@strawberry.type
class EventMutationsAmbassadors(
    PublicRequestMutations,
):
    pass


@strawberry.type
class EventMutationsSpark(
    EventMutationsAmbassadors,
    EventMutationsClient,
    EventTypeMutations,
    TimeZoneMutations,
):
    pass


@strawberry.type
class EventMutationsMobile(
    PublicRequestMutations,
):
    pass
