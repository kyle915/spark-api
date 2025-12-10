import strawberry

from ambassadors import queries, mutations


@strawberry.type
class AmbassadorQuerySpark(
    queries.FileTypeQueries,
    queries.AmbassadorManagementQueries,
):
    pass


@strawberry.type
class AmbassadorQueryClient(
    queries.FileTypeQueries,
    queries.AmbassadorManagementQueries,
):
    pass


@strawberry.type
class AmbassadorMutations(
    mutations.AmbassadorMutations,
):
    pass
