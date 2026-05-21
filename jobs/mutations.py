import strawberry
from django.db.models import Model
from django.db.models.deletion import ProtectedError
from strawberry import relay
from graphql import GraphQLError
from asgiref.sync import sync_to_async
import logging

from jobs import models, inputs, types
from jobs.envelopes import (
    AmbassadorAppliedJobUpdatedMailer,
    AmbassadorAssignedToJobMailer,
    AmbassadorApprovedForJobMailer,
    AmbassadorInvitedJobUpdatedMailer,
    AmbassadorJobApprovedNotificationMailer,
    AmbassadorJobUpdatedMailer,
    AmbassadorUnassignedFromJobMailer,
)
from jobs.notification_rules import should_send_ambassador_event_email
from ambassadors.models import AmbassadorEvent
from tenants.models import Role, TenantedUser
from utils.onesignal import OneSignalError, one_signal_client
from utils.graphql.mixins import (
    BaseMutationService,
    SparkGraphQLMixin,
    resolve_id_to_int,
)
from utils.graphql.permissions import StrictIsAuthenticated, IsClientOrSparkAdmin
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import build_mutation_response
from utils.calendar import GoogleCalendarService

ensure_relay_mutation()

logger = logging.getLogger(__name__)


async def _create_calendar_event_for_approved_job(
    ambassador_job: models.AmbassadorJob,
) -> None:
    job = ambassador_job.job
    event = job.event
    tenant = ambassador_job.tenant

    if not job.start_date or not job.end_date:
        logger.warning(
            f"Cannot create calendar event for job {job.id}: missing start or end date."
        )
        return

    ambassador = getattr(ambassador_job, "ambassador", None)
    user = getattr(ambassador, "user", None)
    ambassador_name = user.get_full_name() if user else "Unknown Ambassador"

    summary = f"[{tenant.name}] {job.name} - {ambassador_name}"
    description = (
        f"Campaign: {event.name or '-'}\n"
        f"Job Title: {job.job_title.name if job.job_title else '-'}\n"
        f"Ambassador: {ambassador_name}\n"
        f"Location: {job.address or event.address or '-'}\n"
    )

    # Try to grab timezone from event, else default to UTC
    event_timezone = getattr(event, "timezone", None)
    timezone_str = getattr(event_timezone, "name", "UTC") if event_timezone else "UTC"

    calendar_service = GoogleCalendarService()

    attendees = []
    if user and user.email:
        attendees.append(user.email)

    # Execute synchronous API call in a thread pool
    await sync_to_async(calendar_service.create_event)(
        summary=summary,
        description=description,
        location=job.address or event.address or "",
        start_time=job.start_date,
        end_time=job.end_date,
        timezone=timezone_str,
        attendees=attendees if attendees else None,
    )


async def _notify_approval_to_rmm_or_clients(
    ambassador_job: models.AmbassadorJob,
) -> None:
    event = ambassador_job.job.event
    rmm_user = getattr(event, "rmm_asigned", None)
    fallback_reply_to = "events@igniteproductions.co"
    reply_to_email = (
        getattr(rmm_user, "email", None) or ""
    ).strip() or fallback_reply_to

    recipients: list[tuple[str, str]] = []
    if rmm_user and rmm_user.email:
        recipients.append(
            (
                rmm_user.email.strip(),
                (rmm_user.first_name or "").strip(),
            )
        )
    else:
        rows = await sync_to_async(list)(
            TenantedUser.objects.filter(
                tenant_id=ambassador_job.tenant_id,
                is_active=True,
                user__role__slug=Role.CLIENT_SLUG,
            ).values("user__email", "user__first_name")
        )
        for row in rows:
            email = (row.get("user__email") or "").strip()
            if not email:
                continue
            recipients.append((email, (row.get("user__first_name") or "").strip()))

    if not recipients:
        return

    for email, first_name in recipients:
        mailer = AmbassadorJobApprovedNotificationMailer(
            ambassador_job=ambassador_job,
            to_emails=[email],
            recipient_first_name=first_name or None,
            reply_to_email=reply_to_email,
        )
        await sync_to_async(mailer.send)()


