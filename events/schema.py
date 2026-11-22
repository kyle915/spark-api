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
from events.account_list import AccountListMutations, AccountListQueries
from events.staffing_search import StaffingSearchQueries
from events.request_intake import RequestIntakeMutations
from events.retailer_aliases import RetailerAliasMutations, RetailerAliasQueries
from events.live_board import LiveBoardQueries
from events.payroll import PayrollQueries, PayrollMutations
from events.campaign_pnl import CampaignPnlQueries
from events.activation_plans import ActivationPlanQueries, ActivationPlanMutations
from events.field_marketing import FieldMarketingMutations, FieldMarketingQueries
from events.plan_results import PlanResultsMutations, PlanResultsQueries
from events.field_marketing_recaps import FieldMarketingRecapQueries
from events.jump_search import JumpSearchQueries
from events.request_rollups import RequestRollupQueries
from hours_check.schema import HoursCheckMutations, HoursCheckQueries


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
    StaffingSearchQueries,
    LiveBoardQueries,
    # Uploaded account lists (Account Map): staff, or a brand's own users.
    AccountListQueries,
    # Store names that look like the same store: staff only.
    RetailerAliasQueries,
    # Admin-only in practice: every resolver is gated by IsClientOrSparkAdmin
    # and scoped through _accessible_tenants. It lives on the CLIENTS schema
    # because that's the endpoint spark-front-client actually calls
    # (VITE_GRAPHQL_ENDPOINT → /graphql/clients) — same as LiveBoardQueries
    # above. Adding it Spark-side only would leave the admin tab unable to
    # reach it.
    EventConfirmationQueries,
    PayrollQueries,
    # Hours check before payroll (staff only), beside the timesheet.
    HoursCheckQueries,
    CampaignPnlQueries,
    ActivationPlanQueries,
    FieldMarketingQueries,
    PlanResultsQueries,
    FieldMarketingRecapQueries,
    JumpSearchQueries,
    RequestRollupQueries,
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
    ActivationPlanMutations,
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
    HoursCheckMutations,
    AccountListMutations,
    # Staff only: accept / dismiss store-name suggestions, and read a pasted
    # email or sheet row into a draft request (creates nothing).
    RetailerAliasMutations,
    RequestIntakeMutations,
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
    ActivationPlanMutations,
    FieldMarketingMutations,
    PlanResultsMutations,
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
