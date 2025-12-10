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
    is_active: bool
    user_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry_django.type(models.AmbassadorEvent)
class AmbassadorEventType:
    id: strawberry.ID
    uuid: str
    is_approved: bool
    ambassador: Ambassador
    tenant_id: strawberry.ID
    event_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class FileTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    file_type: FileType | None = None


@strawberry_django.type(models.AmbassadorInvitation)
class AmbassadorInvitationType:
    id: strawberry.ID
    uuid: str
    email: str
    token: str
    expires_at: str
    is_used: bool
    used_at: str | None
    invited_by_id: strawberry.ID
    tenant_id: strawberry.ID
    ambassador_id: strawberry.ID | None
    created_at: str
    updated_at: str


@strawberry.type
class PublicAmbassadorCreationResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador: Ambassador | None = None
    activation_token: str | None = None


@strawberry.type
class AmbassadorInvitationResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    invitation: AmbassadorInvitationType | None = None


@strawberry.type
class AcceptInvitationResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador: Ambassador | None = None
    activation_token: str | None = None


@strawberry.type
class ApproveAmbassadorResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador: Ambassador | None = None
