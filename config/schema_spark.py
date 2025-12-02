from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQuerySpark, EventMutationsSpark
from tenants.schema import MutationSpark, QuerySpark
from tenants.dashboard.schema import DashboardQueries
from jobs.schema import SparkJobMutations, SparkJobQueries
from utils.utils import BlockIntrospectionForAnonymous
from utils.graphql.gcs_schema import GCSQuery

# Spark Schemas
QuerySpark = merge_types(
    "Query", (EventQuerySpark, QuerySpark, SparkJobQueries, DashboardQueries, GCSQuery))
MutationSpark = merge_types(
    "Mutation", (EventMutationsSpark, MutationSpark, SparkJobMutations))

schema_spark = JwtSchema(
    query=QuerySpark,
    mutation=MutationSpark,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
