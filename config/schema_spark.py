from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventsQuery
from tenants.schema import MutationSpark, QuerySpark
from utils.utils import BlockIntrospectionForAnonymous

#Spark Schemas
QuerySpark = merge_types("Query", (EventsQuery, QuerySpark))
MutationSpark = merge_types("Mutation", (MutationSpark,))

schema_spark = JwtSchema(
    query=QuerySpark,
    mutation=MutationSpark,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)