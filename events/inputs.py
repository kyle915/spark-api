import strawberry
from typing import List

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class CreateEventInput(SparkGraphQLInput):
    name: str
    event_type_id: strawberry.ID
    status_id: strawberry.ID
    tenant_id: strawberry.ID | None = None


@strawberry.input
class UpdateEventInput(CreateEventInput):
    id: strawberry.ID


@strawberry.input
class CreateEventTypeInput(SparkGraphQLInput):
    name: str
    tenant_id: strawberry.ID | None = None


@strawberry.input
class UpdateEventTypeInput(CreateEventTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateEventStatusInput(SparkGraphQLInput):
    name: str
    tenant_id: strawberry.ID | None = None


@strawberry.input
class UpdateEventStatusInput(CreateEventStatusInput):
    id: strawberry.ID
