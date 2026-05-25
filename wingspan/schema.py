import strawberry

from . import queries


@strawberry.type
class WingspanQuerySpark(queries.WingspanQueries):
    pass


@strawberry.type
class WingspanQueryClient(queries.WingspanQueries):
    pass
