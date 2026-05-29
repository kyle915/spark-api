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
class MyEarningsStats:
    """Lightweight per-BA earnings snapshot used by the mobile Earnings
    tab. No dollar figures: Spark doesn't own the payroll system. We
    surface a real shift count and the hour estimate so the BA can
    sanity-check their Wingspan/Gusto payouts.
    """

    # Number of approved AmbassadorEvent rows whose event.date falls
    # within the window.
    shifts_count: int
    # Sum of (event.end_time - event.start_time) across those shifts,
    # expressed as decimal hours. None when no shifts are eligible.
    hours_estimate: float | None
    # The lookback window the numbers were computed over.
    within_days: int


@strawberry.type
class EarningsShiftRow:
    """One completed shift in the BA's earnings breakdown (#194).

    Sourced entirely from AmbassadorEvent + Event — data the BA already
    owns. Deliberately carries NO fabricated dollars: Spark does not own
    payroll (Wingspan does, keyed only by contractor email with no link
    back to a specific shift), so `gross` is always None and
    `payment_status` is always "not_available" until a real payment->shift
    join exists. The mobile UI renders hours/blocks (real) and a neutral
    "Paid via Wingspan" status pill instead of inventing a number.
    """

    # Stable id for React keys / future payment correlation.
    ambassador_event_uuid: strawberry.ID
    event_uuid: strawberry.ID
    # Human label: Event.name, falling back to retailer name.
    venue: str
    # ISO date of the shift (Event.date), e.g. "2026-05-20".
    date: str | None
    start_time: str | None  # ISO datetime
    end_time: str | None    # ISO datetime
    state_code: str | None
    # Decimal hours for THIS shift (end - start). None if either bound
    # is missing. Same math as myEarningsStats, per-row.
    hours: float | None
    # Whole-block proxy used in field-marketing scheduling: ceil(hours/4),
    # min 1 when hours>0. None when hours is None. Pure presentation.
    blocks: int | None
    # ALWAYS None today — see class docstring. Typed so the field can be
    # populated later without a schema change.
    gross: float | None
    # ALWAYS "not_available" today. Enum-ish string the UI maps to a pill:
    # not_available | pending | paid. Kept forward-compatible.
    payment_status: str


@strawberry.type
class MyEarningsBreakdown:
    """Per-shift earnings breakdown for the mobile Earnings tab (#194).

    Header totals (shift count, hours) intentionally mirror
    MyEarningsStats so the screen can show one consistent summary, then
    list the rows that make it up.
    """

    within_days: int
    shifts_count: int
    hours_total: float | None
    # True the moment Spark can show a real per-shift dollar figure /
    # payment status. False today so the UI shows the honest Wingspan
    # explainer instead of empty money columns.
    payments_available: bool
    rows: list[EarningsShiftRow]


@strawberry.type
class MyRatingRecent:
    """One recent star rating, BA-facing (#197). `event_name` is the gig
    the rating was about (null for a general, non-gig rating)."""

    score: int
    comment: str | None
    created_at: str
    event_name: str | None


@strawberry.type
class MyRatingSummary:
    """BA-facing ratings + reliability snapshot for the mobile Profile
    card (#197). Read-only; computed live off AmbassadorRating +
    Attendance(clock_in) + Recap. No dollar / PII leakage.
    """

    # ---- ratings ----
    average: float            # mean of ALL ratings (admin + client), 1dp; 0.0 when none
    count: int                # total number of ratings counted into `average`
    recent: list[MyRatingRecent]   # newest 5

    # ---- reliability streak ----
    # Consecutive most-recent completed shifts with an on-time clock-in
    # (clock_time <= start_time + 10m grace) AND a filed recap.
    current_streak: int
    best_streak: int          # longest such run in history (scan-bounded)
    # Did the single most recent completed shift pass the on-time test?
    # None when the BA has no completed shifts yet.
    last_shift_on_time: bool | None


