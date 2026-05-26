import strawberry
from typing import List

from utils.graphql.inputs import SparkGraphQLInput, BaseTenantInput, BaseNameableInput


@strawberry.input
class CreatePublicAmbassadorInput(SparkGraphQLInput):
    """Input for public ambassador creation."""

    first_name: str
    email: str
    password1: str
    password2: str
    address: str | None = None
    phone: str | None = None
    about_me: str | None = None
    coordinates: List[float] | None = None  # [latitude, longitude]


@strawberry.input
class CreateAmbassadorWithUserInput(CreatePublicAmbassadorInput):
    """Input for authenticated ambassador creation with active option."""

    last_name: str | None = None
    location_id: strawberry.ID | None = None
    password1: str | None = None
    password2: str | None = None
    is_active: bool | None = None


@strawberry.input
class CreateAmbassadorInvitationInput(BaseTenantInput):
    """Input for creating ambassador invitation."""

    email: str


@strawberry.input
class AcceptAmbassadorInvitationInput(SparkGraphQLInput):
    """Input for accepting ambassador invitation."""

    token: str
    first_name: str
    password1: str
    password2: str
    address: str | None = None
    coordinates: List[float] | None = None  # [latitude, longitude]


@strawberry.input
class AcceptByTokenInput(SparkGraphQLInput):
    """Input for accepting by token."""

    token: str


@strawberry.input
class ApproveAmbassadorInput(SparkGraphQLInput):
    """Input for approving an ambassador."""

    ambassador_id: strawberry.ID


@strawberry.input
class DisableAmbassadorInput(SparkGraphQLInput):
    """Input for disabling an ambassador and their user account."""

    ambassador_id: strawberry.ID


@strawberry.input
class RegenerateAmbassadorPasswordsInput(SparkGraphQLInput):
    """Input for regenerating passwords for multiple ambassadors."""

    ambassador_ids: list[strawberry.ID]


@strawberry.input
class AmbassadorInvitationFiltersInput:
    """Filters for ambassador invitation queries."""

    tenant_id: strawberry.ID | None = None
    tenant_uuid: strawberry.ID | None = None
    job_id: strawberry.ID | None = None
    job_uuid: strawberry.ID | None = None
    is_expired: bool | None = None  # True for expired, False for active, None for all
    is_used: bool | None = None  # True for used, False for unused, None for all
    email: str | None = None  # Search by email (partial match)
    search: str | None = None  # Search by email or name (general search)


@strawberry.input
class AmbassadorFiltersInput:
    """Filters for ambassador queries."""

    tenant_id: strawberry.ID | None = None
    tenant_uuid: strawberry.ID | None = None
    is_active: bool | None = None  # True for active, False for inactive, None for all
    rating_min: int | None = None
    rating_max: int | None = None
    email: str | None = None  # Search by user email (partial match)
    name: str | None = None  # Search by user first_name or last_name
    address: str | None = None  # Search by address (partial match)
    about_me: str | None = None  # Search by about_me (partial match)
    search: str | None = None  # General search across email, name, address


@strawberry.input
class UpdateAmbassadorInput(SparkGraphQLInput):
    """Input for updating an ambassador."""

    ambassador_id: strawberry.ID
    address: str | None = None
    about_me: str | None = None
    coordinates: List[float] | None = None
    is_active: bool | None = None
    tenant_id: strawberry.ID | None = None  # For assigning to tenant


@strawberry.input
class CreateAmbassadorInput(SparkGraphQLInput):
    """Input for creating an ambassador."""

    user_id: strawberry.ID
    address: str | None = None
    about_me: str | None = None
    coordinates: List[float] | None = None
    is_active: bool | None = None
    rating: int | None = None


@strawberry.input
class DeleteInvitationInput(SparkGraphQLInput):
    """Input for deleting an invitation."""

    invitation_id: strawberry.ID


