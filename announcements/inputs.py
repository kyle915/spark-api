import strawberry

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class AnnouncementFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None


@strawberry.input
class CreateAnnouncementInput(SparkGraphQLInput):
    title: str
    body: str = ""
    audience: str = "all_bas"
    tenant_id: strawberry.ID | None = None


@strawberry.input
class DeleteAnnouncementInput(SparkGraphQLInput):
    uuid: strawberry.ID
