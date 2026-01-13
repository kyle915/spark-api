import strawberry
from django.db.models import Model
from strawberry import relay
from graphql import GraphQLError
from asgiref.sync import sync_to_async

from jobs import models, inputs, types
from utils.graphql.mixins import (
    BaseMutationService,
    SparkGraphQLMixin,
    resolve_id_to_int,
)
from utils.graphql.permissions import StrictIsAuthenticated, IsClientOrSparkAdmin
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import build_mutation_response

ensure_relay_mutation()


# Status Mutations
class StatusMutationService(BaseMutationService):
    """Service for status mutations."""
    response_class = types.StatusDetailResponse
    model_field_name = "status"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Status


@strawberry.type
class StatusMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_ambassador_job_status(
        self,
        info: strawberry.Info,
        input: inputs.CreateStatusInput,
    ) -> types.StatusDetailResponse:
        return await StatusMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_ambassador_job_status(
        self,
        info: strawberry.Info,
        input: inputs.UpdateStatusInput,
    ) -> types.StatusDetailResponse:
        return await StatusMutationService.update(input, info)


# CompanyFile Mutations
class CompanyFileMutationService(BaseMutationService):
    """Service for company file mutations."""
    response_class = types.CompanyFileDetailResponse
    model_field_name = "company_file"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.CompanyFile


@strawberry.type
class CompanyFileMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_company_file(
        self,
        info: strawberry.Info,
        input: inputs.CreateCompanyFileInput,
    ) -> types.CompanyFileDetailResponse:
        return await CompanyFileMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_company_file(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCompanyFileInput,
    ) -> types.CompanyFileDetailResponse:
        return await CompanyFileMutationService.update(input, info)


# Company Mutations
class CompanyMutationService(BaseMutationService):
    """Service for company mutations."""
    response_class = types.CompanyDetailResponse
    model_field_name = "company"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Company


@strawberry.type
class CompanyMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_company(
        self,
        info: strawberry.Info,
        input: inputs.CreateCompanyInput,
    ) -> types.CompanyDetailResponse:
        return await CompanyMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_company(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCompanyInput,
    ) -> types.CompanyDetailResponse:
        return await CompanyMutationService.update(input, info)


# CompanyReview Mutations
class CompanyReviewMutationService(BaseMutationService):
    """Service for company review mutations."""
    response_class = types.CompanyReviewDetailResponse
    model_field_name = "company_review"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.CompanyReview


@strawberry.type
class CompanyReviewMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_company_review(
        self,
        info: strawberry.Info,
        input: inputs.CreateCompanyReviewInput,
    ) -> types.CompanyReviewDetailResponse:
        return await CompanyReviewMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_company_review(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCompanyReviewInput,
    ) -> types.CompanyReviewDetailResponse:
        return await CompanyReviewMutationService.update(input, info)


# PayTiming Mutations
class PayTimingMutationService(BaseMutationService):
    """Service for pay timing mutations."""
    response_class = types.PayTimingDetailResponse
    model_field_name = "pay_timing"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.PayTiming


@strawberry.type
class PayTimingMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_pay_timing(
        self,
        info: strawberry.Info,
        input: inputs.CreatePayTimingInput,
    ) -> types.PayTimingDetailResponse:
        return await PayTimingMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_pay_timing(
        self,
        info: strawberry.Info,
        input: inputs.UpdatePayTimingInput,
    ) -> types.PayTimingDetailResponse:
        return await PayTimingMutationService.update(input, info)


# ReviewScore Mutations
class ReviewScoreMutationService(BaseMutationService):
    """Service for review score mutations."""
    response_class = types.ReviewScoreDetailResponse
    model_field_name = "review_score"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.ReviewScore


@strawberry.type
class ReviewScoreMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_review_score(
        self,
        info: strawberry.Info,
        input: inputs.CreateReviewScoreInput,
    ) -> types.ReviewScoreDetailResponse:
        return await ReviewScoreMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_review_score(
        self,
        info: strawberry.Info,
        input: inputs.UpdateReviewScoreInput,
    ) -> types.ReviewScoreDetailResponse:
        return await ReviewScoreMutationService.update(input, info)