async def _notify_approved_ambassador_by_push(
    ambassador_job: models.AmbassadorJob,
) -> None:
    ambassador = getattr(ambassador_job, "ambassador", None)
    user = getattr(ambassador, "user", None)
    if not user:
        return

    job = ambassador_job.job
    title = "Job application accepted"
    message = f"You were accepted for {job.name}."
    deep_link = f"spark://my-gigs/{ambassador_job.id}"

    try:
        await one_signal_client.send_push(
            external_ids=[str(user.uuid)],
            title=title,
            message=message,
            url=deep_link,
            data={
                "type": "job_application_accepted",
                "job_id": str(job.id),
                "ambassador_job_id": str(ambassador_job.id),
                "deep_link": deep_link,
            },
        )
    except OneSignalError as exc:
        logger.warning(
            "Failed to send OneSignal approval push for ambassador_job=%s: %s",
            ambassador_job.id,
            exc,
        )


async def _notify_approved_ambassador_by_email(
    ambassador_job: models.AmbassadorJob,
) -> None:
    if not should_send_ambassador_event_email(ambassador_job):
        return

    ambassador = getattr(ambassador_job, "ambassador", None)
    user = getattr(ambassador, "user", None)
    email = (getattr(user, "email", None) or "").strip()
    if not email:
        return

    mailer = AmbassadorApprovedForJobMailer(
        ambassador_job=ambassador_job,
        to_emails=[email],
        recipient_first_name=(getattr(user, "first_name", None) or "").strip() or None,
    )
    await sync_to_async(mailer.send)()


async def _notify_assigned_ambassador_by_email(
    ambassador_job: models.AmbassadorJob,
) -> None:
    if not should_send_ambassador_event_email(ambassador_job):
        return

    ambassador = getattr(ambassador_job, "ambassador", None)
    user = getattr(ambassador, "user", None)
    email = (getattr(user, "email", None) or "").strip()
    if not email:
        return

    mailer = AmbassadorAssignedToJobMailer(
        ambassador_job=ambassador_job,
        to_emails=[email],
        recipient_first_name=(getattr(user, "first_name", None) or "").strip() or None,
    )
    await sync_to_async(mailer.send)()


async def _notify_unassigned_ambassador_by_email(
    ambassador_job: models.AmbassadorJob,
) -> None:
    if not should_send_ambassador_event_email(ambassador_job):
        return

    ambassador = getattr(ambassador_job, "ambassador", None)
    user = getattr(ambassador, "user", None)
    email = (getattr(user, "email", None) or "").strip()
    if not email:
        return

    mailer = AmbassadorUnassignedFromJobMailer(
        ambassador_job=ambassador_job,
        to_emails=[email],
        recipient_first_name=(getattr(user, "first_name", None) or "").strip() or None,
    )
    await sync_to_async(mailer.send)()


async def _notify_updated_ambassador_by_email(
    ambassador_job: models.AmbassadorJob,
) -> None:
    if not should_send_ambassador_event_email(ambassador_job):
        return

    ambassador = getattr(ambassador_job, "ambassador", None)
    user = getattr(ambassador, "user", None)
    email = (getattr(user, "email", None) or "").strip()
    if not email:
        return

    status_slug = (
        (getattr(getattr(ambassador_job, "status", None), "slug", None) or "")
        .strip()
        .lower()
    )
    if status_slug == "pending":
        mailer_class = AmbassadorAppliedJobUpdatedMailer
    elif status_slug == "invited":
        mailer_class = AmbassadorInvitedJobUpdatedMailer
    else:
        mailer_class = AmbassadorJobUpdatedMailer

    mailer = mailer_class(
        ambassador_job=ambassador_job,
        to_emails=[email],
        recipient_first_name=(getattr(user, "first_name", None) or "").strip() or None,
    )
    await sync_to_async(mailer.send)()


