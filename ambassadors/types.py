import strawberry_django
import strawberry


from . import models
from tenants.types import SparkUserType


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
    user: SparkUserType
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


@strawberry.type
class UpdateAmbassadorResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador: Ambassador | None = None


@strawberry.type
class DeleteInvitationResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry_django.type(models.AmbassadorReview)
class AmbassadorReviewType:
    id: strawberry.ID
    uuid: str
    review: str | None
    score: int | None
    ambassador_id: strawberry.ID | None
    client_id: strawberry.ID | None
    tenant_id: strawberry.ID | None
    created_at: str
    updated_at: str


@strawberry.type
class CreateAmbassadorReviewResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_review: AmbassadorReviewType | None = None


@strawberry.type
class UpdateAmbassadorReviewResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_review: AmbassadorReviewType | None = None


@strawberry.type
class DeleteAmbassadorReviewResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry_django.type(models.AmbassadorNote)
class AmbassadorNoteType:
    id: strawberry.ID
    uuid: str
    note: str
    ambassador_id: strawberry.ID
    tenant_id: strawberry.ID
    created_by_id: strawberry.ID
    updated_by_id: strawberry.ID | None
    created_at: str
    updated_at: str


@strawberry.type
class CreateAmbassadorNoteResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_note: AmbassadorNoteType | None = None


@strawberry.type
class UpdateAmbassadorNoteResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_note: AmbassadorNoteType | None = None


@strawberry.type
class DeleteAmbassadorNoteResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry_django.type(models.Skill)
class SkillType:
    id: strawberry.ID
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class CreateSkillResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    skill: SkillType | None = None


@strawberry.type
class UpdateSkillResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    skill: SkillType | None = None


@strawberry.type
class DeleteSkillResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry_django.type(models.AmbassadorSkill)
class AmbassadorSkillType:
    id: strawberry.ID
    uuid: str
    ambassador_id: strawberry.ID
    skill_id: strawberry.ID
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class CreateAmbassadorSkillResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_skill: AmbassadorSkillType | None = None


@strawberry.type
class DeleteAmbassadorSkillResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry_django.type(models.AttendanceType)
class AttendanceType:
    id: strawberry.ID
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry.type
class AttendanceTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    attendance_type: AttendanceType | None = None


@strawberry_django.type(models.AttendanceStatus)
class AttendanceStatus:
    id: strawberry.ID
    uuid: str
    name: str
    tenant_id: strawberry.ID | None
    created_at: str
    updated_at: str


@strawberry.type
class AttendanceStatusDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    attendance_status: AttendanceStatus | None = None


@strawberry_django.type(models.Source)
class Source:
    id: strawberry.ID
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry.type
class SourceDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    source: Source | None = None


@strawberry_django.type(models.Attendance)
class Attendance:
    id: strawberry.ID
    uuid: str
    clock_time: str
    coordinates: list[float] | None
    ambassador_id: strawberry.ID | None
    job_id: strawberry.ID | None
    event_id: strawberry.ID | None
    attendace_type_id: strawberry.ID | None
    attendance_status_id: strawberry.ID | None
    source_id: strawberry.ID | None
    timezone_id: strawberry.ID | None
    created_at: str
    updated_at: str


@strawberry.type
class AttendanceDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    attendance: Attendance | None = None
