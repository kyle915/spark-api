import strawberry
from strawberry import relay
from strawberry.types import Info
from asgiref.sync import sync_to_async

from jobs.models import (
    AmbassadorJob as AmbassadorJobModel,
    Job,
    Status as JobStatus,
)
from jobs.types import AmbassadorJob as AmbassadorJobType

from events.models import Event
from utils.graphql.permissions import StrictIsAuthenticated, IsClientOrSparkAdmin

from .models import Ambassador, AmbassadorEvent
from .types import (
    AmbassadorEventType,
    PublicAmbassadorCreationResponse,
    AmbassadorInvitationResponse,
    AcceptInvitationResponse,
    ApproveAmbassadorResponse,
    UpdateAmbassadorResponse,
    DeleteInvitationResponse,
    CreateAmbassadorReviewResponse,
    UpdateAmbassadorReviewResponse,
    DeleteAmbassadorReviewResponse,
    CreateSkillResponse,
    UpdateSkillResponse,
    DeleteSkillResponse,
    CreateAmbassadorSkillResponse,
    DeleteAmbassadorSkillResponse,
)
from . import inputs
from .services import (
    PublicAmbassadorCreationService,
    AmbassadorInvitationService,
    AcceptInvitationService,
    ApproveAmbassadorService,
    UpdateAmbassadorService,
    DeleteInvitationService,
    CreateAmbassadorReviewService,
    UpdateAmbassadorReviewService,
    DeleteAmbassadorReviewService,
    CreateSkillService,
    UpdateSkillService,
    DeleteSkillService,
    CreateAmbassadorSkillService,
    DeleteAmbassadorSkillService,
)


@strawberry.type
class ApplyAmbassadorEventResponse:
    success: bool
    message: str
    application: AmbassadorEventType | None = None


@strawberry.type
class ApplyAmbassadorJobResponse:
    success: bool
    message: str
    application: AmbassadorJobType | None = None


async def _get_default_application_status(tenant_id: int) -> JobStatus | None:
    """Pick a sensible default status for a new application."""
    for pattern in ("apply", "pending"):
        status = await sync_to_async(
            JobStatus.objects.filter(
                tenant_id=tenant_id, name__icontains=pattern
            ).first
        )()
        if status:
            return status

    return await sync_to_async(
        JobStatus.objects.filter(tenant_id=tenant_id).first
    )()


@strawberry.type
class AmbassadorMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def apply_ambassador_event(
        self, info: Info, event_id: strawberry.ID
    ) -> ApplyAmbassadorEventResponse:
        user = info.context.request.user
        # Manual check removed as StrictIsAuthenticated handles it

        try:
            ambassador = await Ambassador.objects.aget(user=user)
        except Ambassador.DoesNotExist:
            return ApplyAmbassadorEventResponse(
                success=False, message="Ambassador profile not found"
            )

        try:
            event = await Event.objects.select_related("tenant").aget(id=event_id)
        except Event.DoesNotExist:
            return ApplyAmbassadorEventResponse(
                success=False, message="Event not found"
            )

        if await AmbassadorEvent.objects.filter(
            ambassador=ambassador, event=event
        ).aexists():
            return ApplyAmbassadorEventResponse(
                success=False, message="Already applied to this event"
            )

        application = await AmbassadorEvent.objects.acreate(
            ambassador=ambassador,
            event=event,
            tenant=event.tenant,
            is_approved=False,
            created_by=user,
            updated_by=user,
        )

        return ApplyAmbassadorEventResponse(
            success=True, message="Application successful", application=application
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def apply_ambassador_job(
        self, info: Info, job_id: strawberry.ID
    ) -> ApplyAmbassadorJobResponse:
        user = info.context.request.user
        # Manual check removed as StrictIsAuthenticated handles it

        try:
            ambassador = await Ambassador.objects.aget(user=user)
        except Ambassador.DoesNotExist:
            return ApplyAmbassadorJobResponse(
                success=False, message="Ambassador profile not found"
            )

        try:
            job = await Job.objects.select_related("tenant", "rate").aget(id=job_id)
        except Job.DoesNotExist:
            return ApplyAmbassadorJobResponse(success=False, message="Job not found")

        if await AmbassadorJobModel.objects.filter(
            ambassador=ambassador, job=job
        ).aexists():
            return ApplyAmbassadorJobResponse(
                success=False, message="Already applied to this job"
            )

        if job.rate is None:
            return ApplyAmbassadorJobResponse(
                success=False,
                message="Job has no rate configured; please contact the client.",
            )

        status = await _get_default_application_status(job.tenant_id)
        if status is None:
            return ApplyAmbassadorJobResponse(
                success=False,
                message="No status configured for this tenant to store applications.",
            )

        application = await AmbassadorJobModel.objects.acreate(
            ambassador=ambassador,
            job=job,
            tenant=job.tenant,
            status=status,
            rate=job.rate,
            appear_as_rfp=False,
            created_by=user,
            updated_by=user,
        )

        return ApplyAmbassadorJobResponse(
            success=True, message="Application successful", application=application
        )

    @relay.mutation  # Public - no permission_classes
    async def create_public_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.CreatePublicAmbassadorInput,
    ) -> PublicAmbassadorCreationResponse:
        return await PublicAmbassadorCreationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_ambassador_invitation(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorInvitationInput,
    ) -> AmbassadorInvitationResponse:
        return await AmbassadorInvitationService.create(input, info)

    @relay.mutation  # Public with token validation
    async def accept_ambassador_invitation(
        self,
        info: strawberry.Info,
        input: inputs.AcceptAmbassadorInvitationInput,
    ) -> AcceptInvitationResponse:
        return await AcceptInvitationService.accept(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def approve_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.ApproveAmbassadorInput,
    ) -> ApproveAmbassadorResponse:
        return await ApproveAmbassadorService.approve(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def update_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAmbassadorInput,
    ) -> UpdateAmbassadorResponse:
        return await UpdateAmbassadorService.update(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def delete_invitation(
        self,
        info: strawberry.Info,
        input: inputs.DeleteInvitationInput,
    ) -> DeleteInvitationResponse:
        return await DeleteInvitationService.delete(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def create_ambassador_review(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorReviewInput,
    ) -> CreateAmbassadorReviewResponse:
        return await CreateAmbassadorReviewService.create(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def update_ambassador_review(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAmbassadorReviewInput,
    ) -> UpdateAmbassadorReviewResponse:
        return await UpdateAmbassadorReviewService.update(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def delete_ambassador_review(
        self,
        info: strawberry.Info,
        input: inputs.DeleteAmbassadorReviewInput,
    ) -> DeleteAmbassadorReviewResponse:
        return await DeleteAmbassadorReviewService.delete(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_skill(
        self,
        info: strawberry.Info,
        input: inputs.CreateSkillInput,
    ) -> CreateSkillResponse:
        return await CreateSkillService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_skill(
        self,
        info: strawberry.Info,
        input: inputs.UpdateSkillInput,
    ) -> UpdateSkillResponse:
        return await UpdateSkillService.update(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_skill(
        self,
        info: strawberry.Info,
        input: inputs.DeleteSkillInput,
    ) -> DeleteSkillResponse:
        return await DeleteSkillService.delete(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def create_ambassador_skill(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorSkillInput,
    ) -> CreateAmbassadorSkillResponse:
        return await CreateAmbassadorSkillService.create(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def delete_ambassador_skill(
        self,
        info: strawberry.Info,
        input: inputs.DeleteAmbassadorSkillInput,
    ) -> DeleteAmbassadorSkillResponse:
        return await DeleteAmbassadorSkillService.delete(input, info)
