import strawberry

from ambassadors import queries, mutations


@strawberry.type
class AmbassadorQuerySpark(
    queries.FileTypeQueries,
):
    pass


@strawberry.type
class AmbassadorQueryClient(
    queries.FileTypeQueries,
):
    pass


@strawberry.type
class AmbassadorMutations(
    mutations.AmbassadorMutations,
):
    pass
