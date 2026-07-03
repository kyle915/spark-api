from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
# Monitored subclass: unexpected resolver crashes feed the backend
# error monitor (utils.error_monitor) instead of vanishing into a
# masked GraphQL 200.
from utils.graphql.monitored_schema import MonitoredJwtSchema as JwtSchema
from events.schema import EventQueryMobile, EventMutationsMobile
from tenants.schema import MutationMobile, QueryMobile
from utils.utils import BlockIntrospectionForAnonymous
from ambassadors.schema import AmbassadorMutationsMobile, AmbassadorQueryMobile
from recaps.schema import RecapQueryMobile, RecapMutationsMobile
from jobs.schema import MobileJobMutations, MobileJobQueries
from academy.schema import AcademyQueryMobile
from chats.schema import ChatQueryMobile, ChatMutationsMobile
from availability.schema import AvailabilityQueryMobile, AvailabilityMutationsMobile
from documents.schema import DocumentQueryMobile, DocumentMutationsMobile
from announcements.schema import AnnouncementQueryMobile
from utils.graphql.gcs_schema import GCSQuery

QueryMobile = merge_types(
    "Query",
    (
        EventQueryMobile,
        QueryMobile,
        RecapQueryMobile,
        AmbassadorQueryMobile,
        MobileJobQueries,
        GCSQuery,
        AcademyQueryMobile,
        ChatQueryMobile,
        AvailabilityQueryMobile,
        DocumentQueryMobile,
        AnnouncementQueryMobile,
    ),
)
MutationMobile = merge_types(
    "Mutation",
    (
        EventMutationsMobile,
        MutationMobile,
        AmbassadorMutationsMobile,
        MobileJobMutations,
        RecapMutationsMobile,
        ChatMutationsMobile,
        AvailabilityMutationsMobile,
        DocumentMutationsMobile,
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
