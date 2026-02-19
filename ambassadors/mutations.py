import strawberry
from strawberry import relay
from strawberry.types import Info
from asgiref.sync import sync_to_async
from django.db.models import Model
from graphql import GraphQLError

from jobs.models import (
    AmbassadorJob as AmbassadorJobModel,
    Job,
    Status as JobStatus,
)
from jobs.types import AmbassadorJob as AmbassadorJobType

from events.models import Event
from utils.graphql.permissions import StrictIsAuthenticated, IsClientOrSparkAdmin
from utils.graphql.mixins import BaseMutationService, resolve_id_to_int

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
    GroupTypeResponse,
    AmbassadorGroupResponse,
    AddAmbassadorsToGroupResponse,
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
    GroupTypeMutationService,
    AmbassadorGroupMutationService,
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
                resolved_ambassador_id = resolve_id_to_int(ambassador_id)
                ambassador = await Ambassador.objects.aget(id=resolved_ambassador_id)
            except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
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
            resolved_event_id = resolve_id_to_int(event_id)
            event = await Event.objects.select_related("tenant").aget(
                id=resolved_event_id
            )
        except (Event.DoesNotExist, ValueError, TypeError, GraphQLError):
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
            resolved_job_id = resolve_id_to_int(job_id)
            job = await Job.objects.select_related("tenant", "rate", "event").aget(
                id=resolved_job_id
            )
        except (Job.DoesNotExist, ValueError, TypeError, GraphQLError):
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

        if not await AmbassadorEvent.objects.filter(
            ambassador=ambassador, event=job.event
        ).aexists():
            await AmbassadorEvent.objects.acreate(
                ambassador=ambassador,
                event=job.event,
                tenant=job.tenant,
                is_approved=False,
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
        return await PublicAmbassadorCreationService.create(
            input, info, ambassador_is_active=False
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_ambassador_with_user(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorWithUserInput,
    ) -> PublicAmbassadorCreationResponse:
        return await PublicAmbassadorCreationService.create(
            input, info, ambassador_is_active=input.is_active
        )

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

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def accept_by_token(self, info: strawberry.Info, input: inputs.AcceptByTokenInput) -> AcceptInvitationResponse:
        return await AcceptInvitationService.accept_by_token(input, info)

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
        """Create ambassador profile including optional about_me."""
        return await CreateAmbassadorService.create(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def update_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAmbassadorInput,
    ) -> UpdateAmbassadorResponse:
        """Update ambassador profile fields including about_me."""
        return await UpdateAmbassadorService.update(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def upsert_ambassador_profile(
        self,
        info: strawberry.Info,
        input: inputs.UpsertAmbassadorProfileInput,
    ) -> UpsertAmbassadorProfileResponse:
        """Upsert ambassador profile and related data, including about_me."""
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


@strawberry.type
class GroupTypeMutations:
    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def create_group_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateGroupTypeInput,
    ) -> GroupTypeResponse:
        return await GroupTypeMutationService.create(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def update_group_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateGroupTypeInput,
    ) -> GroupTypeResponse:
        return await GroupTypeMutationService.update(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def delete_group_type(
        self,
        info: strawberry.Info,
        input: inputs.DeleteGroupTypeInput,
    ) -> GroupTypeResponse:
        return await GroupTypeMutationService.delete(input, info)


@strawberry.type
class AmbassadorGroupMutations:
    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def create_ambassador_group(
        self,
        info: strawberry.Info,
        input: inputs.CreateAmbassadorGroupInput,
    ) -> AmbassadorGroupResponse:
        return await AmbassadorGroupMutationService.create(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def update_ambassador_group(
        self,
        info: strawberry.Info,
        input: inputs.UpdateAmbassadorGroupInput,
    ) -> AmbassadorGroupResponse:
        return await AmbassadorGroupMutationService.update(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def delete_ambassador_group(
        self,
        info: strawberry.Info,
        input: inputs.DeleteAmbassadorGroupInput,
    ) -> AmbassadorGroupResponse:
        return await AmbassadorGroupMutationService.delete(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def add_ambassadors_to_group(
        self,
        info: strawberry.Info,
        input: inputs.AddAmbassadorsToGroupInput,
    ) -> AddAmbassadorsToGroupResponse:
        return await AmbassadorGroupMutationService.add_ambassadors_to_group(
            input, info, response_class=AddAmbassadorsToGroupResponse
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def remove_ambassadors_from_group(
        self,
        info: strawberry.Info,
        input: inputs.RemoveAmbassadorsFromGroupInput,
    ) -> AmbassadorGroupResponse:
        return await AmbassadorGroupMutationService.remove_ambassadors_from_group(
            input, info, response_class=AmbassadorGroupResponse
        )


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

    async def set_user_and_tenant(self, info: strawberry.Info) -> "AttendanceMutationService":
        """
        Override tenant resolution so attendance creation does not require tenant membership.
        Ambassadors are not tied to tenants, so we skip the tenant lookup entirely.
        """
        self.info = info
        self.user = await self.get_user(info)
        self.is_spark_schema = self.is_spark_schema_request(
            info, user=self.user)
        self.tenant_id = None
        return self

    async def validations(self):
        """Skip tenant validations for attendance because it is tenant-agnostic."""
        return None

    async def _assign_status_for_creator(self):
        """Force attendance status based on creator role."""
        role_slug = self.get_role_slug(self.user)
        if role_slug not in {"spark-admin", "ambassador"}:
            return

        status_slug = "approved" if role_slug == "spark-admin" else "pending"
        tenant_id = getattr(self.input, "tenant_id", None)

        queryset = AttendanceStatus.objects.filter(slug=status_slug)
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)

        status = await sync_to_async(queryset.first)()

        # fallback to global status if tenant-specific not found
        if not status and tenant_id:
            status = await sync_to_async(
                AttendanceStatus.objects.filter(
                    slug=status_slug, tenant__isnull=True
                ).first
            )()

        if not status:
            raise GraphQLError(
                f"Attendance status with slug '{status_slug}' not found."
            )

        self.input.attendance_status_id = status.id

    async def save(self) -> Model:
        """Set status based on role before saving."""
        await self._assign_status_for_creator()
        return await super().save()


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
