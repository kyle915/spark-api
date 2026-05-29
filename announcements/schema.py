import strawberry

from . import queries
from .mutations import AnnouncementMutations


@strawberry.type
class AnnouncementQuerySpark(
    queries.AnnouncementQueries,
    queries.AnnouncementMobileQueries,
):
    pass


@strawberry.type
class AnnouncementQueryMobile(queries.AnnouncementMobileQueries):
    pass


@strawberry.type
class AnnouncementMutationsSpark(AnnouncementMutations):
    pass


# Mobile gets NO announcement mutations (BAs can't broadcast). This empty
# type is intentionally NOT merged into the mobile mutation schema —
# merge_types rejects a fieldless type. It's kept here only as a named
# placeholder for a future mobile-side announcement write, should one be
# needed. Do not add it to config/schema_mobile.py's MutationMobile tuple.
@strawberry.type
class AnnouncementMutationsMobile:
    pass


# The admin web app queries the CLIENT surface (/graphql/clients), so the
# announcement admin query + composer mutations must be exposed there too.
# (Same resolvers as Spark. Mutations are tenant-scoped + the web composer
# is role-gated client-side; tightening the backend role gate is a tracked
# follow-up.)
@strawberry.type
class AnnouncementQueryClient(
    queries.AnnouncementQueries,
    queries.AnnouncementMobileQueries,
):
    pass


@strawberry.type
class AnnouncementMutationsClient(AnnouncementMutations):
    pass
