from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQueryAmbassadors, EventMutationsAmbassadors
from tenants.schema import MutationAmbassadors, QueryAmbassadors
from jobs.schema import AmbassadorJobQueries
from utils.utils import BlockIntrospectionForAnonymous


QueryAmbassadors = merge_types(
    "Query", (EventQueryAmbassadors, QueryAmbassadors, AmbassadorJobQueries))
MutationAmbassadors = merge_types(
    "Mutation", (EventMutationsAmbassadors, MutationAmbassadors,))

schema_ambassador = JwtSchema(
    query=QueryAmbassadors,
    mutation=MutationAmbassadors,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