# JobTitle Mutations
class JobTitleMutationService(BaseMutationService):
    """Service for job title mutations."""
    response_class = types.JobTitleDetailResponse
    model_field_name = "job_title"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobTitle


@strawberry.type
class JobTitleMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_job_title(
        self,
        info: strawberry.Info,
        input: inputs.CreateJobTitleInput,
    ) -> types.JobTitleDetailResponse:
        return await JobTitleMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_job_title(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobTitleInput,
    ) -> types.JobTitleDetailResponse:
        return await JobTitleMutationService.update(input, info)


# RateType Mutations
class RateTypeMutationService(BaseMutationService):
    """Service for rate type mutations."""
    response_class = types.RateTypeDetailResponse
    model_field_name = "rate_type"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.RateType


@strawberry.type
class RateTypeMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_rate_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateRateTypeInput,
    ) -> types.RateTypeDetailResponse:
        return await RateTypeMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_rate_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRateTypeInput,
    ) -> types.RateTypeDetailResponse:
        return await RateTypeMutationService.update(input, info)


# Rate Mutations
class RateMutationService(BaseMutationService):
    """Service for rate mutations."""
    response_class = types.RateDetailResponse
    model_field_name = "rate"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Rate


@strawberry.type
class RateMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_rate(
        self,
        info: strawberry.Info,
        input: inputs.CreateRateInput,
    ) -> types.RateDetailResponse:
        return await RateMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_rate(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRateInput,
    ) -> types.RateDetailResponse:
        return await RateMutationService.update(input, info)


# Job Mutations
class JobMutationService(BaseMutationService):
    """Service for job mutations."""
    response_class = types.JobDetailResponse
    model_field_name = "job"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Job


@strawberry.type
class JobMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_job(
        self,
        info: strawberry.Info,
        input: inputs.CreateJobInput,
    ) -> types.JobDetailResponse:
        return await JobMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_job(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobInput,
    ) -> types.JobDetailResponse:
        return await JobMutationService.update(input, info)


# JobFile Mutations
class JobFileMutationService(BaseMutationService):
    """Service for job file mutations."""
    response_class = types.JobFileDetailResponse
    model_field_name = "job_file"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobFile


@strawberry.type
class JobFileMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_job_file(
        self,
        info: strawberry.Info,
        input: inputs.CreateJobFileInput,
    ) -> types.JobFileDetailResponse:
        return await JobFileMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_job_file(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobFileInput,
    ) -> types.JobFileDetailResponse:
        return await JobFileMutationService.update(input, info)


# JobRequirementType Mutations
class JobRequirementTypeMutationService(BaseMutationService):
    """Service for job requirement type mutations."""
    response_class = types.JobRequirementTypeDetailResponse
    model_field_name = "job_requirement_type"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirementType


@strawberry.type
class JobRequirementTypeMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_job_requirement_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateJobRequirementTypeInput,
    ) -> types.JobRequirementTypeDetailResponse:
        return await JobRequirementTypeMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_job_requirement_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobRequirementTypeInput,
    ) -> types.JobRequirementTypeDetailResponse:
        return await JobRequirementTypeMutationService.update(input, info)


# JobRequirement Mutations
class JobRequirementMutationService(BaseMutationService):
    """Service for job requirement mutations."""
    response_class = types.JobRequirementDetailResponse
    model_field_name = "job_requirement"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirement


@strawberry.type
class JobRequirementMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_job_requirement(
        self,
        info: strawberry.Info,
        input: inputs.CreateJobRequirementInput,
    ) -> types.JobRequirementDetailResponse:
        return await JobRequirementMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_job_requirement(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobRequirementInput,
    ) -> types.JobRequirementDetailResponse:
        return await JobRequirementMutationService.update(input, info)


# JobRequirementFile Mutations
class JobRequirementFileMutationService(BaseMutationService):
    """Service for job requirement file mutations."""
    response_class = types.JobRequirementFileDetailResponse
    model_field_name = "job_requirement_file"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirementFile


