from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQueryAmbassadors, EventsMutations
from tenants.schema import MutationAmbassadors, QueryAmbassadors
from utils.utils import BlockIntrospectionForAnonymous


QueryAmbassadors = merge_types(
    "Query", (EventQueryAmbassadors, QueryAmbassadors))
MutationAmbassadors = merge_types(
    "Mutation", (EventsMutations, MutationAmbassadors,))

schema_ambassador = JwtSchema(
    query=QueryAmbassadors,
    mutation=MutationAmbassadors,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