async def _notify_updated_ambassadors_for_job(job_id: int) -> None:
    ambassador_jobs = await sync_to_async(list)(
        models.AmbassadorJob.objects.filter(job_id=job_id)
        .select_related(
            "ambassador",
            "ambassador__user",
            "job",
            "job__event",
            "job__event__timezone",
            "job__event__retailer",
            "job__event__retailer__location",
            "job__event__retailer__location__state",
            "tenant",
            "status",
            "rate",
        )
        .distinct()
    )

    for ambassador_job in ambassador_jobs:
        await _notify_updated_ambassador_by_email(ambassador_job)


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

    @classmethod
    async def update(
        cls,
        input: inputs.UpdateJobInput,
        info: strawberry.Info,
        *,
        response_class: type | None = None,
        model_field_name: str | None = None,
        update_message: str | None = None,
    ) -> types.JobDetailResponse:
        job_id = resolve_id_to_int(input.id)
        original_job = await sync_to_async(
            models.Job.objects.select_related("event").get
        )(id=job_id)

        relevant_fields_before = {
            "address": original_job.address,
            "rate_id": original_job.rate_id,
            "extension_rate": original_job.extension_rate,
            "start_date": original_job.start_date,
            "end_date": original_job.end_date,
        }

        response = await super().update(
            input,
            info,
            response_class=response_class,
            model_field_name=model_field_name,
            update_message=update_message,
        )

        if not getattr(response, "success", False):
            return response

        updated_job = getattr(response, "job", None)
        if updated_job is None:
            return response

        updated_job = await sync_to_async(models.Job.objects.get)(id=updated_job.id)
        relevant_fields_after = {
            "address": updated_job.address,
            "rate_id": updated_job.rate_id,
            "extension_rate": updated_job.extension_rate,
            "start_date": updated_job.start_date,
            "end_date": updated_job.end_date,
        }

        if relevant_fields_before != relevant_fields_after:
            await _notify_updated_ambassadors_for_job(updated_job.id)

        return response


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

    @classmethod
    async def create(
        cls,
        input: inputs.CreateAmbassadorJobInput,
        info: strawberry.Info,
        *,
        response_class: type | None = None,
        model_field_name: str | None = None,
        create_message: str | None = None,
    ) -> types.AmbassadorJobDetailResponse:
        try:
            job_id = resolve_id_to_int(input.job_id)
        except (TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Please select a valid job.",
                input_obj=input,
            )
        try:
            ambassador_id = resolve_id_to_int(input.ambassador_id)
        except (TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Please select a valid ambassador.",
                input_obj=input,
            )

        try:
            job = await sync_to_async(models.Job.objects.only("id", "tenant_id").get)(
                id=job_id
            )
        except models.Job.DoesNotExist:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Job not found.",
                input_obj=input,
            )

        already_assigned = await sync_to_async(
            models.AmbassadorJob.objects.filter(
                job_id=job_id,
                ambassador_id=ambassador_id,
            ).exists
        )()
        if already_assigned:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="This ambassador is already assigned to this job. You can update the existing assignment instead.",
                input_obj=input,
            )

        try:
            approved_status = await sync_to_async(models.Status.objects.get)(
                slug="approved",
                tenant_id=job.tenant_id,
            )
        except models.Status.DoesNotExist:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Approved status not found for this job tenant.",
                input_obj=input,
            )

        input.status_id = approved_status.id
        if getattr(input, "time_blocks_15m", None) is None:
            input.time_blocks_15m = 0

        response = await super().create(
            input,
            info,
            response_class=response_class,
            model_field_name=model_field_name,
            create_message=create_message,
        )

        if not getattr(response, "success", False):
            return response

        ambassador_job = getattr(response, "ambassador_job", None)
        if ambassador_job is None:
            return response

        ambassador_job = await sync_to_async(
            models.AmbassadorJob.objects.select_related(
                "ambassador",
                "ambassador__user",
                "job",
                "job__event",
                "job__event__timezone",
                "job__event__retailer",
                "job__event__retailer__location",
                "job__event__retailer__location__state",
                "tenant",
                "status",
                "rate",
            ).get
        )(id=ambassador_job.id)

        job = await models.Job.objects.only("id", "event_id").aget(
            id=ambassador_job.job_id
        )
        if not await AmbassadorEvent.objects.filter(
            ambassador_id=ambassador_job.ambassador_id,
            event_id=job.event_id,
        ).aexists():
            await AmbassadorEvent.objects.acreate(
                ambassador_id=ambassador_job.ambassador_id,
                event_id=job.event_id,
                tenant_id=ambassador_job.tenant_id,
                is_approved=False,
                created_by_id=ambassador_job.created_by_id,
                updated_by_id=ambassador_job.updated_by_id,
            )

        await _notify_assigned_ambassador_by_email(ambassador_job)

        return response


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

    @classmethod
    async def unassign(
        cls,
        input: inputs.UnassignAmbassadorJobInput,
        info: strawberry.Info,
    ) -> types.DeleteAmbassadorJobResponse:
        service = cls()
        await service.get_user(info)

        try:
            ambassador_job_id = resolve_id_to_int(input.ambassador_job_id)
        except (TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.DeleteAmbassadorJobResponse,
                success=False,
                message="Invalid ambassador job ID.",
                input_obj=input,
            )

        try:
            ambassador_job = await sync_to_async(models.AmbassadorJob.objects.get)(
                id=ambassador_job_id,
            )
        except models.AmbassadorJob.DoesNotExist:
            return build_mutation_response(
                types.DeleteAmbassadorJobResponse,
                success=False,
                message="Ambassador job not found.",
                input_obj=input,
            )

        try:
            ambassador_job = await sync_to_async(
                models.AmbassadorJob.objects.select_related(
                    "ambassador",
                    "ambassador__user",
                    "job",
                    "job__event",
                    "job__event__timezone",
                    "job__event__retailer",
                    "job__event__retailer__location",
                    "job__event__retailer__location__state",
                    "tenant",
                    "status",
                    "rate",
                ).get
            )(id=ambassador_job.id)
            await _notify_unassigned_ambassador_by_email(ambassador_job)
            await sync_to_async(ambassador_job.delete)()
        except ProtectedError:
            return build_mutation_response(
                types.DeleteAmbassadorJobResponse,
                success=False,
                message="Ambassador job cannot be unassigned because it is referenced by other records.",
                input_obj=input,
            )

        return build_mutation_response(
            types.DeleteAmbassadorJobResponse,
            success=True,
            message="Ambassador unassigned from job.",
            input_obj=input,
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

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def unassign_ambassador_job(
        self,
        info: strawberry.Info,
        input: inputs.UnassignAmbassadorJobInput,
    ) -> types.DeleteAmbassadorJobResponse:
        return await ManageAmbassadorJobMutationService.unassign(input, info)


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
            try:
                job_id = resolve_id_to_int(input.job_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Please select a valid job.") from exc

            job = models.Job.objects.filter(
                id=job_id, tenant_id=tenant.id, rate__isnull=False
            ).first()
            if not job:
                raise GraphQLError("Job not found or has no rate.")
            ambassador_ids = getattr(input, "ambassador_ids", None)
            if not ambassador_ids:
                return []

            resolved_ids = []
            for ambassador_id in ambassador_ids:
                try:
                    resolved_id = resolve_id_to_int(ambassador_id)
                    resolved_ids.append(resolved_id)
                except (TypeError, ValueError, GraphQLError) as exc:
                    raise GraphQLError("One of the selected ambassadors is not valid.") from exc

            # Filter ambassadors by tenant via TenantedUser relationship
            ambassadors = models.Ambassador.objects.filter(
                id__in=resolved_ids
            ).distinct()
            if not ambassadors:
                return []

            existing_ambassador_ids = set(
                models.AmbassadorJob.objects.filter(
                    job=job,
                    ambassador_id__in=[ambassador.id for ambassador in ambassadors],
                ).values_list("ambassador_id", flat=True)
            )
            if existing_ambassador_ids:
                raise GraphQLError(
                    "Some selected ambassadors already have an invitation for this job."
                )

            ambassador_jobs = []
            for ambassador in ambassadors:
                ambassador_job = models.AmbassadorJob.objects.create_and_invite(
                    job=job,
                    ambassador=ambassador,
                    action_by=user,
                )
                ambassador_jobs.append(ambassador_job)
            return ambassador_jobs

        try:
            ambassador_jobs = await invite_ambassadors_to_job()
        except GraphQLError as exc:
            return build_mutation_response(
                types.InviteAmbassadorsToJobResponse,
                success=False,
                message=str(exc),
                input_obj=input,
                ambassador_jobs=[],
            )

        if ambassador_jobs:
            ambassador_jobs = await sync_to_async(list)(
                models.AmbassadorJob.objects.filter(
                    id__in=[ambassador_job.id for ambassador_job in ambassador_jobs]
                ).select_related(
                    "ambassador",
                    "ambassador__user",
                    "job",
                    "job__event",
                    "job__event__timezone",
                    "job__event__retailer",
                    "job__event__retailer__location",
                    "job__event__retailer__location__state",
                    "status",
                    "rate",
                    "tenant",
                )
            )
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
                slug=inputs.AmbassadorJobStatusEnum.APPROVED.value, tenant_id=tenant.id
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
        ambassador_job = await sync_to_async(
            models.AmbassadorJob.objects.select_related(
                "job",
                "job__job_title",
                "job__event",
                "job__event__timezone",
                "job__event__retailer",
                "job__event__retailer__location",
                "job__event__retailer__location__state",
                "job__event__rmm_asigned",
                "ambassador",
                "ambassador__user",
                "tenant",
                "status",
            ).get
        )(id=ambassador_job.id)
        await _notify_approval_to_rmm_or_clients(ambassador_job)
        await _notify_approved_ambassador_by_email(ambassador_job)
        await _notify_approved_ambassador_by_push(ambassador_job)
        await _create_calendar_event_for_approved_job(ambassador_job)

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
        return await ApproveAmbassadorJobMutationService.invite_ambassadors_to_job(
            input, info
        )


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
                slug=inputs.AmbassadorJobStatusEnum.DECLINED.value, tenant_id=tenant.id
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


class AcceptAmbassadorJobInvitationMutationService(SparkGraphQLMixin):
    """Service for ambassadors accepting invited jobs."""

    @classmethod
    async def accept(
        cls,
        input: inputs.AcceptAmbassadorJobInvitationInput,
        info: strawberry.Info,
    ) -> types.AmbassadorJobDetailResponse:
        service = cls()
        user = await service.get_user(info)

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
            ambassador_job = await sync_to_async(
                models.AmbassadorJob.objects.select_related(
                    "ambassador",
                    "ambassador__user",
                    "job",
                    "status",
                ).get
            )(
                id=ambassador_job_id,
            )
        except models.AmbassadorJob.DoesNotExist:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Ambassador job not found.",
                input_obj=input,
            )

        if ambassador_job.ambassador.user_id != user.id:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="You can only accept your own job invitations.",
                input_obj=input,
            )

        current_status_slug = (
            (getattr(ambassador_job.status, "slug", None) or "").strip().lower()
        )
        if current_status_slug != inputs.AmbassadorJobStatusEnum.INVITED.value:
            return build_mutation_response(
                types.AmbassadorJobDetailResponse,
                success=False,
                message="Only invited ambassador jobs can be accepted.",
                input_obj=input,
            )

        accepted_status = await sync_to_async(models.Status.objects.get_accepted)(
            tenant_id=ambassador_job.tenant_id,
            user=user,
        )

        ambassador_job.status = accepted_status
        ambassador_job.updated_by = user
        await sync_to_async(ambassador_job.save)()

        if not await AmbassadorEvent.objects.filter(
            ambassador_id=ambassador_job.ambassador_id,
            event_id=ambassador_job.job.event_id,
        ).aexists():
            await AmbassadorEvent.objects.acreate(
                ambassador_id=ambassador_job.ambassador_id,
                event_id=ambassador_job.job.event_id,
                tenant_id=ambassador_job.tenant_id,
                is_approved=False,
                created_by_id=ambassador_job.created_by_id,
                updated_by_id=user.id,
            )

        return build_mutation_response(
            types.AmbassadorJobDetailResponse,
            success=True,
            message="Ambassador job invitation accepted.",
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


@strawberry.type
class AcceptAmbassadorJobInvitationMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def accept_ambassador_job_invitation(
        self,
        info: strawberry.Info,
        input: inputs.AcceptAmbassadorJobInvitationInput,
    ) -> types.AmbassadorJobDetailResponse:
        return await AcceptAmbassadorJobInvitationMutationService.accept(input, info)


# ============================================================
# Job lifecycle mutations (Post / OpenToAll / Apply / Assign /
# Favorites)
# ============================================================
from django.utils import timezone as _django_tz  # noqa: E402
from ambassadors.models import Ambassador as _Ambassador  # noqa: E402
from utils.graphql.mixins import resolve_id_to_int as _resolve_id  # noqa: E402
from utils.graphql.permissions import StrictIsAuthenticated as _StrictIsAuth  # noqa: E402


def _build_lifecycle_response(success, message, *, job=None, input_obj=None):
    cmid = getattr(input_obj, "client_mutation_id", None) if input_obj else None
    return types.JobLifecycleResponse(
        success=success,
        message=message,
        client_mutation_id=cmid,
        job_uuid=str(job.uuid) if job else None,
        lifecycle_status=getattr(job, "lifecycle_status", None) if job else None,
    )


@strawberry.type
class JobLifecycleMutations:
    """Admin-side lifecycle transitions on a Job.

    pending → posted   via post_job
    posted (favorites_only=true) → favorites_only=false via open_job_to_all
    posted → filled    via assign_ambassador_to_job
    """

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def post_job(
        self,
        info: strawberry.Info,
        input: inputs.PostJobInput,
    ) -> types.JobLifecycleResponse:
        try:
            job_pk = _resolve_id(input.id)
        except (TypeError, ValueError, GraphQLError):
            return _build_lifecycle_response(False, "Invalid job id.", input_obj=input)

        def _post():
            try:
                job = models.Job.objects.get(pk=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if job.lifecycle_status != models.Job.STATUS_PENDING:
                return None, (
                    f"Job is already {job.lifecycle_status}; can only post pending jobs."
                )
            job.total_hours = input.total_hours
            job.hourly_rate = input.hourly_rate
            if input.uniform_notes is not None:
                job.uniform_notes = input.uniform_notes
            if input.description is not None:
                job.description = input.description
            if input.max_applications is not None:
                job.max_applications = input.max_applications
            if input.open_to_all:
                job.favorites_only = False
            job.lifecycle_status = models.Job.STATUS_POSTED
            job.posted_at = _django_tz.now()
            job.public = True
            job.save(update_fields=[
                "total_hours", "hourly_rate", "uniform_notes",
                "description", "max_applications", "favorites_only",
                "lifecycle_status", "posted_at", "public", "updated_at",
            ])
            return job, "Job posted."

        job, msg = await sync_to_async(_post)()
        return _build_lifecycle_response(job is not None, msg, job=job, input_obj=input)

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def open_job_to_all(
        self,
        info: strawberry.Info,
        input: inputs.OpenJobToAllInput,
    ) -> types.JobLifecycleResponse:
        try:
            job_pk = _resolve_id(input.id)
        except (TypeError, ValueError, GraphQLError):
            return _build_lifecycle_response(False, "Invalid job id.", input_obj=input)

        def _open():
            try:
                job = models.Job.objects.get(pk=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if job.lifecycle_status != models.Job.STATUS_POSTED:
                return None, "Job must be posted before opening to all."
            if not job.favorites_only:
                return job, "Job is already open to all BAs."
            job.favorites_only = False
            job.save(update_fields=["favorites_only", "updated_at"])
            return job, "Job opened to all BAs."

        job, msg = await sync_to_async(_open)()
        return _build_lifecycle_response(job is not None, msg, job=job, input_obj=input)

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def assign_ambassador_to_job(
        self,
        info: strawberry.Info,
        input: inputs.AssignAmbassadorToJobInput,
    ) -> types.JobLifecycleResponse:
        try:
            job_pk = _resolve_id(input.job_id)
            ba_pk = _resolve_id(input.ambassador_id)
        except (TypeError, ValueError, GraphQLError):
            return _build_lifecycle_response(False, "Invalid ID.", input_obj=input)

        actor = info.context.request.user

        def _assign():
            try:
                job = models.Job.objects.get(pk=job_pk)
                amb = _Ambassador.objects.get(pk=ba_pk)
            except (models.Job.DoesNotExist, _Ambassador.DoesNotExist):
                return None, "Job or ambassador not found."
            if job.lifecycle_status not in (
                models.Job.STATUS_PENDING, models.Job.STATUS_POSTED
            ):
                return None, f"Job is {job.lifecycle_status}; can't reassign."

            now = _django_tz.now()
            app, created = models.JobApplication.objects.get_or_create(
                job=job, ambassador=amb,
                defaults={
                    "tenant_id": job.tenant_id,
                    "status": models.JobApplication.STATUS_ACCEPTED,
                    "decided_at": now,
                    "decided_by": actor if getattr(actor, "id", None) else None,
                    "note": "Manually assigned by admin.",
                },
            )
            if not created:
                app.status = models.JobApplication.STATUS_ACCEPTED
                app.decided_at = now
                app.decided_by = actor if getattr(actor, "id", None) else None
                app.save(update_fields=["status", "decided_at", "decided_by", "updated_at"])

            # Decline all other Applied rows for this job.
            models.JobApplication.objects.filter(
                job=job, status=models.JobApplication.STATUS_APPLIED,
            ).exclude(pk=app.pk).update(
                status=models.JobApplication.STATUS_DECLINED,
                decided_at=now, decided_by=actor if getattr(actor, "id", None) else None,
            )

            job.lifecycle_status = models.Job.STATUS_FILLED
            job.closed = True
            job.save(update_fields=["lifecycle_status", "closed", "updated_at"])
            return job, f"{amb.user.get_full_name() if amb.user else 'BA'} assigned to the job."

        job, msg = await sync_to_async(_assign)()
        return _build_lifecycle_response(job is not None, msg, job=job, input_obj=input)


@strawberry.type
class JobApplicationMutations:
    """BA-side mutations: apply / withdraw."""

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def apply_to_job(
        self,
        info: strawberry.Info,
        input: inputs.ApplyToJobInput,
    ) -> types.JobApplicationResponse:
        try:
            job_pk = _resolve_id(input.job_id)
        except (TypeError, ValueError, GraphQLError):
            return types.JobApplicationResponse(
                success=False, message="Invalid job id.",
                client_mutation_id=input.client_mutation_id,
            )

        actor = info.context.request.user

        def _apply():
            try:
                job = models.Job.objects.get(pk=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if job.lifecycle_status != models.Job.STATUS_POSTED:
                return None, "Job isn't currently accepting applications."
            try:
                amb = _Ambassador.objects.get(user=actor)
            except _Ambassador.DoesNotExist:
                return None, "Only ambassadors can apply to jobs."

            # If favorites-only, the BA must be on the tenant's favorites list.
            if job.favorites_only:
                is_fav = models.TenantFavoriteAmbassador.objects.filter(
                    tenant_id=job.tenant_id, ambassador=amb,
                ).exists()
                if not is_fav:
                    return None, (
                        "This job is currently open to favorite BAs only."
                    )

            # Cap check
            if job.max_applications:
                live_count = models.JobApplication.objects.filter(
                    job=job, status__in=[
                        models.JobApplication.STATUS_APPLIED,
                        models.JobApplication.STATUS_ACCEPTED,
                    ],
                ).count()
                if live_count >= job.max_applications:
                    return None, "Application cap reached for this job."

            app, created = models.JobApplication.objects.get_or_create(
                job=job, ambassador=amb,
                defaults={
                    "tenant_id": job.tenant_id,
                    "status": models.JobApplication.STATUS_APPLIED,
                    "note": (input.note or "")[:1000],
                },
            )
            if not created:
                # Re-apply path — flip withdrawn/declined back to applied
                if app.status in (
                    models.JobApplication.STATUS_WITHDRAWN,
                    models.JobApplication.STATUS_DECLINED,
                ):
                    app.status = models.JobApplication.STATUS_APPLIED
                    if input.note:
                        app.note = input.note[:1000]
                    app.save(update_fields=["status", "note", "updated_at"])
                else:
                    return app, "Application already on file."
            return app, "Applied."

        app, msg = await sync_to_async(_apply)()
        return types.JobApplicationResponse(
            success=app is not None,
            message=msg,
            client_mutation_id=input.client_mutation_id,
            application_uuid=str(app.uuid) if app else None,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def withdraw_job_application(
        self,
        info: strawberry.Info,
        input: inputs.WithdrawJobApplicationInput,
    ) -> types.JobApplicationResponse:
        actor = info.context.request.user

        def _withdraw():
            try:
                app = models.JobApplication.objects.get(uuid=str(input.application_id))
            except models.JobApplication.DoesNotExist:
                return None, "Application not found."
            if not app.ambassador or not app.ambassador.user_id == getattr(actor, "id", None):
                return None, "You can only withdraw your own application."
            if app.status != models.JobApplication.STATUS_APPLIED:
                return None, f"Application is {app.status}; can't withdraw."
            app.status = models.JobApplication.STATUS_WITHDRAWN
            app.save(update_fields=["status", "updated_at"])
            return app, "Application withdrawn."

        app, msg = await sync_to_async(_withdraw)()
        return types.JobApplicationResponse(
            success=app is not None,
            message=msg,
            client_mutation_id=input.client_mutation_id,
            application_uuid=str(app.uuid) if app else None,
        )


@strawberry.type
class FavoriteAmbassadorMutations:
    """Tenant-curated list of BAs that get first-look at posted jobs.

    Per-tenant unique on (tenant, ambassador). Admins manage from the
    Favorites tab; both spark-admin and client roles in the tenant can
    edit their own list.
    """

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def add_favorite_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.AddFavoriteAmbassadorInput,
    ) -> types.FavoriteAmbassadorResponse:
        actor = info.context.request.user

        def _add():
            try:
                ba_pk = _resolve_id(input.ambassador_id)
            except (TypeError, ValueError, GraphQLError):
                return False, "Invalid ambassador id."

            # Resolve tenant — explicit input wins, else fall back to
            # the actor's bound tenant.
            tenant_id = None
            if input.tenant_id:
                try:
                    tenant_id = _resolve_id(input.tenant_id)
                except (TypeError, ValueError, GraphQLError):
                    return False, "Invalid tenant id."
            else:
                bound = actor.get_tenant() if hasattr(actor, "get_tenant") else None
                if bound:
                    tenant_id = bound.id
            if not tenant_id:
                return False, "No tenant in scope."

            try:
                amb = _Ambassador.objects.get(pk=ba_pk)
            except _Ambassador.DoesNotExist:
                return False, "Ambassador not found."

            fav, created = models.TenantFavoriteAmbassador.objects.get_or_create(
                tenant_id=tenant_id, ambassador=amb,
                defaults={
                    "note": (input.note or "")[:255],
                    "added_by": actor if getattr(actor, "id", None) else None,
                },
            )
            if not created and input.note and fav.note != input.note:
                fav.note = input.note[:255]
                fav.save(update_fields=["note"])
            return True, "Added to favorites." if created else "Already a favorite."

        ok, msg = await sync_to_async(_add)()
        return types.FavoriteAmbassadorResponse(
            success=ok, message=msg, client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def remove_favorite_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.RemoveFavoriteAmbassadorInput,
    ) -> types.FavoriteAmbassadorResponse:
        actor = info.context.request.user

        def _rm():
            try:
                ba_pk = _resolve_id(input.ambassador_id)
            except (TypeError, ValueError, GraphQLError):
                return False, "Invalid ambassador id."
            tenant_id = None
            if input.tenant_id:
                try:
                    tenant_id = _resolve_id(input.tenant_id)
                except (TypeError, ValueError, GraphQLError):
                    return False, "Invalid tenant id."
            else:
                bound = actor.get_tenant() if hasattr(actor, "get_tenant") else None
                if bound:
                    tenant_id = bound.id
            if not tenant_id:
                return False, "No tenant in scope."

            deleted, _ = models.TenantFavoriteAmbassador.objects.filter(
                tenant_id=tenant_id, ambassador_id=ba_pk,
            ).delete()
            return bool(deleted), "Removed from favorites." if deleted else "Wasn't on the list."

        ok, msg = await sync_to_async(_rm)()
        return types.FavoriteAmbassadorResponse(
            success=ok, message=msg, client_mutation_id=input.client_mutation_id,
        )
