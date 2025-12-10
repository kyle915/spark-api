from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQueryMobile, EventMutationsMobile
from tenants.schema import MutationMobile, QueryMobile
from utils.utils import BlockIntrospectionForAnonymous
from ambassadors.schema import AmbassadorMutationsMobile, AmbassadorQueryMobile
from recaps.schema import RecapQueryMobile, RecapMutationsMobile
from utils.graphql.gcs_schema import GCSQuery

QueryMobile = merge_types(
    "Query",
    (EventQueryMobile, QueryMobile, RecapQueryMobile, AmbassadorQueryMobile, GCSQuery),
)
MutationMobile = merge_types(
    "Mutation",
    (
        EventMutationsMobile,
        MutationMobile,
        AmbassadorMutationsMobile,
        RecapMutationsMobile,
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
