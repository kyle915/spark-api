from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventQuerySpark, EventMutationsSpark
from tenants.schema import MutationSpark, QuerySpark
from jobs.schema import SparkJobMutations
from utils.utils import BlockIntrospectionForAnonymous

# Spark Schemas
QuerySpark = merge_types("Query", (EventQuerySpark, QuerySpark))
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
