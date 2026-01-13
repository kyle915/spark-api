import strawberry
from enum import Enum
from typing import List

from utils.graphql.inputs import SparkGraphQLInput


from utils.graphql.inputs import BaseNameableInput, BaseTenantInput


@strawberry.input
class CreateStatusInput(BaseNameableInput):
    slug: str | None = None


@strawberry.input
class UpdateStatusInput(CreateStatusInput):
    id: strawberry.ID


@strawberry.input
class CreateCompanyFileInput(BaseNameableInput):
    file_type_id: strawberry.ID
    url: str | None = None


@strawberry.input
class UpdateCompanyFileInput(CreateCompanyFileInput):
    id: strawberry.ID


@strawberry.input
class CreateCompanyInput(BaseTenantInput):
    email: str
    name: str
    website_url: str | None = None
    founding_date: str | None = None
    phone: str
    address: str | None = None
    about_us: str | None = None
    company_size_min: int | None = None
    company_size_max: int | None = None
    approved: bool = False
    location_id: strawberry.ID | None = None
    cover_id: strawberry.ID | None = None
    profile_image_id: strawberry.ID | None = None


@strawberry.input
class UpdateCompanyInput(CreateCompanyInput):
    id: strawberry.ID


@strawberry.input
class CreateCompanyReviewInput(BaseTenantInput):
    company_id: strawberry.ID
    global_score: int
    review: str
    min_pay_timing: int
    max_pay_timing: int
    pay_timing_range: int
    ambassador_id: strawberry.ID


@strawberry.input
class UpdateCompanyReviewInput(CreateCompanyReviewInput):
    id: strawberry.ID


@strawberry.input
class CreatePayTimingInput(BaseTenantInput):
    min_pay_timing: int
    max_pay_timing: int
    unit: str
    company_review_id: strawberry.ID


@strawberry.input
class UpdatePayTimingInput(CreatePayTimingInput):
    id: strawberry.ID


@strawberry.input
class CreateReviewScoreInput(BaseNameableInput):
    score: int
    company_review_id: strawberry.ID


@strawberry.input
class UpdateReviewScoreInput(CreateReviewScoreInput):
    id: strawberry.ID


@strawberry.input
class CreateJobTitleInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateJobTitleInput(CreateJobTitleInput):
    id: strawberry.ID