@strawberry.input
class CreateAmbassadorReviewInput(BaseTenantInput):
    """Input for creating an ambassador review."""

    ambassador_id: strawberry.ID
    client_id: strawberry.ID | None = None
    review: str | None = None
    score: int | None = None


@strawberry.input
class UpdateAmbassadorReviewInput(SparkGraphQLInput):
    """Input for updating an ambassador review."""

    review_id: strawberry.ID
    review: str | None = None
    score: int | None = None


@strawberry.input
class DeleteAmbassadorReviewInput(SparkGraphQLInput):
    """Input for deleting an ambassador review."""

    review_id: strawberry.ID


@strawberry.input
class AmbassadorReviewFiltersInput:
    """Filters for ambassador review queries."""

    ambassador_id: strawberry.ID | None = None
    client_id: strawberry.ID | None = None
    tenant_id: strawberry.ID | None = None
    tenant_uuid: strawberry.ID | None = None
    min_score: int | None = None
    max_score: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    search: str | None = None


@strawberry.input
class CreateAmbassadorNoteInput(BaseTenantInput):
    """Input for creating an ambassador note."""

    ambassador_id: strawberry.ID
    note: str


@strawberry.input
class UpdateAmbassadorNoteInput(SparkGraphQLInput):
    """Input for updating an ambassador note."""

    note_id: strawberry.ID
    note: str | None = None


@strawberry.input
class DeleteAmbassadorNoteInput(SparkGraphQLInput):
    """Input for deleting an ambassador note."""

    note_id: strawberry.ID


@strawberry.input
class AmbassadorFileInput(SparkGraphQLInput):
    """Input for ambassador files."""

    name: str
    url: str | None = None
    main_resume: bool | None = None
    profile_pic: bool | None = None
    is_public: bool | None = None
    file_type_id: strawberry.ID | None = None


@strawberry.input
class AmbassadorTraitInput(SparkGraphQLInput):
    """Input for ambassador traits."""

    user_id: strawberry.ID


@strawberry.input
class AmbassadorSkillInput(SparkGraphQLInput):
    """Input for ambassador skills."""

    skill_id: strawberry.ID


@strawberry.input
class AmbassadorWorkHistoryInput(SparkGraphQLInput):
    """Input for ambassador work history."""

    user_id: strawberry.ID


@strawberry.input
class AmbassadorProfileNoteInput(BaseTenantInput):
    """Input for ambassador notes inside profile save."""

    note: str


@strawberry.input
class UpsertAmbassadorProfileInput(SparkGraphQLInput):
    """Input for creating/updating an ambassador profile and related data."""

    ambassador_id: strawberry.ID | None = None
    ambassador_uuid: strawberry.ID | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    location_id: strawberry.ID | None = None
    t_shirt_size: str | None = None
    address: str | None = None
    phone: str | None = None
    about_me: str | None = None
    image: str | None = None
    coordinates: List[float] | None = None
    is_active: bool | None = None
    rating: int | None = None
    files: list[AmbassadorFileInput] | None = None
    traits: list[AmbassadorTraitInput] | None = None
    skills: list[AmbassadorSkillInput] | None = None
    notes: list[AmbassadorProfileNoteInput] | None = None
    work_history: list[AmbassadorWorkHistoryInput] | None = None


@strawberry.input
class AmbassadorNoteFiltersInput:
    """Filters for ambassador note queries."""

    ambassador_id: strawberry.ID | None = None
    tenant_id: strawberry.ID | None = None
    tenant_uuid: strawberry.ID | None = None
    created_by_id: strawberry.ID | None = None
    start_date: str | None = None
    end_date: str | None = None
    search: str | None = None


@strawberry.input
class CreateSkillInput(BaseNameableInput):
    """Input for creating a skill."""

    pass  # name and tenant_id inherited from BaseNameableInput


@strawberry.input
class UpdateSkillInput(CreateSkillInput):
    """Input for updating a skill."""

    id: strawberry.ID


@strawberry.input
class DeleteSkillInput(SparkGraphQLInput):
    """Input for deleting a skill."""

    id: strawberry.ID


