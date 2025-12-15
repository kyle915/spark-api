from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQueryClient, EventMutationsClient
from recaps.schema import RecapQueryClient, RecapMutationsClient
from ambassadors.schema import AmbassadorQueryClient, AmbassadorMutations
from tenants.schema import QueryClients, MutationClients
from tenants.dashboard.schema import DashboardQueries
from jobs.schema import ClientJobMutations, ClientJobQueries
from utils.utils import BlockIntrospectionForAnonymous
from utils.graphql.gcs_schema import GCSQuery

# Clients Schemas
QueryClients = merge_types(
    "Query",
    (
        EventQueryClient,
        RecapQueryClient,
        AmbassadorQueryClient,
        QueryClients,
        ClientJobQueries,
        DashboardQueries,
        GCSQuery,
    ),
)
MutationClients = merge_types(
    "Mutation",
    (
        EventMutationsClient,
        RecapMutationsClient,
        MutationClients,
        ClientJobMutations,
        AmbassadorMutations,
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
