import strawberry

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class AcademyModuleFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    kind: str | None = None
    published: bool | None = None


@strawberry.input
class CreateAcademyModuleInput(SparkGraphQLInput):
    title: str
    kind: str = "training"
    body: str = ""
    order: int = 0
    published: bool = False
    tenant_id: strawberry.ID | None = None


@strawberry.input
class UpdateAcademyModuleInput(SparkGraphQLInput):
    uuid: strawberry.ID
    title: str | None = None
    kind: str | None = None
    body: str | None = None
    order: int | None = None
    published: bool | None = None


@strawberry.input
class DeleteAcademyModuleInput(SparkGraphQLInput):
    uuid: strawberry.ID
