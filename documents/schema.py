import strawberry

from . import queries, mutations


@strawberry.type
class DocumentQueryMobile(
    queries.DocumentMobileQueries,
):
    pass


@strawberry.type
class DocumentMutationsMobile(
    mutations.DocumentMobileMutations,
):
    pass
