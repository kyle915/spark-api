import strawberry

from receipts.mutations import ReceiptMutations
from receipts import queries


@strawberry.type
class ReceiptQueryClient(
    queries.ReceiptQueries,
):
    pass


@strawberry.type
class ReceiptMutationsClient(
    ReceiptMutations,
):
    pass