@strawberry.type
class ShiftProduct:
    """One product the BA will be repping on a shift, surfaced on the
    mobile shift-detail "BRAND & PRODUCTS" card. Sourced from
    RequestProduct -> Product on the event's parent Request. Read-only:
    admins manage products in the existing request flows.
    """

    name: str
    # Public (non-signed) URL for the product image, via utils.gcs
    # public_url — same resolution the web Product type uses. None when
    # the product has no image uploaded.
    image_url: str | None = None


@strawberry.type
class ShiftContext:
    """Brand / project / product context for a single shift, shown on the
    mobile shift-detail screen next to the pre-shift briefing (#191).

    Purely additive read-only display: every field is derived from the
    event's parent Request (Request.client / client_name, Request.notes,
    and RequestProduct -> Product). The editable surfaces stay where they
    already are — admins edit brand + products in the request flows, and
    the free-form shift text is the existing briefing builder. Resolves
    gracefully to null fields + an empty product list when the event has
    no request attached.
    """

    # Request.client.name, falling back to Request.client_name. None when
    # neither is set.
    brand_name: str | None = None
    products: list[ShiftProduct] = strawberry.field(default_factory=list)
    # Request.notes — free-form project notes. None / empty when unset.
    project_notes: str | None = None


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


@strawberry_django.type(models.AmbassadorRating)
class AmbassadorRatingType(Node):
    """A single 1-5 star rating left for a BA on a gig.

    `by_client` distinguishes a client-submitted rating from an Ignite
    admin one — the query layer uses it to hide client ratings from
    other clients. `rater_name` is a convenience for the UI timeline.
    """

    uuid: str
    score: int
    comment: str | None
    by_client: bool
    ambassador_id: strawberry.ID
    event_id: strawberry.ID | None
    tenant_id: strawberry.ID | None
    created_at: str
    updated_at: str

    @strawberry.field
    async def rater_name(self) -> str:
        """Display name of whoever left the rating (first+last, else email)."""

        def _name(obj):
            u = obj.created_by
            if u is None:
                return ""
            full = " ".join(
                filter(None, [getattr(u, "first_name", ""), getattr(u, "last_name", "")])
            ).strip()
            return full or getattr(u, "email", "") or ""

        return await sync_to_async(_name)(self)


@strawberry.type
class RateAmbassadorResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_rating: AmbassadorRatingType | None = None
    # Recomputed BA-level aggregate so the UI can update the star
    # average without a refetch. `ambassador_average` is the mean of
    # all ratings (admin + client); `ambassador_rating_count` is the
    # total number of ratings counted into it.
    ambassador_average: float = 0.0
    ambassador_rating_count: int = 0


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
class CancelShiftInviteResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    # The deleted row's uuid, echoed back so the front-end can update
    # its local cache without re-fetching the roster.
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
    # [latitude, longitude] from Event.coordinates. None when the
    # event hasn't been geocoded yet (admin can still see the address
    # on the card; map view falls back to address-only). Mobile uses
    # this on ShiftDetailScreen to drop a venue pin + launch the
    # native maps app with directions. Defaulted so existing callsites
    # (myPendingOffers, single shift-offer lookup) don't need to pass
    # them — only my_upcoming_shifts populates them today.
    latitude: float | None = None
    longitude: float | None = None
    # Pre-formatted, human-readable date/time labels in the EVENT's
    # timezone (the venue's local time), so the mobile client renders
    # them verbatim instead of converting the raw datetimes against the
    # device clock — which showed a NY 10:30 PM shift as 5:30 AM on a CA
    # phone. Formatting is DST-aware via utils.tz.apply_dst_aware_offset
    # (same helper the email/recap formatters use); falls back to
    # server/local time when the event has no resolvable timezone.
    # Emitted as camelCase: dateLabel / startLabel / endLabel. Examples:
    #   date_label  → "Tue, May 28"
    #   start_label → "10:15 PM"
    #   end_label   → "10:30 PM"
    # Defaulted so existing callsites (shift_offer, my_pending_offers)
    # that don't populate them keep working unchanged.
    date_label: str | None = None
    start_label: str | None = None
    end_label: str | None = None


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
