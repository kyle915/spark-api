import strawberry
from typing import List

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class RecapFiltersInput(SparkGraphQLInput):
    event_id: strawberry.ID | None = None


@strawberry.input
class CreateRecapFileInput(SparkGraphQLInput):
    name: str
    file: str
    file_type_id: strawberry.ID
    approved: bool = False


@strawberry.input
class CreateRecapInput(SparkGraphQLInput):
    name: str
    event_id: strawberry.ID
    files: List[str]  # List of file URLs/paths from GCS


@strawberry.input
class UpdateRecapInput(CreateRecapInput):
    id: strawberry.ID


@strawberry.input
class DeleteRecapInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class DeleteRecapFileInput(SparkGraphQLInput):
    id: strawberry.ID