@strawberry.type
class JobRequirementFileMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_job_requirement_file(
        self,
        info: strawberry.Info,
        input: inputs.CreateJobRequirementFileInput,
    ) -> types.JobRequirementFileDetailResponse:
        return await JobRequirementFileMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_job_requirement_file(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobRequirementFileInput,
    ) -> types.JobRequirementFileDetailResponse:
        return await JobRequirementFileMutationService.update(input, info)


# AmbassadorJob Mutations
class AmbassadorJobMutationService(BaseMutationService):
    """Service for ambassador job mutations."""
    response_class = types.AmbassadorJobDetailResponse
    model_field_name = "ambassador_job"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AmbassadorJob


@strawberry.type
class AmbassadorJobMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_ambassador_job(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorJobInput,
    ) -> types.AmbassadorJobDetailResponse:
        return await AmbassadorJobMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_ambassador_job(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAmbassadorJobInput,
    ) -> types.AmbassadorJobDetailResponse:
        return await AmbassadorJobMutationService.update(input, info)


# Manage Ambassador Job Assignment Mutations
class ManageAmbassadorJobMutationService(SparkGraphQLMixin):
    """Service for managing ambassador job assignments."""

    @classmethod
    async def _find_status_by_name_pattern(
        cls, tenant_id: int, name_pattern: str
    ) -> models.Status | None:
        """Find a status by name pattern (case-insensitive)."""
        try:
            status = await sync_to_async(
                models.Status.objects.filter(
                    tenant_id=tenant_id, name__icontains=name_pattern
                ).first
            )()
            return status
        except Exception:
            return None

    @classmethod
    async def _get_status_for_action(
        cls,
        action: inputs.ManageAmbassadorJobAssignmentAction,
        tenant_id: int,
        status_id: strawberry.ID | None = None,
    ) -> models.Status:
        """Get the status for the given action."""
        if status_id:
            try:
                status_id = resolve_id_to_int(status_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid status ID.") from exc
            try:
                status = await sync_to_async(models.Status.objects.get)(
                    id=status_id, tenant_id=tenant_id
                )
                return status
            except models.Status.DoesNotExist:
                raise GraphQLError(f"Status with ID {status_id} not found.")

        # Try to find status by name pattern if status_id not provided
        status_name_map = {
            inputs.ManageAmbassadorJobAssignmentAction.ACCEPT: "accept",
            inputs.ManageAmbassadorJobAssignmentAction.REJECT: "reject",
            inputs.ManageAmbassadorJobAssignmentAction.BLACKLIST: "blacklist",
            inputs.ManageAmbassadorJobAssignmentAction.WHITELIST: "whitelist",
        }

        name_pattern = status_name_map.get(action, "accept")
        status = await cls._find_status_by_name_pattern(tenant_id, name_pattern)

        if not status:
            raise GraphQLError(
                f"Status for action '{action.value}' not found. "
                f"Please create a status with name containing '{name_pattern}' or provide statusId."
            )

        return status

    @classmethod
    async def manage_assignment(
        cls,
        input: inputs.ManageAmbassadorJobAssignmentInput,
        info: strawberry.Info,
    ) -> types.AmbassadorJobDetailResponse:
        """Manage ambassador job assignment (accept, reject, blacklist, whitelist)."""
        service = cls()
        user = await service.get_user(info)
        tenant = await service.get_user_tenant(
            info,
            tenant_id=input.tenant_id,
            user=user,
        )

        # Get the AmbassadorJob
        try:
            ambassador_job_id = resolve_id_to_int(input.ambassador_job_id)
        except (TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Invalid ambassador job ID.",
                input_obj=input,
            )
        try:
            ambassador_job = await sync_to_async(models.AmbassadorJob.objects.get)(
                id=ambassador_job_id, tenant_id=tenant.id
            )
        except models.AmbassadorJob.DoesNotExist:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Ambassador job not found.",
                input_obj=input,
            )

        # Get the status for the action
        try:
            status = await cls._get_status_for_action(
                input.action, tenant.id, input.status_id
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

        # Update the status
        ambassador_job.status = status
        ambassador_job.updated_by = user
        await sync_to_async(ambassador_job.save)()

        action_messages = {
            inputs.ManageAmbassadorJobAssignmentAction.ACCEPT: "Ambassador accepted for job.",
            inputs.ManageAmbassadorJobAssignmentAction.REJECT: "Ambassador rejected for job.",
            inputs.ManageAmbassadorJobAssignmentAction.BLACKLIST: "Ambassador blacklisted for future jobs.",
            inputs.ManageAmbassadorJobAssignmentAction.WHITELIST: "Ambassador whitelisted for future jobs.",
        }

        message = action_messages.get(
            input.action, f"Ambassador job assignment updated to {status.name}."
        )

        return build_mutation_response(
            types.AmbassadorJobDetailResponse,
            success=True,
            message=message,
            input_obj=input,
            ambassador_job=ambassador_job,
        )


@strawberry.type
class ManageAmbassadorJobMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def manage_ambassador_job_assignment(
        self,
        info: strawberry.Info,
        input: inputs.ManageAmbassadorJobAssignmentInput,
    ) -> types.AmbassadorJobDetailResponse:
        """Manage ambassador job assignment (accept, reject, blacklist, whitelist)."""
        return await ManageAmbassadorJobMutationService.manage_assignment(input, info)


# CompanyToAmbassadorReview Mutations
class CompanyToAmbassadorReviewMutationService(BaseMutationService):
    """Service for company to ambassador review mutations."""
    response_class = types.CompanyToAmbassadorReviewDetailResponse
    model_field_name = "company_to_ambassador_review"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.CompanyToAmbassadorReview


@strawberry.type
class CompanyToAmbassadorReviewMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_company_to_ambassador_review(
        self,
        info: strawberry.Info,
        input: inputs.CreateCompanyToAmbassadorReviewInput,
    ) -> types.CompanyToAmbassadorReviewDetailResponse:
        return await CompanyToAmbassadorReviewMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_company_to_ambassador_review(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCompanyToAmbassadorReviewInput,
    ) -> types.CompanyToAmbassadorReviewDetailResponse:
        return await CompanyToAmbassadorReviewMutationService.update(input, info)


# AmbassadorToAmbassadorReview Mutations
class AmbassadorToAmbassadorReviewMutationService(BaseMutationService):
    """Service for ambassador to ambassador review mutations."""
    response_class = types.AmbassadorToAmbassadorReviewDetailResponse
    model_field_name = "ambassador_to_ambassador_review"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AmbassadorToAmbassadorReview


@strawberry.type
class AmbassadorToAmbassadorReviewMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_ambassador_to_ambassador_review(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorToAmbassadorReviewInput,
    ) -> types.AmbassadorToAmbassadorReviewDetailResponse:
        return await AmbassadorToAmbassadorReviewMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_ambassador_to_ambassador_review(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAmbassadorToAmbassadorReviewInput,
    ) -> types.AmbassadorToAmbassadorReviewDetailResponse:
        return await AmbassadorToAmbassadorReviewMutationService.update(input, info)


# QuestionType Mutations
class QuestionTypeMutationService(BaseMutationService):
    """Service for question type mutations."""
    response_class = types.QuestionTypeDetailResponse
    model_field_name = "question_type"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.QuestionType


@strawberry.type
class QuestionTypeMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_question_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateQuestionTypeInput,
    ) -> types.QuestionTypeDetailResponse:
        return await QuestionTypeMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_question_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateQuestionTypeInput,
    ) -> types.QuestionTypeDetailResponse:
        return await QuestionTypeMutationService.update(input, info)


