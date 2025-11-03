import strawberry
from typing import List


@strawberry.type
class SparkGraphQLErrorResponse:
    success: bool
    message: str
    errors: List[str] | None = None
