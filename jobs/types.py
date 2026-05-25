from __future__ import annotations

import strawberry_django
import strawberry
from strawberry.relay import Node
from typing import List, Optional
from asgiref.sync import sync_to_async

from . import models
from events.types import Location, Event
from tenants.types import TenantType as Tenant
from ambassadors.types import Ambassador, Attendance


@strawberry_django.type(models.Status)
class Status(Node):
    uuid: str
    name: str
    slug: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class StatusDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    status: Status | None = None


@strawberry_django.type(models.CompanyFile)
class CompanyFile(Node):
    uuid: str
    name: str
    url: str | None
    file_type_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class CompanyFileDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    company_file: CompanyFile | None = None


@strawberry_django.type(models.Company)
class Company(Node):
    uuid: str
    name: str
    email: str
    website_url: str | None
    founding_date: str | None
    phone: str
    address: str | None
    about_us: str | None
    company_size_min: int | None
    company_size_max: int | None
    approved: bool
    tenant_id: strawberry.ID | None = None
    location: Location | None = None
    cover: CompanyFile | None = None
    profile_image: CompanyFile | None = None
    created_at: str
    updated_at: str


@strawberry.type
class CompanyDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    company: Company | None = None


@strawberry_django.type(models.CompanyReview)
class CompanyReview(Node):
    uuid: str
    global_score: int
    review: str
    min_pay_timing: int
    max_pay_timing: int
    pay_timing_range: int
    company: Company
    ambassador_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class CompanyReviewDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    company_review: CompanyReview | None = None


@strawberry_django.type(models.PayTiming)
class PayTiming(Node):
    uuid: str
    min_pay_timing: int
    max_pay_timing: int
    unit: str
    company_review: CompanyReview
    created_at: str
    updated_at: str


@strawberry.type
class PayTimingDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    pay_timing: PayTiming | None = None


@strawberry_django.type(models.ReviewScore)
class ReviewScore(Node):
    uuid: str
    name: str | None
    score: int | None
    company_review: CompanyReview
    created_at: str
    updated_at: str


@strawberry.type
class ReviewScoreDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    review_score: ReviewScore | None = None


@strawberry_django.type(models.JobTitle)
class JobTitle(Node):
    uuid: str
    name: str | None
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class JobTitleDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_title: JobTitle | None = None


@strawberry_django.type(models.RateType)
class RateType(Node):
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class RateTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    rate_type: RateType | None = None


@strawberry_django.type(models.Rate)
class Rate(Node):
    uuid: str
    amount: float  # Note: typo in model field name (DecimalField)
    tenant_id: strawberry.ID
    rate_type: RateType
    created_at: str
    updated_at: str


@strawberry.type
class RateDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    rate: Rate | None = None


@strawberry_django.type(models.Job)
class Job(Node):
    uuid: str
    name: str
    description: str | None
    code: str | None
    address: str
    start_date: str | None
    end_date: str | None
    public: bool
    closed: bool
    national: bool
    ongoing: bool
    coordinates: List[float] | None
    extension_rate: float | None
    # Lifecycle fields — drive the admin Jobs page columns.
    # Stored as a CharField so the GraphQL type stays a plain string.
    lifecycle_status: str
    total_hours: float | None
    hourly_rate: float | None
    uniform_notes: str | None
    favorites_only: bool
    posted_at: str | None
    max_applications: int | None
    job_title: JobTitle
    other_title: JobTitle | None
    event: Event
    tenant_id: strawberry.ID
    tenant: Tenant | None = None
    rate: Rate
    job_requirements: List[JobRequirement]
    created_at: str
    updated_at: str

    @strawberry.field
    async def attendances(self) -> List[Attendance]:
        """Attendance records linked to this job."""
        return await sync_to_async(list)(self.attendance.all())

    @strawberry.field
    async def ambassador_jobs(self) -> List[AmbassadorJob]:
        """Ambassador assignments linked to this job."""
        return await sync_to_async(list)(self.ambassador_jobs.all())

    @strawberry.field(name="applied")
    def resolve_applied(self) -> bool:
        """Whether the current ambassador user already applied to this job."""
        return bool(getattr(self, "applied", False))

    @strawberry.field
    async def applications_count(self) -> int:
        """Number of BA applications attached to this job, across all
        statuses (applied + accepted + declined + withdrawn). Used by
        the admin Jobs board to show "N applicants" per row without a
        round-trip per job."""
        def _count() -> int:
            try:
                return int(self.applications.count())
            except Exception:
                return 0
        return await sync_to_async(_count)()

    @strawberry.field
    async def applied_count(self) -> int:
        """Just the ones in `applied` status — i.e. waiting on admin
        decision. Drives the orange "N pending" pill on Posted rows."""
        def _count() -> int:
            try:
                return int(
                    self.applications.filter(status="applied").count()
                )
            except Exception:
                return 0
        return await sync_to_async(_count)()

    @strawberry.field
    async def briefing(self) -> "JobBriefingPayload":
        """The job's BA briefing — title, body, attachments. Always
        returns a payload (even when empty) so mobile clients can
        unconditionally render the briefing section."""
        def _build():
            title = getattr(self, "briefing_title", None) or ""
            body = getattr(self, "briefing_body", None) or ""
            tpl_uuid = None
            tpl_id = getattr(self, "briefing_template_id", None)
            if tpl_id:
                try:
                    tpl_uuid = str(self.briefing_template.uuid)
                except Exception:
                    tpl_uuid = None
            try:
                atts = list(self.briefing_attachments.all())
            except Exception:
                atts = []
            return JobBriefingPayload(
                title=title,
                body=body,
                template_uuid=tpl_uuid,
                attachments=atts,
            )
        return await sync_to_async(_build)()


