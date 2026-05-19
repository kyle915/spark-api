import strawberry_django
import strawberry
from strawberry.relay import Node
from asgiref.sync import sync_to_async

from . import models
from tenants.types import SparkUserType
from events.types import Event, Location


@strawberry_django.type(models.FileType)
class FileType(Node):
    uuid: str
    name: str
    extension: str | None
    created_at: str
    updated_at: str


@strawberry_django.type(models.Ambassador)
class Ambassador(Node):
    uuid: str
    rating: int
    address: str | None
    phone: str | None
    about_me: str | None
    coordinates: list[float]
    is_active: bool
    location: Location | None
    t_shirt_size: str | None
    user: SparkUserType
    created_at: str
    updated_at: str


@strawberry_django.type(models.AmbassadorEvent)
class AmbassadorEventType(Node):
    uuid: str
    is_approved: bool
    ambassador: Ambassador
    event: Event
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
class AmbassadorInvitationType(Node):
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
class DisableAmbassadorResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador: Ambassador | None = None


@strawberry.type
class RegenerateAmbassadorPasswordResult:
    ambassador_id: strawberry.ID | None = None
    email: str | None = None
    success: bool
    message: str


@strawberry.type
class RegenerateAmbassadorPasswordsResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    results: list[RegenerateAmbassadorPasswordResult] | None = None


@strawberry.type
class CreateAmbassadorResponse:
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
class AmbassadorReviewType(Node):
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
class AmbassadorNoteType(Node):
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
class SkillType(Node):
    uuid: str
    name: str
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
class AmbassadorSkillType(Node):
    uuid: str
    ambassador_id: strawberry.ID
    skill_id: strawberry.ID
    skill: SkillType
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


@strawberry_django.type(models.AmbassadorFile)
class AmbassadorFileType(Node):
    uuid: str
    name: str
    url: str | None
    main_resume: bool
    profile_pic: bool
    is_public: bool
    ambassador_id: strawberry.ID
    file_type: FileType | None
    created_at: str
    updated_at: str


@strawberry_django.type(models.AmbassadorTrait)
class AmbassadorTraitType(Node):
    uuid: str
    ambassador_id: strawberry.ID
    user_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry_django.type(models.AmbassadorWorkHistory)
class AmbassadorWorkHistoryType(Node):
    uuid: str
    ambassador_id: strawberry.ID
    user_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class AmbassadorProfile:
    ambassador: Ambassador
    reviews: list[AmbassadorReviewType]
    files: list[AmbassadorFileType]
    traits: list[AmbassadorTraitType]
    skills: list[AmbassadorSkillType]
    notes: list[AmbassadorNoteType]
    work_history: list[AmbassadorWorkHistoryType]


@strawberry.type
class UpsertAmbassadorProfileResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    profile: AmbassadorProfile | None = None


@strawberry_django.type(models.AttendanceType)
class AttendanceType(Node):
    uuid: str
    name: str
    slug: str | None
    created_at: str
    updated_at: str


@strawberry.type
class AttendanceTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    attendance_type: AttendanceType | None = None


@strawberry_django.type(models.AttendanceStatus)
class AttendanceStatus(Node):
    uuid: str
    name: str
    slug: str | None
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
class Source(Node):
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
class Attendance(Node):
    uuid: str
    clock_time: str
    coordinates: list[float] | None
    ambassador_id: strawberry.ID | None
    job_id: strawberry.ID | None
    event_id: strawberry.ID | None
    attendace_type_id: strawberry.ID | None
    attendace_type: AttendanceType | None
    attendance_status_id: strawberry.ID | None
    attendance_status: AttendanceStatus | None
    source_id: strawberry.ID | None
    timezone_id: strawberry.ID | None
    created_at: str
    updated_at: str

    @strawberry.field
    async def ambassador(self) -> Ambassador | None:
        """Return the ambassador who submitted the attendance."""
        return await sync_to_async(lambda obj: obj.ambassador)(self)


@strawberry.type
class AttendanceDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    attendance: Attendance | None = None


@strawberry_django.type(models.GroupType)
class GroupType(Node):
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry.type
class GroupTypeResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    group_type: GroupType | None = None


@strawberry_django.type(models.UserGroup)
class UserGroup(Node):
    uuid: str
    user: SparkUserType
    ambassador: Ambassador | None


@strawberry_django.type(models.AmbassadorGroup)
class AmbassadorGroup(Node):
    uuid: str
    name: str
    description: str | None
    private: bool
    group_type: GroupType
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str
    members: list[UserGroup]


@strawberry.type
class AmbassadorGroupResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_group: AmbassadorGroup | None = None


@strawberry.type
class AddAmbassadorsToGroupResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    members: list[UserGroup] | None = None


@strawberry.type
class RegisterPushTokenResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class OAuthTokenType:
    """Mirrors the gqlauth TokenType shape mobile expects."""

    token: str
    refresh_token: str | None = None


@strawberry.type
class OAuthUserType:
    uuid: strawberry.ID
    email: str
    first_name: str | None = None
    last_name: str | None = None


@strawberry.type
class InviteAmbassadorToShiftResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_event_uuid: strawberry.ID | None = None


@strawberry.type
class RespondToShiftOfferResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    accepted: bool = False


@strawberry.type
class ShiftOfferDetails:
    """Slim shape for the mobile ShiftOfferScreen — just what the
    BA needs to decide. Avoids pulling the full AmbassadorEvent /
    Event graph when most of it isn't shown."""

    ambassador_event_uuid: strawberry.ID
    event_uuid: strawberry.ID
    event_name: str
    venue: str | None
    address: str | None
    date: str | None
    start_time: str | None
    end_time: str | None
    state_code: str | None
    is_approved: bool


@strawberry.type
class LocationPingType:
    """Slim shape — what the Today map actually renders."""

    uuid: strawberry.ID
    lat: float
    lng: float
    accuracy_meters: float | None
    recorded_at: str
    source: str
    ambassador_uuid: strawberry.ID
    ambassador_name: str
    event_uuid: strawberry.ID
    event_name: str


@strawberry.type
class LocationPingResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class OAuthSignInResponse:
    """Response for the mobile appleSignIn / googleSignIn mutations.

    Shape mirrors what the LoginScreen consumes:
        { token { token, refreshToken }, user { uuid, email, firstName, lastName } }
    Plus a ``success`` / ``message`` envelope so we can surface
    verification errors without throwing.
    """

    success: bool
    message: str
    token: OAuthTokenType | None = None
    user: OAuthUserType | None = None
    is_new_account: bool = False
    client_mutation_id: strawberry.ID | None = None