# JobRequirementQuestion Mutations
class JobRequirementQuestionMutationService(BaseMutationService):
    """Service for job requirement question mutations."""
    response_class = types.JobRequirementQuestionDetailResponse
    model_field_name = "job_requirement_question"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirementQuestion


@strawberry.type
class JobRequirementQuestionMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_job_requirement_question(
        self,
        info: strawberry.Info,
        input: inputs.CreateJobRequirementQuestionInput,
    ) -> types.JobRequirementQuestionDetailResponse:
        return await JobRequirementQuestionMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_job_requirement_question(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobRequirementQuestionInput,
    ) -> types.JobRequirementQuestionDetailResponse:
        return await JobRequirementQuestionMutationService.update(input, info)


# QuestionOption Mutations
class QuestionOptionMutationService(BaseMutationService):
    """Service for question option mutations."""
    response_class = types.QuestionOptionDetailResponse
    model_field_name = "question_option"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.QuestionOption


@strawberry.type
class QuestionOptionMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_question_option(
        self,
        info: strawberry.Info,
        input: inputs.CreateQuestionOptionInput,
    ) -> types.QuestionOptionDetailResponse:
        return await QuestionOptionMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_question_option(
        self,
        info: strawberry.Info,
        input: inputs.UpdateQuestionOptionInput,
    ) -> types.QuestionOptionDetailResponse:
        return await QuestionOptionMutationService.update(input, info)