@strawberry.type
class JobDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job: Job | None = None


@strawberry_django.type(models.JobFile)
class JobFile(Node):
    uuid: str
    name: str
    url: str
    job: Job
    file_type_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class JobFileDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_file: JobFile | None = None


@strawberry_django.type(models.JobRequirementType)
class JobRequirementType(Node):
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class JobRequirementTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_requirement_type: JobRequirementType | None = None


@strawberry_django.type(models.JobRequirement)
class JobRequirement(Node):
    uuid: str
    name: str
    tenant_id: strawberry.ID
    job_requirement_type: JobRequirementType
    job: "Job"
    created_at: str
    updated_at: str


@strawberry.type
class JobRequirementDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_requirement: JobRequirement | None = None


@strawberry_django.type(models.JobRequirementFile)
class JobRequirementFile(Node):
    uuid: str
    name: str
    url: str
    job_requirement: JobRequirement
    file_type_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class JobRequirementFileDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_requirement_file: JobRequirementFile | None = None


@strawberry_django.type(models.AmbassadorJob)
class AmbassadorJob(Node):
    uuid: str
    accepted_terms: bool
    appear_as_rfp: bool
    time_blocks_15m: int
    real_amount: float | None
    tenant_id: strawberry.ID
    ambassador_id: strawberry.ID
    ambassador: Ambassador
    job: Job
    status: Status
    rate: Rate
    created_at: str
    updated_at: str


@strawberry.type
class AmbassadorJobDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_job: AmbassadorJob | None = None


@strawberry.type
class DeleteAmbassadorJobResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry_django.type(models.CompanyToAmbassadorReview)
class CompanyToAmbassadorReview(Node):
    uuid: str
    description: str
    rate: float  # DecimalField
    tenant_id: strawberry.ID
    ambassador_id: strawberry.ID
    job: Job
    created_at: str
    updated_at: str


@strawberry.type
class CompanyToAmbassadorReviewDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    company_to_ambassador_review: CompanyToAmbassadorReview | None = None


@strawberry_django.type(models.AmbassadorToAmbassadorReview)
class AmbassadorToAmbassadorReview(Node):
    uuid: str
    description: str
    rate: float  # DecimalField
    tenant_id: strawberry.ID
    ambassador_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class AmbassadorToAmbassadorReviewDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_to_ambassador_review: AmbassadorToAmbassadorReview | None = None


@strawberry_django.type(models.QuestionType)
class QuestionType(Node):
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class QuestionTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    question_type: QuestionType | None = None


@strawberry_django.type(models.JobRequirementQuestion)
class JobRequirementQuestion(Node):
    uuid: str
    question: str
    tenant_id: strawberry.ID
    job_requirement: JobRequirement
    question_type: QuestionType
    created_at: str
    updated_at: str


@strawberry.type
class JobRequirementQuestionDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_requirement_question: JobRequirementQuestion | None = None


@strawberry_django.type(models.QuestionOption)
class QuestionOption(Node):
    uuid: str
    option: str
    tenant_id: strawberry.ID
    job_requirement_question: JobRequirementQuestion
    created_at: str
    updated_at: str


@strawberry.type
class QuestionOptionDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    question_option: QuestionOption | None = None


@strawberry_django.type(models.JobRequirementAnswer)
class JobRequirementAnswer(Node):
    uuid: str
    selected_answer: List[int]
    tenant_id: strawberry.ID
    job_requirement_question: JobRequirementQuestion
    ambassador_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class JobRequirementAnswerDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_requirement_answer: JobRequirementAnswer | None = None


