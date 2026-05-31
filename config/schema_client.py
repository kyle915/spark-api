from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQueryClient, EventMutationsClient
from recaps.schema import RecapQueryClient, RecapMutationsClient
from recaps.report_types import CampaignReportQueries
from receipts.schema import ReceiptQueryClient, ReceiptMutationsClient
from billing.schema import BillingQueryClient, BillingMutationsClient
from ambassadors.schema import AmbassadorQueryClient, AmbassadorMutationsClient
from tenants.schema import QueryClients, MutationClients
from tenants.dashboard.schema import DashboardQueries
from tenants.dashboard.mutations import DashboardMutations
from jobs.schema import ClientJobMutations, ClientJobQueries
from academy.schema import AcademyQueryClient, AcademyMutationsClient
from wingspan.schema import WingspanQueryClient
from chats.schema import ChatQueryClient, ChatMutationsClient
from announcements.schema import AnnouncementQueryClient, AnnouncementMutationsClient
from utils.utils import BlockIntrospectionForAnonymous
from utils.graphql.gcs_schema import GCSQuery

# Clients Schemas
QueryClients = merge_types(
    "Query",
    (
        EventQueryClient,
        RecapQueryClient,
        CampaignReportQueries,
        AmbassadorQueryClient,
        QueryClients,
        ClientJobQueries,
        DashboardQueries,
        GCSQuery,
        AcademyQueryClient,
        WingspanQueryClient,
        ChatQueryClient,
        AnnouncementQueryClient,
        ReceiptQueryClient,
        BillingQueryClient,
    ),
)
MutationClients = merge_types(
    "Mutation",
    (
        EventMutationsClient,
        RecapMutationsClient,
        MutationClients,
        ClientJobMutations,
        AmbassadorMutationsClient,
        DashboardMutations,
        AcademyMutationsClient,
        ChatMutationsClient,
        AnnouncementMutationsClient,
        ReceiptMutationsClient,
        BillingMutationsClient,
    ),
)

schema_clients = JwtSchema(
    query=QueryClients,
    mutation=MutationClients,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
