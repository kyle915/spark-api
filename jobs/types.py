import strawberry_django
import strawberry

from . import models


@strawberry_django.type(models.Status)
class Status:
    id: strawberry.ID
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class StatusDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    status: Status | None = None

