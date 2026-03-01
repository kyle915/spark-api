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
