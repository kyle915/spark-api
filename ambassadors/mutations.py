import strawberry
from strawberry import relay
from strawberry.types import Info
from asgiref.sync import sync_to_async
from django.db.models import Model, Avg, Count
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
    AmbassadorRating,
)
from .types import (
    AmbassadorEventType,
    PublicAmbassadorCreationResponse,
    AmbassadorInvitationResponse,
    AcceptInvitationResponse,
    ApproveAmbassadorResponse,
    DisableAmbassadorResponse,
    RegenerateAmbassadorPasswordsResponse,
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
    RegisterPushTokenResponse,
    OAuthSignInResponse,
    LocationPingResponse,
    RespondToShiftOfferResponse,
    InviteAmbassadorToShiftResponse,
    CancelShiftInviteResponse,
    RateAmbassadorResponse,
)
from . import inputs
from .services import (
    PublicAmbassadorCreationService,
    AmbassadorInvitationService,
    AcceptInvitationService,
    ApproveAmbassadorService,
    DisableAmbassadorService,
    RegenerateAmbassadorPasswordsService,
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
    RegisterPushTokenService,
    OAuthSignInService,
    LocationPingService,
    ShiftOfferService,
    set_ambassador_job_real_amount_from_clock_out,
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
            model_id = getattr(self.input, "id", None)
            try:
                resolved_id = resolve_id_to_int(model_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {model_id}")
            model = await sync_to_async(model_class.objects.get)(id=resolved_id)
            if self.user and hasattr(model, "updated_by"):
                setattr(model, "updated_by", self.user)
        else:
            model = model_class()
            if self.user and hasattr(model, "created_by"):
                setattr(model, "created_by", self.user)
            if self.is_public and getattr(self.input, "tenant_id", None):
                try:
                    self.tenant_id = resolve_id_to_int(self.input.tenant_id)
                except (TypeError, ValueError, GraphQLError):
                    raise GraphQLError(f"Invalid tenant_id: {self.input.tenant_id}")

        params: dict = self.input.to_dict(["tenant_id", "id"])
        for key, value in list(params.items()):
            if key.endswith("_id") and value is not None:
                try:
                    params[key] = resolve_id_to_int(value)
                except (TypeError, ValueError, GraphQLError):
                    raise GraphQLError(f"Invalid {key}: {value}")
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

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def invite_ambassador_to_shift(
        self,
        info: strawberry.Info,
        input: inputs.InviteAmbassadorToShiftInput,
    ) -> InviteAmbassadorToShiftResponse:
        """Admin invites a specific BA to a specific event.

        Creates an `AmbassadorEvent` row with `is_approved=False`.
        The existing post_save signal fans out:
          - "New shift offered" push to the BA's device
          - Google Calendar sync if the BA is connected
        BA can then accept / decline via the mobile shift-offer
        screen, which flips is_approved=True (accept) or deletes
        the row (decline).

        Idempotent on (ambassador, event) — re-inviting the same
        BA returns success=False with a helpful message so admins
        don't accidentally double-push.
        """
        user = info.context.request.user

        try:
            resolved_ambassador_id = resolve_id_to_int(input.ambassador_id)
            ambassador = await Ambassador.objects.aget(id=resolved_ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
            return InviteAmbassadorToShiftResponse(
                success=False,
                message="Ambassador not found.",
                client_mutation_id=input.client_mutation_id,
            )

        try:
            resolved_event_id = resolve_id_to_int(input.event_id)
            event = await Event.objects.select_related("tenant").aget(
                id=resolved_event_id
            )
        except (Event.DoesNotExist, ValueError, TypeError, GraphQLError):
            return InviteAmbassadorToShiftResponse(
                success=False,
                message="Event not found.",
                client_mutation_id=input.client_mutation_id,
            )

        if await AmbassadorEvent.objects.filter(
            ambassador=ambassador, event=event
        ).aexists():
            return InviteAmbassadorToShiftResponse(
                success=False,
                message="This BA is already invited to this shift.",
                client_mutation_id=input.client_mutation_id,
            )

        try:
            ae = await AmbassadorEvent.objects.acreate(
                ambassador=ambassador,
                event=event,
                tenant=event.tenant,
                is_approved=False,
                created_by=user,
                updated_by=user,
            )
        except Exception as exc:
            return InviteAmbassadorToShiftResponse(
                success=False,
                message=f"Could not create invite: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

        # Audit log: write a ba_invited entry on the request that
        # spawned this event so the activity timeline on the request
        # detail page picks it up. Best-effort; never raises.
        try:
            from asgiref.sync import sync_to_async
            from events.models import Request, RequestActivityLog

            req = None
            if getattr(event, "request_id", None):
                req = await sync_to_async(
                    lambda: Request.objects.filter(id=event.request_id).first()
                )()
            if req is not None:
                ba_name = (
                    " ".join(
                        filter(
                            None,
                            [
                                getattr(ambassador, "first_name", None),
                                getattr(ambassador, "last_name", None),
                            ],
                        )
                    )
                    or getattr(ambassador, "email", "")
                    or "BA"
                )
                await sync_to_async(RequestActivityLog.objects.create)(
                    tenant=event.tenant,
                    request=req,
                    kind=RequestActivityLog.KIND_BA_INVITED,
                    actor_user=user if getattr(user, "id", None) else None,
                    summary=f"Invited {ba_name}",
                    metadata={
                        "ambassador_uuid": str(ambassador.uuid),
                        "ba_name": ba_name,
                        "event_uuid": str(event.uuid),
                    },
                )
        except Exception:
            pass

        return InviteAmbassadorToShiftResponse(
            success=True,
            message="Invite sent. The BA has been notified.",
            client_mutation_id=input.client_mutation_id,
            ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def cancel_shift_invite(
        self,
        info: strawberry.Info,
        input: inputs.CancelShiftInviteInput,
    ) -> CancelShiftInviteResponse:
        """Admin retracts a pending invite or removes an accepted BA
        from a shift. Deletes the AmbassadorEvent row.

        Symmetric with the BA's decline path. Returns success=False
        when the row doesn't exist (already declined / never created)
        rather than raising — front-end can treat as idempotent.
        """
        try:
            ae = await AmbassadorEvent.objects.select_related(
                "ambassador", "event"
            ).aget(uuid=str(input.ambassador_event_uuid))
        except AmbassadorEvent.DoesNotExist:
            return CancelShiftInviteResponse(
                success=False,
                message="Invite not found — may have already been declined.",
                client_mutation_id=input.client_mutation_id,
            )

        try:
            await ae.adelete()
        except Exception as exc:  # noqa: BLE001
            return CancelShiftInviteResponse(
                success=False,
                message=f"Could not cancel invite: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

        return CancelShiftInviteResponse(
            success=True,
            message="Invite cancelled.",
            client_mutation_id=input.client_mutation_id,
            ambassador_event_uuid=input.ambassador_event_uuid,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def rate_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.RateAmbassadorInput,
    ) -> RateAmbassadorResponse:
        """Leave (or update) a 1-5 star rating for a BA.

        Both Ignite admins and clients can rate. The rating is tied to a
        gig via `event_id` when supplied; omit it for a general profile
        rating. Re-rating the same (ambassador, event) by the same user
        overwrites the prior row rather than stacking duplicates.

        `by_client` is derived server-side from the caller's role — never
        trusted from the client — so the query layer can keep client
        ratings out of other clients' view. After writing, the BA's
        canonical `Ambassador.rating` (rounded mean of *all* ratings) is
        recomputed, and the response echoes the average/count visible to
        *this* caller so the UI can update without a refetch.
        """
        from tenants.models import TenantedUser

        user = info.context.request.user

        # --- validate score -------------------------------------------------
        try:
            score = int(input.score)
        except (TypeError, ValueError):
            return RateAmbassadorResponse(
                success=False,
                message="Score must be a whole number from 1 to 5.",
                client_mutation_id=input.client_mutation_id,
            )
        if score < AmbassadorRating.SCORE_MIN or score > AmbassadorRating.SCORE_MAX:
            return RateAmbassadorResponse(
                success=False,
                message="Score must be between 1 and 5 stars.",
                client_mutation_id=input.client_mutation_id,
            )

        # --- resolve ambassador ---------------------------------------------
        try:
            resolved_ambassador_id = resolve_id_to_int(input.ambassador_id)
            ambassador = await Ambassador.objects.aget(id=resolved_ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
            return RateAmbassadorResponse(
                success=False,
                message="Ambassador not found.",
                client_mutation_id=input.client_mutation_id,
            )

        # --- resolve event (optional) + tenant ------------------------------
        event = None
        tenant = None
        if input.event_id not in (None, ""):
            try:
                resolved_event_id = resolve_id_to_int(input.event_id)
                event = await Event.objects.select_related("tenant").aget(
                    id=resolved_event_id
                )
                tenant = event.tenant
            except (Event.DoesNotExist, ValueError, TypeError, GraphQLError):
                return RateAmbassadorResponse(
                    success=False,
                    message="Event not found.",
                    client_mutation_id=input.client_mutation_id,
                )

        if tenant is None:
            # Profile-level rating (no gig): fall back to the rater's own
            # active tenant membership so the row is still tenant-scoped.
            tu = await (
                TenantedUser.objects.select_related("tenant")
                .filter(user=user, is_active=True)
                .afirst()
            )
            tenant = tu.tenant if tu is not None else None

        if tenant is None:
            return RateAmbassadorResponse(
                success=False,
                message="Could not determine a company for this rating. "
                "Rate the BA from a specific gig instead.",
                client_mutation_id=input.client_mutation_id,
            )

        # --- role → by_client flag (server-trusted) -------------------------
        @sync_to_async
        def _role_slug() -> str:
            # Authoritative by PK — request.user.role doesn't reliably hydrate
            # in the async path (would mis-attribute a client rating).
            pk = getattr(user, "pk", None)
            if pk is None:
                return ""
            try:
                from django.contrib.auth import get_user_model

                db_user = (
                    get_user_model()
                    .objects.select_related("role")
                    .filter(pk=pk)
                    .first()
                )
                return (
                    getattr(getattr(db_user, "role", None), "slug", "") or ""
                ).lower()
            except Exception:
                return ""

        by_client = (await _role_slug()) == "client"

        # --- upsert the rating ----------------------------------------------
        comment = (input.comment or "").strip() or None
        try:
            rating, _created = await AmbassadorRating.objects.aupdate_or_create(
                ambassador=ambassador,
                event=event,
                created_by=user,
                defaults={
                    "score": score,
                    "comment": comment,
                    "tenant": tenant,
                    "by_client": by_client,
                    "updated_by": user,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return RateAmbassadorResponse(
                success=False,
                message=f"Could not save rating: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

        # --- recompute canonical BA rating (mean of ALL ratings) ------------
        overall = await AmbassadorRating.objects.filter(
            ambassador=ambassador
        ).aaggregate(avg=Avg("score"), count=Count("id"))
        overall_avg = overall["avg"] or 0.0
        ambassador.rating = round(overall_avg)
        await ambassador.asave(update_fields=["rating"])

        # --- aggregate visible to THIS caller -------------------------------
        # Admins see every rating; a client only sees their own, so the
        # number they see matches what `ambassadorRatings` returns for them.
        if by_client:
            visible = await AmbassadorRating.objects.filter(
                ambassador=ambassador, created_by=user
            ).aaggregate(avg=Avg("score"), count=Count("id"))
        else:
            visible = overall

        return RateAmbassadorResponse(
            success=True,
            message="Rating saved.",
            client_mutation_id=input.client_mutation_id,
            ambassador_rating=rating,
            ambassador_average=round(visible["avg"] or 0.0, 2),
            ambassador_rating_count=visible["count"] or 0,
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def apply_ambassador_job(
        self,
        info: Info,
        job_id: strawberry.ID,
        accepted_terms: bool = False,
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

        if job.rate_id is None:
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
            accepted_terms=accepted_terms,
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

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def disable_ambassador(
        self,
        info: strawberry.Info,
        input: inputs.DisableAmbassadorInput,
    ) -> DisableAmbassadorResponse:
        return await DisableAmbassadorService.disable(input, info)

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def regenerate_ambassador_passwords(
        self,
        info: strawberry.Info,
        input: inputs.RegenerateAmbassadorPasswordsInput,
    ) -> RegenerateAmbassadorPasswordsResponse:
        return await RegenerateAmbassadorPasswordsService.regenerate(input, info)

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
class AmbassadorMobileMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def disable_ambassador_mobile(
        self,
        info: strawberry.Info,
    ) -> DisableAmbassadorResponse:
        return await DisableAmbassadorService.disable_mobile(info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def register_push_token(
        self,
        info: strawberry.Info,
        input: inputs.RegisterPushTokenInput,
    ) -> RegisterPushTokenResponse:
        return await RegisterPushTokenService.register(input, info)

    # NB: no permission class — sign-in is the path to becoming
    # authenticated, so requiring auth would be a chicken-and-egg.
    @relay.mutation
    async def apple_sign_in(
        self,
        info: strawberry.Info,
        input: inputs.AppleSignInInput,
    ) -> OAuthSignInResponse:
        return await OAuthSignInService.sign_in_with_apple(input, info)

    @relay.mutation
    async def google_sign_in(
        self,
        info: strawberry.Info,
        input: inputs.GoogleSignInInput,
    ) -> OAuthSignInResponse:
        return await OAuthSignInService.sign_in_with_google(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def location_ping(
        self,
        info: strawberry.Info,
        input: inputs.LocationPingInput,
    ) -> LocationPingResponse:
        return await LocationPingService.record(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def respond_to_shift_offer(
        self,
        info: strawberry.Info,
        input: inputs.RespondToShiftOfferInput,
    ) -> RespondToShiftOfferResponse:
        return await ShiftOfferService.respond(input, info)


# -------------------------------------------------------------------
# BA-side shift attendance — arrive / clock-in / clock-out
# -------------------------------------------------------------------
#
# Mobile uses these from the shift-detail screen. Each one creates an
# Attendance row keyed to the BA's ambassador_event uuid; admin sees
# the timestamps + GPS in the shift replay panel.

@strawberry.input
class ArriveAtShiftInput:
    ambassador_event_uuid: strawberry.ID
    latitude: float | None = None
    longitude: float | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class ClockInToShiftInput:
    ambassador_event_uuid: strawberry.ID
    latitude: float | None = None
    longitude: float | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class ClockOutOfShiftInput:
    ambassador_event_uuid: strawberry.ID
    latitude: float | None = None
    longitude: float | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class ShiftAttendanceResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    attendance_uuid: str | None = None
    clock_time: str | None = None
    kind: str | None = None  # "arrived" | "clock_in" | "clock_out"


def _resolve_amb_event_by_uuid(uuid: str):
    from ambassadors.models import AmbassadorEvent
    try:
        return AmbassadorEvent.objects.select_related(
            "ambassador", "ambassador__user", "event"
        ).get(uuid=uuid)
    except AmbassadorEvent.DoesNotExist:
        return None


def _ensure_source(name: str):
    """Get-or-create the Source lookup row by name."""
    from ambassadors.models import Source
    source, _ = Source.objects.get_or_create(name=name)
    return source


def _record_attendance(*, amb_event, source_name: str, coordinates, actor):
    """Insert one Attendance row + return it.

    `coordinates` is an optional [lat, lng] list (matches the model's
    ArrayField(size=2)). Wrapping in sync_to_async happens at the
    caller — this stays pure-sync so it can run inside @sync_to_async.
    """
    from ambassadors.models import Attendance
    from django.utils import timezone as _tz
    source = _ensure_source(source_name)
    return Attendance.objects.create(
        clock_time=_tz.now(),
        coordinates=coordinates,
        ambassador=amb_event.ambassador,
        job=None,
        event=amb_event.event,
        source=source,
    )


@strawberry.type
class ShiftAttendanceMutations:
    """BA-side mobile mutations for the shift detail screen."""

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def arrive_at_shift(
        self, info: strawberry.Info, input: ArriveAtShiftInput,
    ) -> ShiftAttendanceResponse:
        """'I'm here' — first ping when the BA arrives at the venue."""
        return await _do_attendance(info, input, kind="arrived")

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def clock_in_to_shift(
        self, info: strawberry.Info, input: ClockInToShiftInput,
    ) -> ShiftAttendanceResponse:
        """Start the activation timer."""
        return await _do_attendance(info, input, kind="clock_in")

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def clock_out_of_shift(
        self, info: strawberry.Info, input: ClockOutOfShiftInput,
    ) -> ShiftAttendanceResponse:
        """End the activation timer."""
        return await _do_attendance(info, input, kind="clock_out")


async def _do_attendance(info, input, *, kind: str) -> "ShiftAttendanceResponse":
    actor = info.context.request.user

    def _go():
        amb_event = _resolve_amb_event_by_uuid(str(input.ambassador_event_uuid))
        if not amb_event:
            return None, "Shift not found."
        # Authz — BA can only clock themselves
        own_user_id = (
            amb_event.ambassador.user_id if amb_event.ambassador else None
        )
        if own_user_id and getattr(actor, "id", None) != own_user_id:
            return None, "Not your shift."
        coords = None
        if input.latitude is not None and input.longitude is not None:
            coords = [float(input.latitude), float(input.longitude)]
        att = _record_attendance(
            amb_event=amb_event, source_name=kind,
            coordinates=coords, actor=actor,
        )
        return att, "OK"

    att, msg = await sync_to_async(_go)()
    if att is None:
        return ShiftAttendanceResponse(
            success=False, message=msg,
            client_mutation_id=None, kind=None,
        )
    return ShiftAttendanceResponse(
        success=True,
        message=msg,
        client_mutation_id=None,
        attendance_uuid=str(att.uuid),
        clock_time=att.clock_time.isoformat(),
        kind=kind,
    )


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

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def assign_group_to_job(
        self,
        info: strawberry.Info,
        input: inputs.AssignGroupToJobInput,
    ) -> AmbassadorGroupResponse:
        return await AmbassadorGroupMutationService.assign_group_to_job(
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
        is_update = hasattr(self.input, "id") and self.input.id is not None
        await self._assign_status_for_creator()
        attendance = await super().save()

        if not is_update:
            await set_ambassador_job_real_amount_from_clock_out(attendance)

        return attendance


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
