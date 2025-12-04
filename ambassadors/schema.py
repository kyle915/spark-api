import strawberry

from ambassadors import queries


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