@strawberry.type
class InviteAmbassadorsToJobResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    ambassador_jobs: List[AmbassadorJob] | None = None


# -------------------------------------------------------------------
# Job lifecycle types
# -------------------------------------------------------------------

@strawberry_django.type(models.JobApplication)
class JobApplication:
    uuid: str
    status: str
    note: str
    applied_at: str
    decided_at: str | None

    @strawberry.field
    def ambassador_first_name(self) -> str:
        a = self.__dict__.get("ambassador")
        if not a:
            return ""
        u = getattr(a, "user", None)
        return (getattr(u, "first_name", None) or "") if u else ""

    @strawberry.field
    def ambassador_last_name(self) -> str:
        a = self.__dict__.get("ambassador")
        if not a:
            return ""
        u = getattr(a, "user", None)
        return (getattr(u, "last_name", None) or "") if u else ""

    @strawberry.field
    def ambassador_email(self) -> str:
        a = self.__dict__.get("ambassador")
        if not a:
            return ""
        u = getattr(a, "user", None)
        return (getattr(u, "email", None) or "") if u else ""

    @strawberry.field
    def ambassador_uuid(self) -> str:
        a = self.__dict__.get("ambassador")
        return str(a.uuid) if a and getattr(a, "uuid", None) else ""


@strawberry.type
class JobApplicationResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    application_uuid: str | None = None


@strawberry.type
class FavoriteAmbassadorResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class JobLifecycleResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_uuid: str | None = None
    lifecycle_status: str | None = None


# -------------------------------------------------------------------
# BA Briefing types — template + per-job + attachments
# -------------------------------------------------------------------

@strawberry_django.type(models.BriefingTemplateAttachment)
class BriefingTemplateAttachment:
    uuid: str
    name: str
    url: str
    content_type: str
    size: int | None
    created_at: str


@strawberry_django.type(models.BriefingTemplate)
class BriefingTemplate:
    uuid: str
    name: str
    title: str
    body: str
    is_archived: bool
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str

    @strawberry.field
    def attachments(self) -> List[BriefingTemplateAttachment]:
        # `attachments` related-manager is pre-fetched on the list
        # resolver. Falling back to a query here keeps this safe in
        # single-object lookups too.
        try:
            return list(self.attachments.all())
        except Exception:
            return []


@strawberry_django.type(models.JobBriefingAttachment)
class JobBriefingAttachment:
    uuid: str
    name: str
    url: str
    content_type: str
    size: int | None
    created_at: str


@strawberry.type
class JobBriefingPayload:
    """Computed bundle representing a Job's briefing — the title, body,
    and the per-job attachments. Surfaced as `job.briefing` so the
    mobile/admin clients don't have to glue the two fields together."""
    title: str
    body: str
    template_uuid: str | None
    attachments: List[JobBriefingAttachment]


@strawberry.type
class BriefingTemplateResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    briefing_template: BriefingTemplate | None = None


@strawberry.type
class JobBriefingResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    job_uuid: str | None = None
    title: str | None = None


# -------------------------------------------------------------------
# Tenant favorite ambassador types (Favorites tab UI)
# -------------------------------------------------------------------

@strawberry_django.type(models.TenantFavoriteAmbassador)
class TenantFavoriteAmbassador:
    uuid: str
    tenant_id: strawberry.ID
    note: str
    created_at: str

    @strawberry.field
    def ambassador_uuid(self) -> str:
        a = self.__dict__.get("ambassador") or getattr(self, "_ambassador_cache", None)
        if not a:
            try:
                a = self.ambassador
            except Exception:
                a = None
        return str(a.uuid) if a and getattr(a, "uuid", None) else ""

    @strawberry.field
    def ambassador_id(self) -> strawberry.ID:
        a = self.__dict__.get("ambassador") or getattr(self, "_ambassador_cache", None)
        if not a:
            try:
                a = self.ambassador
            except Exception:
                a = None
        return strawberry.ID(str(a.id)) if a and getattr(a, "id", None) else strawberry.ID("")

    @strawberry.field
    def first_name(self) -> str:
        a = self.__dict__.get("ambassador")
        u = getattr(a, "user", None) if a else None
        return (getattr(u, "first_name", None) or "") if u else ""

    @strawberry.field
    def last_name(self) -> str:
        a = self.__dict__.get("ambassador")
        u = getattr(a, "user", None) if a else None
        return (getattr(u, "last_name", None) or "") if u else ""

    @strawberry.field
    def email(self) -> str:
        a = self.__dict__.get("ambassador")
        u = getattr(a, "user", None) if a else None
        return (getattr(u, "email", None) or "") if u else ""
