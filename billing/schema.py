import strawberry

from billing.mutations import BillingMutations
from billing import queries


@strawberry.type
class BillingQueryClient(
    queries.BillingQueries,
):
    pass


@strawberry.type
class BillingMutationsClient(
    BillingMutations,
):
    pass
