from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
# Monitored subclass: unexpected resolver crashes feed the backend
# error monitor (utils.error_monitor) instead of vanishing into a
# masked GraphQL 200.
from utils.graphql.monitored_schema import MonitoredJwtSchema as JwtSchema
from events.schema import EventQueryAmbassadors, EventMutationsAmbassadors
from tenants.schema import MutationAmbassadors, QueryAmbassadors
from jobs.schema import AmbassadorJobQueries, AmbassadorJobMutations
from ambassadors.schema import AmbassadorMutations
from utils.utils import BlockIntrospectionForAnonymous


QueryAmbassadors = merge_types(
    "Query", (EventQueryAmbassadors, QueryAmbassadors, AmbassadorJobQueries))
MutationAmbassadors = merge_types(
    "Mutation", (EventMutationsAmbassadors, MutationAmbassadors, AmbassadorJobMutations, AmbassadorMutations))

schema_ambassador = JwtSchema(
    query=QueryAmbassadors,
    mutation=MutationAmbassadors,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
