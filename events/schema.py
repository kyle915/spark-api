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
    BillingEntityMutations,
    RequestMutations,
    PublicRequestMutations,
    RequestStoreManagerMutations,
    TimeZoneMutations,
)
from events import queries
from events.event_confirmation_schema import (
    EventConfirmationMutations,
    EventConfirmationQueries,
)
from events.staffing_board import StaffingBoardQueries
from events.live_board import LiveBoardQueries
from events.payroll import PayrollQueries, PayrollMutations
from events.campaign_pnl import CampaignPnlQueries


@strawberry.type
class EventQueryAmbassadors(
    queries.EventQueries,
    queries.EventTypeQueries,
    queries.EventStatusQueries,
    queries.StateQueries,
    queries.TimeZoneQueries,
):
    pass


@strawberry.type
class EventQueryClient(
    queries.EventQueries,
    StaffingBoardQueries,
    LiveBoardQueries,
    # Admin-only in practice: every resolver is gated by IsClientOrSparkAdmin
    # and scoped through _accessible_tenants. It lives on the CLIENTS schema
    # because that's the endpoint spark-front-client actually calls
    # (VITE_GRAPHQL_ENDPOINT → /graphql/clients) — same as LiveBoardQueries
    # above. Adding it Spark-side only would leave the admin tab unable to
    # reach it.
    EventConfirmationQueries,
    PayrollQueries,
    CampaignPnlQueries,
    queries.EventTypeQueries,
    queries.EventStatusQueries,
    queries.ClientQueries,
    queries.DistributorQueries,
    queries.RetailerQueries,
    queries.ProductTypeQueries,
    queries.ProductQueries,
    queries.RequestTypeQueries,
    queries.BillingEntityQueries,
    queries.RequestStatusQueries,
    queries.RequestQueries,
    queries.RequestStoreManagerQueries,
    queries.LocationQueries,
    queries.StateQueries,
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
    queries.LocationQueries,
    queries.RetailerQueries,
    queries.ProductTypeQueries,
    queries.ProductQueries,
    queries.StateQueries,
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
    BillingEntityMutations,
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
    PayrollMutations,
    # Admin-gated; on the clients schema for the same endpoint reason as
    # EventConfirmationQueries above.
    EventConfirmationMutations,
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
    BillingEntityMutations,
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
