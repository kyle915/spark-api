import strawberry

from jobs import mutations


@strawberry.type
class ClientJobMutations(
    mutations.StatusMutations,
):
    pass


@strawberry.type
class SparkJobMutations(
    mutations.StatusMutations,
):
    pass