@strawberry.input
class SkillFiltersInput:
    """Filters for skill queries."""

    tenant_id: strawberry.ID | None = None
    tenant_uuid: strawberry.ID | None = None
    search: str | None = None


@strawberry.input
class CreateAmbassadorSkillInput(BaseTenantInput):
    """Input for creating an ambassador skill."""

    ambassador_id: strawberry.ID
    skill_id: strawberry.ID


@strawberry.input
class DeleteAmbassadorSkillInput(SparkGraphQLInput):
    """Input for deleting an ambassador skill."""

    ambassador_skill_id: strawberry.ID


@strawberry.input
class AmbassadorSkillFiltersInput:
    """Filters for ambassador skill queries."""

    ambassador_id: strawberry.ID | None = None
    skill_id: strawberry.ID | None = None
    tenant_id: strawberry.ID | None = None
    tenant_uuid: strawberry.ID | None = None


@strawberry.input
class CreateAttendanceTypeInput(BaseNameableInput):
    slug: str | None = None


@strawberry.input
class UpdateAttendanceTypeInput(CreateAttendanceTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateAttendanceStatusInput(BaseNameableInput):
    slug: str | None = None


@strawberry.input
class UpdateAttendanceStatusInput(CreateAttendanceStatusInput):
    id: strawberry.ID


@strawberry.input
class CreateSourceInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateSourceInput(CreateSourceInput):
    id: strawberry.ID


@strawberry.input
class CreateAttendanceInput(BaseTenantInput):
    clock_time: str
    coordinates: List[float] | None = None
    ambassador_id: strawberry.ID | None = None
    job_id: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    attendace_type_id: strawberry.ID | None = None
    attendance_status_id: strawberry.ID | None = None
    source_id: strawberry.ID | None = None
    timezone_id: strawberry.ID | None = None


@strawberry.input
class UpdateAttendanceInput(CreateAttendanceInput):
    id: strawberry.ID


@strawberry.input
class AttendanceFiltersInput(BaseTenantInput):
    """Filtros para attendances."""

    job_id: strawberry.ID | None = None
    ambassador_job_id: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    attendance_status_id: strawberry.ID | None = None
    source_id: strawberry.ID | None = None
    attendace_type_id: strawberry.ID | None = None


@strawberry.input
class ActiveAmbassadorFiltersInput:
    """Filters for active ambassadors."""

    email: str | None = None
    name: str | None = None


@strawberry.input
class CreateGroupTypeInput(SparkGraphQLInput):
    """Input for creating a group type."""

    name: str


@strawberry.input
class UpdateGroupTypeInput(CreateGroupTypeInput):
    """Input for updating a group type."""

    id: strawberry.ID


@strawberry.input
class DeleteGroupTypeInput(SparkGraphQLInput):
    """Input for deleting a group type."""

    id: strawberry.ID


@strawberry.input
class GroupTypeFiltersInput:
    """Filters for group type queries."""

    search: str | None = None


@strawberry.input
class CreateAmbassadorGroupInput(BaseNameableInput):
    """Input for creating an ambassador group."""

    job_id: strawberry.ID | None = None
    group_type_id: strawberry.ID
    description: str | None = None
    private: bool | None = None
    ambassador_ids: list[strawberry.ID] | None = None


@strawberry.input
class UpdateAmbassadorGroupInput(BaseNameableInput):
    """Input for updating an ambassador group."""

    id: strawberry.ID
    group_type_id: strawberry.ID | None = None
    job_id: strawberry.ID | None = None
    description: str | None = None
    private: bool | None = None
    ambassador_ids: list[strawberry.ID] | None = None


@strawberry.input
class DeleteAmbassadorGroupInput(SparkGraphQLInput):
    """Input for deleting an ambassador group."""

    id: strawberry.ID


@strawberry.input
class AmbassadorGroupFiltersInput:
    """Filters for ambassador group queries."""

    tenant_id: strawberry.ID | None = None
    search: str | None = None
    job_id: strawberry.ID | None = None
    job_uuid: strawberry.ID | None = None


@strawberry.input
class AddAmbassadorsToGroupInput(BaseTenantInput):
    """Input for adding ambassadors to a group."""

    job_id: strawberry.ID | None = None
    group_id: strawberry.ID
    ambassador_ids: list[strawberry.ID]


@strawberry.input
class RemoveAmbassadorsFromGroupInput(BaseTenantInput):
    """Input for removing ambassadors from a group."""

    group_id: strawberry.ID
    user_group_ids: list[strawberry.ID]


@strawberry.input
class AssignGroupToJobInput(BaseTenantInput):
    """Input for assigning an existing ambassador group to a job."""

    group_id: strawberry.ID
    job_id: strawberry.ID


@strawberry.input
class RegisterPushTokenInput(SparkGraphQLInput):
    """Input for the mobile app registering an Expo push token."""

    token: str
    platform: str  # "ios" | "android" | "web"
    device_name: str | None = None
    app_version: str | None = None


@strawberry.input
class AppleSignInInput(SparkGraphQLInput):
    """Sign in with Apple — identity token issued by Apple to the device."""

    id_token: str
    # Apple only sends name/email on the FIRST sign-in. The mobile client
    # caches whatever it got and resends it on every call so we can fill
    # in the user's name on initial account creation.
    first_name: str | None = None
    last_name: str | None = None


@strawberry.input
class GoogleSignInInput(SparkGraphQLInput):
    """Google ID token sign-in."""

    id_token: str


@strawberry.input
class InviteAmbassadorToShiftInput(SparkGraphQLInput):
    """Admin invites a specific BA to a specific event.

    Creates an AmbassadorEvent (is_approved=False), which fires the
    existing post_save signal → "New shift offered" push to the
    BA's mobile device.
    """

    ambassador_id: strawberry.ID
    event_id: strawberry.ID


@strawberry.input
class RateAmbassadorInput(SparkGraphQLInput):
    """Submit (or update) a 1-5 star rating for a BA.

    Both admins and clients can call this. `event_id` ties the rating
    to a specific gig; omit it for a general BA-profile rating. Re-rating
    the same (ambassador, event) by the same user updates the existing
    row rather than stacking a duplicate.
    """

    ambassador_id: strawberry.ID
    event_id: strawberry.ID | None = None
    score: int  # 1-5
    comment: str | None = None


@strawberry.input
class CancelShiftInviteInput(SparkGraphQLInput):
    """Admin retracts a pending shift invite (or removes an accepted
    BA from a shift). Deletes the AmbassadorEvent row.

    For "pending" rows, this is symmetric with the BA's decline path
    (same delete). For "accepted" rows, this is effectively kicking
    the BA off the shift — admins should confirm before calling.
    """

    ambassador_event_uuid: strawberry.ID


@strawberry.input
class RespondToShiftOfferInput(SparkGraphQLInput):
    """BA's response to a shift invitation pushed from the admin app.

    Created by an admin (AmbassadorEvent with is_approved=False).
    Mobile renders the offer + Accept/Decline. Accept flips
    is_approved=True; decline removes the invitation row.
    """

    ambassador_event_uuid: strawberry.ID
    accepted: bool


@strawberry.input
class LocationPingInput(SparkGraphQLInput):
    """A GPS reading the spark-mobile activation tracker fires every
    ~2 min during an active shift. The mobile client supplies the
    Event uuid so we can scope the ping to the shift the BA is on,
    plus the recorded-at timestamp so freshness math doesn't depend
    on the server clock."""

    event_uuid: strawberry.ID
    lat: float
    lng: float
    accuracy_meters: float | None = None
    # ISO-8601. If omitted we fall back to the server clock.
    recorded_at: str | None = None
    # "foreground" | "background" | "clock_in" | "clock_out"
    source: str | None = "background"