# JobRequirementAnswer Mutations
class JobRequirementAnswerMutationService(BaseMutationService):
    """Service for job requirement answer mutations."""
    response_class = types.JobRequirementAnswerDetailResponse
    model_field_name = "job_requirement_answer"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirementAnswer


@strawberry.type
class JobRequirementAnswerMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_job_requirement_answer(
        self,
        info: strawberry.Info,
        input: inputs.CreateJobRequirementAnswerInput,
    ) -> types.JobRequirementAnswerDetailResponse:
        return await JobRequirementAnswerMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_job_requirement_answer(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobRequirementAnswerInput,
    ) -> types.JobRequirementAnswerDetailResponse:
        return await JobRequirementAnswerMutationService.update(input, info)


# Approve Ambassador Job Mutation
class ApproveAmbassadorJobMutationService(SparkGraphQLMixin):
    """Service for approving ambassador job."""

    @classmethod
    async def invite_ambassadors_to_job(
        cls,
        input: inputs.InviteAmbassadorsToJobInput,
        info: strawberry.Info,
    ) -> types.AmbassadorJobDetailResponse:
        service = cls()
        user = await service.get_user(info)
        tenant = await service.get_user_tenant(
            info,
            tenant_id=input.tenant_id,
            user=user,
        )

        @sync_to_async
        def invite_ambassadors_to_job():
            invite_status = models.Status.objects.get_invited(
                tenant_id=tenant.id,
                user=user
            )

            job = models.Job.objects.filter(
                id=input.job_id, tenant_id=tenant.id, rate__isnull=False).first()
            if not job:
                raise GraphQLError("Job not found or has no rate.")
            ambassador_ids = getattr(input, "ambassador_ids", None)
            if not ambassador_ids:
                return []

            # Resolve ambassador IDs from Relay IDs to integers
            from utils.graphql.mixins import resolve_id_to_int
            resolved_ids = []
            for ambassador_id in ambassador_ids:
                try:
                    resolved_id = resolve_id_to_int(ambassador_id)
                    resolved_ids.append(resolved_id)
                except (TypeError, ValueError, GraphQLError) as exc:
                    raise GraphQLError(
                        f"Invalid ambassador ID: {ambassador_id}") from exc

            # Filter ambassadors by tenant via TenantedUser relationship
            ambassadors = models.Ambassador.objects.filter(
                id__in=resolved_ids
            ).distinct()
            if not ambassadors:
                return []
            ambassador_jobs = []
            for ambassador in ambassadors:
                ambassador_job = models.AmbassadorJob.objects.create(
                    ambassador=ambassador,
                    job=job,
                    tenant=tenant,
                    status=invite_status,
                    rate=job.rate,
                    appear_as_rfp=True,
                    created_by=user,
                    updated_by=user,
                )
                ambassador_jobs.append(ambassador_job)
            return ambassador_jobs

        ambassador_jobs = await invite_ambassadors_to_job()
        return build_mutation_response(
            types.InviteAmbassadorsToJobResponse,
            success=True,
            message="Ambassadors invited to job.",
            input_obj=input,
            ambassador_jobs=ambassador_jobs,
        )

    @classmethod
    async def approve(
        cls,
        input: inputs.ApproveAmbassadorJobInput,
        info: strawberry.Info,
    ) -> types.AmbassadorJobDetailResponse:
        service = cls()
        user = await service.get_user(info)
        tenant = await service.get_user_tenant(
            info,
            tenant_id=input.tenant_id,
            user=user,
        )

        try:
            ambassador_job_id = resolve_id_to_int(input.ambassador_job_id)
        except (TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Invalid ambassador job ID.",
                input_obj=input,
            )
        try:
            ambassador_job = await sync_to_async(models.AmbassadorJob.objects.get)(
                id=ambassador_job_id, tenant_id=tenant.id
            )
        except models.AmbassadorJob.DoesNotExist:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Ambassador job not found.",
                input_obj=input,
            )

        # Find status with slug 'approved'
        try:
            status = await sync_to_async(models.Status.objects.get)(
                slug=inputs.AmbassadorJobStatusEnum.APPROVED.value,
                tenant_id=tenant.id
            )
        except models.Status.DoesNotExist:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message=f"Status with slug '{inputs.AmbassadorJobStatusEnum.APPROVED.value}' not found.",
                input_obj=input,
            )

        ambassador_job.status = status
        ambassador_job.updated_by = user
        await sync_to_async(ambassador_job.save)()

        return build_mutation_response(
            types.AmbassadorJobDetailResponse,
            success=True,
            message="Ambassador job approved.",
            input_obj=input,
            ambassador_job=ambassador_job,
        )


