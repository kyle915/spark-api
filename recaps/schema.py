import strawberry

from recaps.mutations import RecapMutations
from recaps import queries


@strawberry.type
class RecapQuerySpark(
    queries.RecapQueries,
    queries.RecapMobileQueries,
):
    pass


@strawberry.type
class RecapQueryClient(
    queries.RecapQueries,
):
    pass


@strawberry.type
class RecapQueryMobile(
    queries.RecapMobileQueries,
):
    pass


@strawberry.type
class RecapMutationsSpark(
    RecapMutations,
):
    pass


@strawberry.type
class RecapMutationsClient(
    RecapMutations,
):
    pass


@strawberry.type
class RecapMutationsMobile(
    RecapMutations,
):
    pass