@strawberry.input
class CreateRateTypeInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateRateTypeInput(CreateRateTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateRateInput(BaseTenantInput):
    amount: float  # Note: matches model field name (typo in model)
    rate_type_id: strawberry.ID


@strawberry.input
class UpdateRateInput(CreateRateInput):
    id: strawberry.ID


@strawberry.input
class CreateJobInput(BaseNameableInput):
    description: str
    code: str
    address: str
    start_date: str | None = None
    end_date: str | None = None
    public: bool = False
    closed: bool = False
    national: bool = False
    ongoing: bool = False
    coordinates: List[float] | None = None
    job_title_id: strawberry.ID
    other_title_id: strawberry.ID | None = None
    event_id: strawberry.ID
    rate_id: strawberry.ID | None = None


@strawberry.input
class UpdateJobInput(CreateJobInput):
    id: strawberry.ID


@strawberry.enum
class JobStatusFilter(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    INVITED = "invited"


@strawberry.input
class JobFiltersInput(BaseTenantInput):
    event_id: strawberry.ID | None = None
    status: JobStatusFilter | None = None
    statuses: list[JobStatusFilter] | None = None


@strawberry.input
class RateTypeFiltersInput(BaseTenantInput):
    pass


@strawberry.input
class CreateJobFileInput(BaseNameableInput):
    url: str
    job_id: strawberry.ID
    file_type_id: strawberry.ID


@strawberry.input
class UpdateJobFileInput(CreateJobFileInput):
    id: strawberry.ID


@strawberry.input
class CreateJobRequirementTypeInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateJobRequirementTypeInput(CreateJobRequirementTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateJobRequirementInput(BaseNameableInput):
    job_requirement_type_id: strawberry.ID
    job_id: strawberry.ID


@strawberry.input
class UpdateJobRequirementInput(CreateJobRequirementInput):
    id: strawberry.ID


@strawberry.input
class CreateJobRequirementFileInput(BaseNameableInput):
    url: str
    job_requirement_id: strawberry.ID
    file_type_id: strawberry.ID


@strawberry.input
class UpdateJobRequirementFileInput(CreateJobRequirementFileInput):
    id: strawberry.ID


@strawberry.input
class CreateAmbassadorJobInput(BaseTenantInput):
    appear_as_rfp: bool = True
    ambassador_id: strawberry.ID
    job_id: strawberry.ID
    status_id: strawberry.ID
    rate_id: strawberry.ID


@strawberry.input
class UpdateAmbassadorJobInput(CreateAmbassadorJobInput):
    id: strawberry.ID


@strawberry.input
class CreateAmbassadorJobStatusInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateAmbassadorJobStatusInput(CreateAmbassadorJobStatusInput):
    id: strawberry.ID


@strawberry.input
class CreateCompanyToAmbassadorReviewInput(BaseTenantInput):
    description: str
    rate: float
    ambassador_id: strawberry.ID
    job_id: strawberry.ID


@strawberry.input
class UpdateCompanyToAmbassadorReviewInput(CreateCompanyToAmbassadorReviewInput):
    id: strawberry.ID


@strawberry.input
class CreateAmbassadorToAmbassadorReviewInput(BaseTenantInput):
    description: str
    rate: float
    ambassador_id: strawberry.ID


@strawberry.input
class UpdateAmbassadorToAmbassadorReviewInput(CreateAmbassadorToAmbassadorReviewInput):
    id: strawberry.ID


@strawberry.input
class CreateQuestionTypeInput(BaseNameableInput):
    pass


@strawberry.input
class UpdateQuestionTypeInput(CreateQuestionTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateJobRequirementQuestionInput(BaseTenantInput):
    question: str
    job_requirement_id: strawberry.ID
    question_type_id: strawberry.ID


@strawberry.input
class UpdateJobRequirementQuestionInput(CreateJobRequirementQuestionInput):
    id: strawberry.ID


@strawberry.input
class CreateQuestionOptionInput(BaseTenantInput):
    option: str
    job_requirement_question_id: strawberry.ID


@strawberry.input
class UpdateQuestionOptionInput(CreateQuestionOptionInput):
    id: strawberry.ID


@strawberry.input
class CreateJobRequirementAnswerInput(BaseTenantInput):
    selected_answer: List[int]
    job_requirement_question_id: strawberry.ID
    ambassador_id: strawberry.ID


@strawberry.input
class UpdateJobRequirementAnswerInput(CreateJobRequirementAnswerInput):
    id: strawberry.ID


@strawberry.enum
class ManageAmbassadorJobAssignmentAction(Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    BLACKLIST = "BLACKLIST"
    WHITELIST = "WHITELIST"


@strawberry.input
class ManageAmbassadorJobAssignmentInput(BaseTenantInput):
    ambassador_job_id: strawberry.ID
    action: ManageAmbassadorJobAssignmentAction
    status_id: strawberry.ID | None = None


@strawberry.enum
class AmbassadorJobStatusEnum(Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    PENDING = "pending"
    INVITED = 'invited'


@strawberry.input
class ApproveAmbassadorJobInput(BaseTenantInput):
    ambassador_job_id: strawberry.ID


@strawberry.input
class DeclineAmbassadorJobInput(BaseTenantInput):
    ambassador_job_id: strawberry.ID


@strawberry.input
class InviteAmbassadorsToJobInput(BaseTenantInput):
    ambassador_ids: List[strawberry.ID]
    job_id: strawberry.ID
