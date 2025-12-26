import strawberry
from strawberry import relay
from strawberry.types import Info
from asgiref.sync import sync_to_async
from django.db.models import Model

from jobs.models import (
    AmbassadorJob as AmbassadorJobModel,
    Job,
    Status as JobStatus,
)
from jobs.types import AmbassadorJob as AmbassadorJobType

from events.models import Event
from utils.graphql.permissions import StrictIsAuthenticated, IsClientOrSparkAdmin
from utils.graphql.mixins import BaseMutationService

from .models import (
    Ambassador,
    AmbassadorEvent,
    AttendanceType,
    AttendanceStatus,
    Source,
    Attendance,
)
from .types import (
    AmbassadorEventType,
    PublicAmbassadorCreationResponse,
    AmbassadorInvitationResponse,
    AcceptInvitationResponse,
    ApproveAmbassadorResponse,
    CreateAmbassadorResponse,
    UpdateAmbassadorResponse,
    DeleteInvitationResponse,
    CreateAmbassadorReviewResponse,
    UpdateAmbassadorReviewResponse,
    DeleteAmbassadorReviewResponse,
    CreateAmbassadorNoteResponse,
    UpdateAmbassadorNoteResponse,
    DeleteAmbassadorNoteResponse,
    CreateSkillResponse,
    UpdateSkillResponse,
    DeleteSkillResponse,
    CreateAmbassadorSkillResponse,
    DeleteAmbassadorSkillResponse,
    UpsertAmbassadorProfileResponse,
    AttendanceTypeDetailResponse,
    AttendanceStatusDetailResponse,
    SourceDetailResponse,
    AttendanceDetailResponse,
)
from . import inputs
from .services import (
    PublicAmbassadorCreationService,
    AmbassadorInvitationService,
    AcceptInvitationService,
    ApproveAmbassadorService,
    CreateAmbassadorService,
    UpdateAmbassadorService,
    DeleteInvitationService,
    CreateAmbassadorReviewService,
    UpdateAmbassadorReviewService,
    DeleteAmbassadorReviewService,
    CreateAmbassadorNoteService,
    UpdateAmbassadorNoteService,
    DeleteAmbassadorNoteService,
    CreateSkillService,
    UpdateSkillService,
    DeleteSkillService,
    CreateAmbassadorSkillService,
    DeleteAmbassadorSkillService,
    UpsertAmbassadorProfileService,
)
from .envelopes import AmbassadorEventApplicationMailer, NotifyApplicationToClientMailer
from utils.mailer import MailChain


class TenantOptionalMutationService(BaseMutationService):
    """Mutation service that skips tenant assignment when the model lacks the field."""

    async def save(self) -> Model:
        """Save the model handling optional tenant fields."""
        await self.validations()

        model_class = self.get_model()
        is_update: bool = hasattr(
            self.input, "id") and self.input.id is not None
        if is_update:
            model = await sync_to_async(model_class.objects.get)(id=self.input.id)
            if self.user and hasattr(model, "updated_by"):
                setattr(model, "updated_by", self.user)
        else:
            model = model_class()
            if self.user and hasattr(model, "created_by"):
                setattr(model, "created_by", self.user)
            if self.is_public and getattr(self.input, "tenant_id", None):
                self.tenant_id = self.input.tenant_id

        params: dict = self.input.to_dict(["tenant_id", "id"])
        for key, value in params.items():
            setattr(model, key, value)

        if hasattr(model, "tenant_id") and self.tenant_id is not None:
            setattr(model, "tenant_id", self.tenant_id)

        await sync_to_async(model.save)()
        return model


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
    """Pick a sensible default status for a new application using the status slug."""
    for slug in ("pending", "apply"):
        status = await sync_to_async(
            JobStatus.objects.filter(tenant_id=tenant_id, slug=slug).first
        )()
        if status:
            return status

    return await sync_to_async(JobStatus.objects.filter(tenant_id=tenant_id).first)()


@strawberry.type
class AmbassadorMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def apply_ambassador_event(
        self,
        info: Info,
        event_id: strawberry.ID,
        ambassador_id: strawberry.ID | None = None,
    ) -> ApplyAmbassadorEventResponse:
        user = info.context.request.user
        # Manual check removed as StrictIsAuthenticated handles it

        if ambassador_id:
            try:
                ambassador = await Ambassador.objects.aget(id=ambassador_id)
            except (Ambassador.DoesNotExist, ValueError):
                return ApplyAmbassadorEventResponse(
                    success=False, message="Ambassador profile not found"
                )
        else:
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

        await MailChain.send_chain_async([
            AmbassadorEventApplicationMailer(application),
            NotifyApplicationToClientMailer(application),
        ])

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
    async def create_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorInput,
    ) -> CreateAmbassadorResponse:
        return await CreateAmbassadorService.create(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def update_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAmbassadorInput,
    ) -> UpdateAmbassadorResponse:
        return await UpdateAmbassadorService.update(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def upsert_ambassador_profile(
        self,
        info: strawberry.Info,
        input: inputs.UpsertAmbassadorProfileInput,
    ) -> UpsertAmbassadorProfileResponse:
        return await UpsertAmbassadorProfileService.upsert(input, info)

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
    async def create_ambassador_note(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorNoteInput,
    ) -> CreateAmbassadorNoteResponse:
        return await CreateAmbassadorNoteService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_ambassador_note(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAmbassadorNoteInput,
    ) -> UpdateAmbassadorNoteResponse:
        return await UpdateAmbassadorNoteService.update(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_ambassador_note(
        self,
        info: strawberry.Info,
        input: inputs.DeleteAmbassadorNoteInput,
    ) -> DeleteAmbassadorNoteResponse:
        return await DeleteAmbassadorNoteService.delete(input, info)

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


class AttendanceTypeMutationService(TenantOptionalMutationService):
    response_class = AttendanceTypeDetailResponse
    model_field_name = "attendance_type"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return AttendanceType


class AttendanceStatusMutationService(TenantOptionalMutationService):
    response_class = AttendanceStatusDetailResponse
    model_field_name = "attendance_status"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return AttendanceStatus


class SourceMutationService(TenantOptionalMutationService):
    response_class = SourceDetailResponse
    model_field_name = "source"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return Source


class AttendanceMutationService(TenantOptionalMutationService):
    response_class = AttendanceDetailResponse
    model_field_name = "attendance"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return Attendance


@strawberry.type
class AttendanceMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_attendance_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateAttendanceTypeInput,
    ) -> AttendanceTypeDetailResponse:
        return await AttendanceTypeMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_attendance_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAttendanceTypeInput,
    ) -> AttendanceTypeDetailResponse:
        return await AttendanceTypeMutationService.update(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_attendance_status(
        self,
        info: strawberry.Info,
        input: inputs.CreateAttendanceStatusInput,
    ) -> AttendanceStatusDetailResponse:
        return await AttendanceStatusMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_attendance_status(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAttendanceStatusInput,
    ) -> AttendanceStatusDetailResponse:
        return await AttendanceStatusMutationService.update(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_source(
        self,
        info: strawberry.Info,
        input: inputs.CreateSourceInput,
    ) -> SourceDetailResponse:
        return await SourceMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_source(
        self,
        info: strawberry.Info,
        input: inputs.UpdateSourceInput,
    ) -> SourceDetailResponse:
        return await SourceMutationService.update(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_attendance(
        self,
        info: strawberry.Info,
        input: inputs.CreateAttendanceInput,
    ) -> AttendanceDetailResponse:
        return await AttendanceMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_attendance(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAttendanceInput,
    ) -> AttendanceDetailResponse:
        return await AttendanceMutationService.update(input, info)
