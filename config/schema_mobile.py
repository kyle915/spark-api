from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQueryMobile, EventMutationsMobile
from tenants.schema import MutationMobile, QueryMobile
from utils.utils import BlockIntrospectionForAnonymous
from ambassadors.schema import AmbassadorMutations

QueryMobile = merge_types("Query", (EventQueryMobile, QueryMobile))
MutationMobile = merge_types(
    "Mutation",
    (
        EventMutationsMobile,
        MutationMobile,
        AmbassadorMutations,
    ),
)

schema_mobile = JwtSchema(
    query=QueryMobile,
    mutation=MutationMobile,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
