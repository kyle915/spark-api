import strawberry

from . import queries
from .mutations import AcademyMutations


@strawberry.type
class AcademyQuerySpark(
    queries.AcademyQueries,
    queries.AcademyMobileQueries,
):
    pass


@strawberry.type
class AcademyQueryClient(queries.AcademyQueries):
    pass


@strawberry.type
class AcademyQueryMobile(queries.AcademyMobileQueries):
    pass


@strawberry.type
class AcademyMutationsSpark(AcademyMutations):
    pass


@strawberry.type
class AcademyMutationsClient(AcademyMutations):
    pass
