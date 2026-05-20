from __future__ import annotations

import strawberry
import strawberry_django
from strawberry.relay import Node

from . import models


@strawberry_django.type(models.AcademyModule)
class AcademyModule(Node):
    uuid: str
    title: str
    kind: str
    body: str
    order: int
    published: bool
    created_at: str
    updated_at: str
    tenant_id: strawberry.ID


@strawberry.type
class AcademyModuleResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    academy_module: AcademyModule | None = None
