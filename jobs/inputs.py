import strawberry
from typing import List

from utils.graphql.inputs import SparkGraphQLInput


from utils.graphql.inputs import BaseNameableInput, BaseTenantInput


@strawberry.input
class CreateStatusInput(BaseNameableInput):
    pass


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
class CreateCompanyInput(BaseNameableInput):
    email: str
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
class CreateCompanyReviewInput(BaseNameableInput):
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
class CreatePayTimingInput(BaseNameableInput):
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
class CreateRateInput(BaseNameableInput):
    amount: float
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
    job_title_id: strawberry.ID
    other_title_id: strawberry.ID | None = None
    company_id: strawberry.ID
    event_id: strawberry.ID
    location_id: strawberry.ID
    rate_id: strawberry.ID | None = None


@strawberry.input
class UpdateJobInput(CreateJobInput):
    id: strawberry.ID


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
