from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQueryClient, EventMutationsClient
from tenants.schema import QueryClients, MutationClients
from utils.utils import BlockIntrospectionForAnonymous

# Clients Schemas
QueryClients = merge_types("Query", (EventQueryClient, QueryClients))
MutationClients = merge_types(
    "Mutation", (EventMutationsClient, MutationClients,))

schema_clients = JwtSchema(
    query=QueryClients,
    mutation=MutationClients,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
