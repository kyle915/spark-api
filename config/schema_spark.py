from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQuerySpark, EventMutationsSpark
from recaps.schema import RecapQuerySpark, RecapMutationsSpark
from ambassadors.schema import AmbassadorQuerySpark, AmbassadorMutationsSpark
from tenants.schema import MutationSpark, QuerySpark
from tenants.dashboard.schema import DashboardQueries
from tenants.dashboard.mutations import DashboardMutations
from jobs.schema import SparkJobMutations, SparkJobQueries
from wingspan.schema import WingspanQuerySpark
from utils.utils import BlockIntrospectionForAnonymous
from utils.graphql.gcs_schema import GCSQuery

# Spark Schemas
QuerySpark = merge_types(
    "Query", (EventQuerySpark, RecapQuerySpark, AmbassadorQuerySpark, QuerySpark, SparkJobQueries, DashboardQueries, GCSQuery, WingspanQuerySpark))
MutationSpark = merge_types(
    "Mutation", (EventMutationsSpark, RecapMutationsSpark, MutationSpark, SparkJobMutations, AmbassadorMutationsSpark, DashboardMutations))

schema_spark = JwtSchema(
    query=QuerySpark,
    mutation=MutationSpark,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
