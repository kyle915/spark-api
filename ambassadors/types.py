import strawberry_django
import strawberry

from . import models


@strawberry_django.type(models.FileType)
class FileType:
    id: strawberry.ID
    uuid: str
    name: str
    extension: str | None
    created_at: str
    updated_at: str


@strawberry_django.type(models.Ambassador)
class Ambassador:
    id: strawberry.ID
    uuid: str
    rating: int
    address: str | None
    coordinates: list[float]
    user_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class FileTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    file_type: FileType | None = None