@strawberry.type
class ApproveAmbassadorJobMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_ambassador_job(
        self,
        info: strawberry.Info,
        input: inputs.ApproveAmbassadorJobInput,
    ) -> types.AmbassadorJobDetailResponse:
        return await ApproveAmbassadorJobMutationService.approve(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def invite_ambassadors_to_job(
        self,
        info: strawberry.Info,
        input: inputs.InviteAmbassadorsToJobInput,
    ) -> types.InviteAmbassadorsToJobResponse:
        return await ApproveAmbassadorJobMutationService.invite_ambassadors_to_job(input, info)

# Decline Ambassador Job Mutation


class DeclineAmbassadorJobMutationService(SparkGraphQLMixin):
    """Service for declining ambassador job."""

    @classmethod
    async def decline(
        cls,
        input: inputs.DeclineAmbassadorJobInput,
        info: strawberry.Info,
    ) -> types.AmbassadorJobDetailResponse:
        service = cls()
        user = await service.get_user(info)
        tenant = await service.get_user_tenant(
            info,
            tenant_id=input.tenant_id,
            user=user,
        )

        try:
            ambassador_job_id = resolve_id_to_int(input.ambassador_job_id)
        except (TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Invalid ambassador job ID.",
                input_obj=input,
            )
        try:
            ambassador_job = await sync_to_async(models.AmbassadorJob.objects.get)(
                id=ambassador_job_id, tenant_id=tenant.id
            )
        except models.AmbassadorJob.DoesNotExist:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Ambassador job not found.",
                input_obj=input,
            )

        # Find status with slug 'declined'
        try:
            status = await sync_to_async(models.Status.objects.get)(
                slug=inputs.AmbassadorJobStatusEnum.DECLINED.value,
                tenant_id=tenant.id
            )
        except models.Status.DoesNotExist:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message=f"Status with slug '{inputs.AmbassadorJobStatusEnum.DECLINED.value}' not found.",
                input_obj=input,
            )

        ambassador_job.status = status
        ambassador_job.updated_by = user
        await sync_to_async(ambassador_job.save)()

        return build_mutation_response(
            types.AmbassadorJobDetailResponse,
            success=True,
            message="Ambassador job declined.",
            input_obj=input,
            ambassador_job=ambassador_job,
        )


@strawberry.type
class DeclineAmbassadorJobMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def decline_ambassador_job(
        self,
        info: strawberry.Info,
        input: inputs.DeclineAmbassadorJobInput,
    ) -> types.AmbassadorJobDetailResponse:
        return await DeclineAmbassadorJobMutationService.decline(input, info)
