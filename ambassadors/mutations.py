import logging

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
from utils.graphql.mixins import (
    BaseMutationService,
    SparkGraphQLMixin,
    resolve_id_to_int,
)

from .models import (
    Ambassador,
    AmbassadorEvent,
    AmbassadorPhoto,
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
    MileageSessionResponse,
    RespondToShiftOfferResponse,
    InviteAmbassadorToShiftResponse,
    CancelShiftInviteResponse,
    BulkInviteResponse,
    RateAmbassadorResponse,
    UpdateBaProfileResponse,
    BaSelfProfileType,
    PushPreferences,
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
    MileageService,
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

        # Push the "New shift offered" notification to the BA's device(s).
        # Best-effort — never block the invite on push delivery. (The
        # AmbassadorEvent post_save signal only does calendar sync; the
        # shift-offer push was never actually wired, so invited BAs got no
        # notification. This is that wire-up.) Delivers only if the BA has
        # an active PushDevice (i.e. has signed into the app + allowed
        # notifications); otherwise it's a no-op.
        try:
            from ambassadors.push import send_push_to_user

            event_label = getattr(event, "name", None) or "a shift"
            await send_push_to_user(
                ambassador.user_id,
                title="New shift offered",
                body=f"You've been invited to {event_label}. Tap to accept or decline.",
                data={
                    "type": "shift_offer",
                    "ambassadorEventUuid": str(ae.uuid),
                    "eventUuid": str(event.uuid),
                },
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "shift-offer push failed for ambassador_event=%s", ae.uuid
            )

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

        # Snapshot fields we need for the push BEFORE delete — the
        # adelete() voids the in-memory references to ambassador/event.
        was_approved = bool(getattr(ae, "is_approved", False))
        ba_user_id = (
            getattr(getattr(ae, "ambassador", None), "user_id", None)
        )
        event_obj = getattr(ae, "event", None)
        event_name = getattr(event_obj, "name", None) or "your shift"
        event_uuid = (
            str(getattr(event_obj, "uuid", "")) if event_obj else ""
        )
        event_date = getattr(event_obj, "date", None) or getattr(
            event_obj, "start_time", None
        )

        try:
            await ae.adelete()
        except Exception as exc:  # noqa: BLE001
            return CancelShiftInviteResponse(
                success=False,
                message=f"Could not cancel invite: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

        # Push to the BA. Two flavors of "cancelled" but a single push
        # tone — what the BA needs to know is "you're no longer on
        # this shift, don't show up." was_approved tells them whether
        # they were already counting on it (more urgent wording) vs.
        # just an in-flight invite getting retracted.
        if ba_user_id:
            try:
                from ambassadors.push import send_push_to_user

                date_str = ""
                if event_date is not None:
                    try:
                        date_str = f" on {event_date.strftime('%a %b %-d')}"
                    except Exception:
                        date_str = ""
                title = (
                    "Shift cancelled" if was_approved else "Invite retracted"
                )
                body = (
                    f"You've been removed from {event_name}{date_str}."
                    if was_approved
                    else f"The invite for {event_name}{date_str} was retracted."
                )
                await send_push_to_user(
                    ba_user_id,
                    title=title,
                    body=body,
                    data={
                        "type": "shift_cancelled",
                        "kind": "shift_cancelled",
                        "eventUuid": event_uuid,
                        "wasApproved": "1" if was_approved else "0",
                    },
                )
            except Exception:
                # Best-effort — never block the cancel on a push miss.
                pass

        return CancelShiftInviteResponse(
            success=True,
            message="Invite cancelled.",
            client_mutation_id=input.client_mutation_id,
            ambassador_event_uuid=input.ambassador_event_uuid,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def bulk_invite_ambassadors_to_shift(
        self,
        info: strawberry.Info,
        input: inputs.BulkInviteToShiftInput,
    ) -> BulkInviteResponse:
        """Invite many BAs to one event in a single round-trip.

        Loops `ambassador_ids` and reuses the existing single-invite
        resolver (`invite_ambassador_to_shift`) per BA so every invite
        goes through the SAME path: AmbassadorEvent create, the
        (ambassador, event) dedupe, the request activity-log entry, and
        the "New shift offered" push. We never reimplement those
        side-effects here — this is purely a batch wrapper.

        Counting: a BA whose single-invite returns success=True is
        counted as `invited`; anyone the single-invite skipped
        (already-invited) or that raised is counted as `skipped`.
        Tenant-gated: clients can only invite into their own tenant's
        event; admins into any.
        """
        # Tenant gate. Mirror the receipts/recaps posture: a client-role
        # caller is pinned to their own tenant and may only target an
        # event in that tenant; admins (spark-admin / staff / super /
        # @igniteproductions.co) pass through to any tenant's event.
        user = info.context.request.user
        try:
            resolved_event_id = resolve_id_to_int(input.event_id)
        except (ValueError, TypeError, GraphQLError):
            return BulkInviteResponse(
                success=False,
                message="Event not found.",
                client_mutation_id=input.client_mutation_id,
                invited=0,
                skipped=len(input.ambassador_ids or []),
            )

        from utils.graphql.permissions import (
            email_grants_ignite_admin,
            resolve_request_user_access,
        )

        role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
        is_admin = (
            is_staff
            or is_super
            or role_slug == "spark-admin"
            or email_grants_ignite_admin(email)
        )

        try:
            event = await Event.objects.select_related("tenant").aget(
                id=resolved_event_id
            )
        except Event.DoesNotExist:
            return BulkInviteResponse(
                success=False,
                message="Event not found.",
                client_mutation_id=input.client_mutation_id,
                invited=0,
                skipped=len(input.ambassador_ids or []),
            )

        if not is_admin:
            # Client-role: confirm the event belongs to the caller's tenant.
            mixin = SparkGraphQLMixin()
            try:
                tenant = await mixin.get_user_tenant(info, user=user)
            except GraphQLError:
                tenant = None
            if tenant is None or tenant.id != event.tenant_id:
                return BulkInviteResponse(
                    success=False,
                    message="You do not have permission to invite BAs to this event.",
                    client_mutation_id=input.client_mutation_id,
                    invited=0,
                    skipped=len(input.ambassador_ids or []),
                )

        invited = 0
        skipped = 0
        # De-dupe the incoming id list so a repeated id can't double-count.
        seen: set[str] = set()
        for ambassador_id in input.ambassador_ids or []:
            key = str(ambassador_id)
            if key in seen:
                skipped += 1
                continue
            seen.add(key)

            single_input = inputs.InviteAmbassadorToShiftInput(
                ambassador_id=ambassador_id,
                event_id=input.event_id,
            )
            try:
                # Reuse the EXACT single-invite path (create + dedupe +
                # push + activity log). The decorated resolver is a plain
                # coroutine function at the attribute level, so calling it
                # directly runs the same body.
                result = await self.invite_ambassador_to_shift(info, single_input)
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).exception(
                    "bulk invite: single-invite failed ba=%s event=%s",
                    ambassador_id,
                    input.event_id,
                )
                skipped += 1
                continue

            if getattr(result, "success", False):
                invited += 1
            else:
                skipped += 1

        if invited and skipped:
            message = f"Invited {invited} BA(s); skipped {skipped}."
        elif invited:
            message = f"Invited {invited} BA(s)."
        elif skipped:
            message = (
                f"No new invites sent; {skipped} BA(s) were already "
                "invited or could not be invited."
            )
        else:
            message = "No ambassadors provided."

        return BulkInviteResponse(
            success=invited > 0,
            message=message,
            client_mutation_id=input.client_mutation_id,
            invited=invited,
            skipped=skipped,
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
        # Shared, email-aware resolver: Ignite admins (staff/superuser/
        # spark-admin/@igniteproductions.co) are never "client"; a rating is
        # by_client only when the real role is client.
        from utils.graphql.permissions import (
            resolve_request_user_access,
            _is_admin_access,
        )

        _rs, _st, _su, _em = await resolve_request_user_access(user)
        by_client = (not _is_admin_access(_rs, _st, _su, _em)) and _rs == "client"

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

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_ba_profile(
        self,
        info: strawberry.Info,
        input: inputs.UpdateBaProfileInput,
    ) -> UpdateBaProfileResponse:
        """BA self-edit of their own TALENT profile.

        Strictly scoped to the authenticated BA: resolves the Ambassador
        from the JWT user and writes only that row. Updates bio /
        college / in_college and attaches headshot / résumé / event-photo
        blobs (paths from the getUploadUrl→GCS flow). Every field is
        optional — any subset can be PATCHed. `acceptances` is tolerated
        and ignored (no legal ledger yet).
        """
        user = info.context.request.user

        @sync_to_async
        def _apply() -> tuple[bool, str, object | None]:
            try:
                ambassador = Ambassador.objects.select_related(
                    "user", "location", "location__state"
                ).get(user=user)
            except Ambassador.DoesNotExist:
                return (False, "Ambassador profile not found.", None)

            user_dirty = False
            if input.first_name is not None:
                ambassador.user.first_name = input.first_name.strip()
                user_dirty = True
            if input.last_name is not None:
                ambassador.user.last_name = input.last_name.strip()
                user_dirty = True
            if user_dirty:
                ambassador.user.save(update_fields=["first_name", "last_name"])

            update_fields: list[str] = []
            if input.phone is not None:
                ambassador.phone = input.phone.strip() or None
                update_fields.append("phone")
            # Prefer an explicit address; otherwise synthesize one from
            # city/state/zip if those came in and no address exists yet.
            if input.address is not None:
                ambassador.address = input.address.strip() or None
                update_fields.append("address")
            elif (
                not ambassador.address
                and (input.city or input.state or input.zip)
            ):
                ambassador.address = ", ".join(
                    p for p in [
                        (input.city or "").strip(),
                        (input.state or "").strip(),
                        (input.zip or "").strip(),
                    ] if p
                ) or None
                update_fields.append("address")
            # Best-effort lat/lng from the onboarding address autocomplete.
            # Written only when provided; mirrors the admin update/upsert
            # paths so Ambassador.coordinates feeds the nearby-gig push.
            if input.coordinates is not None:
                ambassador.coordinates = input.coordinates
                update_fields.append("coordinates")
            if input.shirt_size is not None:
                ambassador.t_shirt_size = input.shirt_size.strip() or None
                update_fields.append("t_shirt_size")
            # bio (with `about` as an alias; bio wins). Mirror to about_me
            # so legacy surfaces stay populated.
            new_bio = input.bio if input.bio is not None else input.about
            if new_bio is not None:
                ambassador.bio = new_bio.strip()
                ambassador.about_me = ambassador.bio
                update_fields.extend(["bio", "about_me"])
            if input.college is not None:
                ambassador.college = input.college.strip()
                update_fields.append("college")
            if input.in_college is not None:
                ambassador.in_college = bool(input.in_college)
                update_fields.append("in_college")
            if input.headshot is not None:
                from utils.gcs import extract_blob_name_from_url

                ambassador.headshot = (
                    extract_blob_name_from_url(input.headshot) or ""
                )
                update_fields.append("headshot")
            if input.resume is not None:
                from utils.gcs import extract_blob_name_from_url

                ambassador.resume = (
                    extract_blob_name_from_url(input.resume) or ""
                )
                update_fields.append("resume")
            if update_fields:
                ambassador.updated_by = user
                ambassador.save(update_fields=list(set(update_fields)) + ["updated_at"])

            # Event photos — replace-the-set semantics when provided.
            if input.event_photos is not None:
                from utils.gcs import extract_blob_name_from_url

                AmbassadorPhoto.objects.filter(ambassador=ambassador).delete()
                for raw in input.event_photos:
                    blob = extract_blob_name_from_url(raw)
                    if not blob:
                        continue
                    AmbassadorPhoto.objects.create(
                        ambassador=ambassador,
                        image=blob,
                        created_by=user,
                    )

            photos = list(
                AmbassadorPhoto.objects.filter(ambassador=ambassador)
            )
            return (True, "Profile updated.", (ambassador, photos))

        ok, message, payload = await _apply()
        if not ok or payload is None:
            return UpdateBaProfileResponse(
                success=ok,
                message=message,
                client_mutation_id=input.client_mutation_id,
                ambassador=None,
            )
        ambassador, photos = payload
        return UpdateBaProfileResponse(
            success=True,
            message=message,
            client_mutation_id=input.client_mutation_id,
            ambassador=BaSelfProfileType.from_ambassador(ambassador, photos),
        )

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
    async def start_mileage_session(
        self,
        info: strawberry.Info,
        input: inputs.StartMileageSessionInput,
    ) -> MileageSessionResponse:
        """BA taps Start on the mileage tracker for a gig."""
        return await MileageService.start(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def record_mileage_breadcrumbs(
        self,
        info: strawberry.Info,
        input: inputs.RecordMileageBreadcrumbsInput,
    ) -> MileageSessionResponse:
        """Append a batch of GPS points to the active mileage session."""
        return await MileageService.record_breadcrumbs(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def stop_mileage_session(
        self,
        info: strawberry.Info,
        input: inputs.StopMileageSessionInput,
    ) -> MileageSessionResponse:
        """BA taps Stop — finalize miles + reimbursement for the trip."""
        return await MileageService.stop(input, info)

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

# Either ambassador_event_uuid (when the caller has the roster row, e.g. a
# shift-offer push) OR event_uuid (the Shifts tab lists todayEvents, which
# carry the Event uuid, not the AmbassadorEvent uuid) identifies the shift.
# event_uuid is resolved to the caller's own AmbassadorEvent server-side.
@strawberry.input
class ArriveAtShiftInput:
    ambassador_event_uuid: strawberry.ID | None = None
    event_uuid: strawberry.ID | None = None
    latitude: float | None = None
    longitude: float | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class ClockInToShiftInput:
    ambassador_event_uuid: strawberry.ID | None = None
    event_uuid: strawberry.ID | None = None
    latitude: float | None = None
    longitude: float | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class ClockOutOfShiftInput:
    ambassador_event_uuid: strawberry.ID | None = None
    event_uuid: strawberry.ID | None = None
    latitude: float | None = None
    longitude: float | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class ReportShiftStatusInput:
    """BA flags they're running late or can't make a shift — fires an
    immediate push to the assigned RMM + the location's notification-group
    admins so they can backfill before it becomes a no-show."""
    ambassador_event_uuid: strawberry.ID | None = None
    event_uuid: strawberry.ID | None = None
    status: str  # "running_late" | "cant_make_it"
    eta_minutes: int | None = None
    note: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class ReportShiftStatusResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    notified_count: int = 0


@strawberry.type
class ShiftAttendanceResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    attendance_uuid: str | None = None
    clock_time: str | None = None
    kind: str | None = None  # "arrived" | "clock_in" | "clock_out"


@strawberry.input
class ConfirmShiftInput:
    """One-tap "I'm in" from the day-before confirmation push. Either
    uuid identifies the shift; the server resolves it to the CALLER's
    own AmbassadorEvent row (same contract as the attendance inputs)."""
    ambassador_event_uuid: strawberry.ID | None = None
    event_uuid: strawberry.ID | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class ConfirmShiftResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    confirmed_at: str | None = None


@strawberry.input
class RequestExtensionInput:
    """BA asks for more activation time mid-shift. event_uuid (Shifts tab)
    or ambassador_event_uuid (roster row) identifies the shift; the
    server resolves it to the caller's own AmbassadorEvent."""
    ambassador_event_uuid: strawberry.ID | None = None
    event_uuid: strawberry.ID | None = None
    minutes_requested: int = 0
    reason: str | None = None
    requested_at: str | None = None  # ISO 8601 from the device
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class ShiftExtensionType:
    id: strawberry.ID
    event_id: str | None = None
    minutes_requested: int = 0
    status: str = "pending"
    approved_minutes: int | None = None


@strawberry.type
class RequestExtensionResponse:
    success: bool
    message: str
    extension: ShiftExtensionType | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class ResolveShiftExtensionInput:
    extension_uuid: strawberry.ID
    decision: str  # "approve" | "decline"
    approved_minutes: int | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class ShiftExtensionAdminMutations:
    """Admin (Ignite) approve/decline of a BA's mid-shift extension request.
    Mirrors the public one-click email page, but for the in-dashboard flow."""

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def resolve_shift_extension(
        self, info: strawberry.Info, input: ResolveShiftExtensionInput,
    ) -> RequestExtensionResponse:
        from ambassadors.extensions import resolve_extension, user_is_ignite_admin
        from ambassadors.models import ShiftExtensionRequest

        actor = info.context.request.user
        if not user_is_ignite_admin(actor):
            return RequestExtensionResponse(
                success=False,
                message="Not authorized.",
                client_mutation_id=input.client_mutation_id,
            )
        decision = (input.decision or "").strip().lower()
        if decision not in ("approve", "decline", "deny"):
            return RequestExtensionResponse(
                success=False,
                message="decision must be approve or decline.",
                client_mutation_id=input.client_mutation_id,
            )
        approve = decision == "approve"

        def _go():
            ext = (
                ShiftExtensionRequest.objects.select_related(
                    "event", "ambassador", "ambassador__user"
                )
                .filter(uuid=str(input.extension_uuid))
                .first()
            )
            if ext is None:
                return None, None
            result = resolve_extension(
                ext,
                approve=approve,
                approved_minutes=input.approved_minutes,
                actor_user=actor,
            )
            return ext, result

        ext, result = await sync_to_async(_go)()
        if ext is None:
            return RequestExtensionResponse(
                success=False,
                message="Extension request not found.",
                client_mutation_id=input.client_mutation_id,
            )
        return RequestExtensionResponse(
            success=True,
            message=(result or {}).get("message", "Done."),
            extension=ShiftExtensionType(
                id=str(ext.uuid),
                event_id=str(getattr(ext.event, "uuid", "")),
                minutes_requested=ext.minutes_requested,
                status=ext.status,
                approved_minutes=ext.approved_minutes,
            ),
            client_mutation_id=input.client_mutation_id,
        )


def _resolve_amb_event_by_uuid(uuid: str):
    from ambassadors.models import AmbassadorEvent
    try:
        return AmbassadorEvent.objects.select_related(
            "ambassador", "ambassador__user", "event"
        ).get(uuid=uuid)
    except AmbassadorEvent.DoesNotExist:
        return None


def _resolve_amb_event_for_actor_by_event(event_uuid: str, actor):
    """Find the calling BA's AmbassadorEvent for a given Event uuid.

    Powers clock-in from the Shifts tab, which lists `todayEvents`
    (Event uuids) rather than AmbassadorEvent uuids. Scoped to the
    actor so a BA can only resolve their own roster row.
    """
    from ambassadors.models import AmbassadorEvent
    actor_id = getattr(actor, "id", None)
    if not actor_id:
        return None
    return (
        AmbassadorEvent.objects.select_related(
            "ambassador", "ambassador__user", "event"
        )
        .filter(event__uuid=event_uuid, ambassador__user_id=actor_id)
        .order_by("-id")
        .first()
    )


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

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def confirm_shift(
        self, info: strawberry.Info, input: ConfirmShiftInput,
    ) -> ConfirmShiftResponse:
        """One-tap "I'm in" for the day-before confirmation push.

        Stamps the caller's AmbassadorEvent.confirmed_at. Idempotent —
        confirming twice keeps the first stamp and still returns success.
        """
        actor = info.context.request.user

        def _go():
            amb_event = None
            if getattr(input, "ambassador_event_uuid", None):
                amb_event = _resolve_amb_event_by_uuid(
                    str(input.ambassador_event_uuid)
                )
            elif getattr(input, "event_uuid", None):
                amb_event = _resolve_amb_event_for_actor_by_event(
                    str(input.event_uuid), actor
                )
            if not amb_event:
                return None, "Shift not found."
            own_user_id = (
                amb_event.ambassador.user_id if amb_event.ambassador else None
            )
            if own_user_id and getattr(actor, "id", None) != own_user_id:
                return None, "Not your shift."
            if amb_event.confirmed_at is None:
                from django.utils import timezone as _dj_tz

                amb_event.confirmed_at = _dj_tz.now()
                amb_event.save(update_fields=["confirmed_at", "updated_at"])
            return amb_event, "Confirmed — see you there!"

        amb_event, msg = await sync_to_async(_go)()
        if amb_event is None:
            return ConfirmShiftResponse(
                success=False, message=msg, client_mutation_id=None
            )
        return ConfirmShiftResponse(
            success=True,
            message=msg,
            client_mutation_id=None,
            confirmed_at=(
                amb_event.confirmed_at.isoformat()
                if amb_event.confirmed_at
                else None
            ),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def report_shift_status(
        self, info: strawberry.Info, input: ReportShiftStatusInput,
    ) -> ReportShiftStatusResponse:
        """BA reports running-late / can't-make-it → notify the Ignite admin
        team only (push + email). The client / assigned RMM is NOT notified."""
        actor = info.context.request.user
        status = (input.status or "").strip().lower()
        if status not in ("running_late", "cant_make_it"):
            return ReportShiftStatusResponse(
                success=False,
                message="status must be running_late or cant_make_it.",
                client_mutation_id=input.client_mutation_id,
            )

        def _go():
            amb_event = None
            if getattr(input, "ambassador_event_uuid", None):
                amb_event = _resolve_amb_event_by_uuid(str(input.ambassador_event_uuid))
            elif getattr(input, "event_uuid", None):
                amb_event = _resolve_amb_event_for_actor_by_event(
                    str(input.event_uuid), actor
                )
            if not amb_event:
                return None, "Shift not found.", 0
            own_user_id = (
                amb_event.ambassador.user_id if amb_event.ambassador else None
            )
            if own_user_id and getattr(actor, "id", None) != own_user_id:
                return None, "Not your shift.", 0

            event = amb_event.event
            ba = amb_event.ambassador
            ba_user = getattr(ba, "user", None)
            ba_name = (
                f"{getattr(ba_user, 'first_name', '') or ''} "
                f"{getattr(ba_user, 'last_name', '') or ''}"
            ).strip() or "A BA"
            venue = getattr(event, "name", None) or "their shift"

            if status == "cant_make_it":
                title = "⚠️ BA can't make a shift"
                body = f"{ba_name} can't make {venue}."
            else:
                eta = f" (~{input.eta_minutes} min late)" if input.eta_minutes else ""
                title = "⏱ BA running late"
                body = f"{ba_name} is running late to {venue}{eta}."
            if input.note:
                body += f" — “{input.note[:120]}”"

            # Ignite admin team only — never the client / assigned RMM.
            watcher_ids = _spark_admin_user_ids()
            sent = 0
            for uid in watcher_ids:
                try:
                    _send_push_to_user_sync(
                        uid, title=title, body=body,
                        data={
                            "screen": "tracker",
                            "eventUuid": str(getattr(event, "uuid", "")),
                            "kind": "shift_status",
                            "shiftStatus": status,
                        },
                    )
                    sent += 1
                except Exception:
                    logging.getLogger(__name__).exception(
                        "shift-status push failed user=%s", uid,
                    )

            # Email the Spark admin team too (parity with the extension
            # request) — best-effort, never blocks the status report.
            try:
                _email_admins_shift_status(
                    ba_name=ba_name,
                    venue=venue,
                    status=status,
                    eta_minutes=input.eta_minutes,
                    note=input.note or "",
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "shift-status admin email failed",
                )
            return amb_event, "Your team has been notified.", sent

        amb_event, msg, sent = await sync_to_async(_go)()
        return ReportShiftStatusResponse(
            success=amb_event is not None,
            message=msg,
            client_mutation_id=input.client_mutation_id,
            notified_count=sent,
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def request_extension(
        self, info: strawberry.Info, input: RequestExtensionInput,
    ) -> RequestExtensionResponse:
        """BA requests more activation time mid-shift. Records the request and
        notifies the Ignite admin team only (push + email) — the client /
        assigned RMM is NOT notified."""
        actor = info.context.request.user
        minutes = int(input.minutes_requested or 0)
        if minutes <= 0:
            return RequestExtensionResponse(
                success=False,
                message="Pick how much more time you need.",
                client_mutation_id=input.client_mutation_id,
            )

        def _go():
            from ambassadors.models import ShiftExtensionRequest
            from django.utils import timezone as _tz
            from django.utils.dateparse import parse_datetime

            amb_event = None
            if getattr(input, "ambassador_event_uuid", None):
                amb_event = _resolve_amb_event_by_uuid(
                    str(input.ambassador_event_uuid)
                )
            elif getattr(input, "event_uuid", None):
                amb_event = _resolve_amb_event_for_actor_by_event(
                    str(input.event_uuid), actor
                )
            if not amb_event:
                return None, "Shift not found.", None
            own_user_id = (
                amb_event.ambassador.user_id if amb_event.ambassador else None
            )
            if own_user_id and getattr(actor, "id", None) != own_user_id:
                return None, "Not your shift.", None

            event = amb_event.event
            ba = amb_event.ambassador
            req_at = parse_datetime(input.requested_at) if input.requested_at else None
            ext = ShiftExtensionRequest.objects.create(
                event=event,
                ambassador=ba,
                minutes_requested=minutes,
                reason=(input.reason or "")[:2000],
                status=ShiftExtensionRequest.STATUS_PENDING,
                requested_at=req_at or _tz.now(),
                created_by=actor if getattr(actor, "id", None) else None,
            )

            ba_user = getattr(ba, "user", None)
            ba_name = (
                f"{getattr(ba_user, 'first_name', '') or ''} "
                f"{getattr(ba_user, 'last_name', '') or ''}"
            ).strip() or "A BA"
            venue = getattr(event, "name", None) or "their shift"

            # 1) Push the Ignite admin team (dashboard flag) — NOT the RMM/client.
            title = "⏱ Extension requested"
            body = f"{ba_name} is requesting +{minutes} min at {venue}."
            if input.reason:
                body += f" — “{input.reason[:120]}”"
            for uid in _spark_admin_user_ids():
                try:
                    _send_push_to_user_sync(
                        uid, title=title, body=body,
                        data={
                            "screen": "tracker",
                            "eventUuid": str(getattr(event, "uuid", "")),
                            # Lets the admin notification feed render inline
                            # Approve / Decline for this extension request.
                            "kind": "extension_request",
                            "extensionUuid": str(ext.uuid),
                        },
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "extension push failed user=%s", uid,
                    )

            # 2) Email every Spark admin (best-effort — never blocks). The
            # email carries one-click Approve / Decline links (extension_id).
            try:
                _email_admins_extension_request(
                    ba_name=ba_name,
                    venue=venue,
                    minutes=minutes,
                    reason=input.reason or "",
                    event_uuid=str(getattr(event, "uuid", "")),
                    extension_id=ext.id,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "extension admin email failed",
                )

            return ext, "Your team has been notified.", event

        ext, msg, event = await sync_to_async(_go)()
        if ext is None:
            return RequestExtensionResponse(
                success=False,
                message=msg,
                client_mutation_id=input.client_mutation_id,
            )
        return RequestExtensionResponse(
            success=True,
            message=msg,
            extension=ShiftExtensionType(
                id=str(ext.uuid),
                event_id=str(getattr(event, "uuid", "")),
                minutes_requested=ext.minutes_requested,
                status=ext.status,
                approved_minutes=ext.approved_minutes,
            ),
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def release_my_shift(
        self,
        info: strawberry.Info,
        input: inputs.CancelShiftInviteInput,
    ) -> CancelShiftInviteResponse:
        """A BA drops a shift they're booked on but can't make.

        Self-scoped: only the caller's own AmbassadorEvent can be released
        (cross-BA attempts get the same "not found" as a missing row, so we
        don't leak existence). Frees the slot by deleting the row — the exact
        effect as an admin removal — and pings the event's RMM + notification-
        group admins to re-staff (also lands in their in-app inbox). PENDING
        offers use the decline path; this is only for APPROVED bookings, and
        only before the shift starts.
        """
        from django.utils import timezone as _tz

        user = info.context.request.user
        cmid = input.client_mutation_id

        def _release():
            try:
                ae = AmbassadorEvent.objects.select_related(
                    "ambassador",
                    "ambassador__user",
                    "event",
                    "event__request",
                    "event__location",
                ).get(uuid=str(input.ambassador_event_uuid))
            except AmbassadorEvent.DoesNotExist:
                return (False, "Shift not found.", None, None, None)

            if getattr(ae.ambassador, "user_id", None) != getattr(user, "id", None):
                return (False, "Shift not found.", None, None, None)
            if not ae.is_approved:
                return (False, "This shift isn't booked yet.", None, None, None)

            ev = ae.event
            start = getattr(ev, "start_time", None)
            if start is not None and start <= _tz.now():
                return (
                    False,
                    "This shift has already started — message your RMM.",
                    None,
                    None,
                    None,
                )

            ba_name = (
                (getattr(ae.ambassador.user, "first_name", "") or "").strip()
                or getattr(ae.ambassador.user, "email", "")
                or "A BA"
            )
            ev_name = getattr(ev, "name", None) or "a shift"
            ev_date = getattr(ev, "date", None) or start
            # Ignite admin team only — never the client / assigned RMM.
            watchers = _spark_admin_user_ids()

            ae.delete()
            # Reopen the freed slot for self-serve claim by another eligible BA
            # (the "Open shifts" board). Best-effort — the drop itself must
            # succeed even if we can't record the open slot.
            try:
                from .models import OpenShift

                OpenShift.objects.create(event=ev, released_by=user)
            except Exception:
                pass
            return (True, "You're off this shift — we've let the team know.",
                    ba_name, (ev_name, ev_date), watchers)

        ok, msg, ba_name, ev_info, watchers = await sync_to_async(_release)()
        if not ok:
            return CancelShiftInviteResponse(
                success=False, message=msg, client_mutation_id=cmid
            )

        # Best-effort re-staff ping to the RMM + notification-group admins.
        try:
            from ambassadors.push import send_push_to_user

            ev_name, ev_date = ev_info
            date_str = ""
            if ev_date is not None:
                try:
                    date_str = f" on {ev_date.strftime('%a %b %-d')}"
                except Exception:
                    date_str = ""
            for uid in watchers or []:
                await send_push_to_user(
                    uid,
                    title="Shift dropped — needs re-staffing",
                    body=f"{ba_name} can't make {ev_name}{date_str}.",
                    data={"kind": "shift_dropped", "screen": "today"},
                )
        except Exception:
            pass

        return CancelShiftInviteResponse(
            success=True, message=msg, client_mutation_id=cmid
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def claim_open_shift(
        self,
        info: strawberry.Info,
        input: inputs.ClaimOpenShiftInput,
    ) -> CancelShiftInviteResponse:
        """A BA claims a dropped shift from the self-serve "Open shifts" board.

        Instantly books them (an approved AmbassadorEvent) — no admin approval
        — when the slot is still open, the event is in the future, and the BA is
        eligible (has worked with the brand, isn't already on the event).
        Race-safe: the OpenShift is locked with select_for_update, so if two
        BAs race for the same slot only the first wins; the loser gets a clear
        "just claimed" message. The event's RMM + notification-group admins get
        a heads-up push so re-staffing oversight stays intact.
        """
        from django.db import transaction
        from django.utils import timezone as _tz
        from .models import Ambassador, OpenShift

        user = info.context.request.user
        cmid = input.client_mutation_id

        def _claim():
            with transaction.atomic():
                try:
                    row = (
                        OpenShift.objects.select_for_update()
                        .select_related("event")
                        .get(uuid=str(input.open_shift_uuid))
                    )
                except OpenShift.DoesNotExist:
                    return (False, "This shift is no longer available.", None, None)

                if row.claimed_at is not None:
                    return (
                        False,
                        "This shift was just claimed by someone else.",
                        None,
                        None,
                    )

                ev = row.event
                start = getattr(ev, "start_time", None)
                if start is not None and start <= _tz.now():
                    return (False, "This shift has already started.", None, None)

                ambassador = Ambassador.objects.filter(user=user).first()
                if ambassador is None:
                    return (False, "This shift is no longer available.", None, None)

                # Eligibility: must have history with this brand (tenant) — the
                # same audience my_open_shifts surfaces it to.
                worked = set(
                    AmbassadorEvent.objects.filter(ambassador=ambassador)
                    .values_list("event__tenant_id", flat=True)
                )
                if ev.tenant_id not in worked:
                    return (False, "This shift isn't available to you.", None, None)

                # If they're somehow already on the event, just resolve the open
                # slot rather than creating a duplicate booking.
                already = AmbassadorEvent.objects.filter(
                    ambassador=ambassador, event=ev
                ).exists()
                if not already:
                    AmbassadorEvent.objects.create(
                        ambassador=ambassador,
                        event=ev,
                        tenant_id=ev.tenant_id,
                        is_approved=True,
                        created_by=user,
                    )

                row.claimed_by = user
                row.claimed_at = _tz.now()
                row.save(update_fields=["claimed_by", "claimed_at"])

                ba_name = (
                    (getattr(user, "first_name", "") or "").strip()
                    or getattr(user, "email", "")
                    or "A BA"
                )
                ev_name = getattr(ev, "name", None) or "a shift"
                ev_date = getattr(ev, "date", None) or start
                # Ignite admin team only — never the client / assigned RMM.
                watchers = _spark_admin_user_ids()
                return (
                    True,
                    "You're booked — see you there!",
                    (ba_name, ev_name, ev_date),
                    watchers,
                )

        ok, msg, info_tuple, watchers = await sync_to_async(_claim)()
        if not ok:
            return CancelShiftInviteResponse(
                success=False, message=msg, client_mutation_id=cmid
            )

        # Heads-up to the RMM + notification-group admins that the slot filled.
        try:
            from ambassadors.push import send_push_to_user

            ba_name, ev_name, ev_date = info_tuple
            date_str = ""
            if ev_date is not None:
                try:
                    date_str = f" on {ev_date.strftime('%a %b %-d')}"
                except Exception:
                    date_str = ""
            for uid in watchers or []:
                await send_push_to_user(
                    uid,
                    title="Open shift claimed",
                    body=f"{ba_name} grabbed {ev_name}{date_str}.",
                    data={"kind": "shift_claimed", "screen": "today"},
                )
        except Exception:
            pass

        return CancelShiftInviteResponse(
            success=True, message=msg, client_mutation_id=cmid
        )


def _event_watcher_user_ids(event) -> list[int]:
    """User ids to notify about a shift event: the request's assigned RMM
    plus the location's notification-group admins (scoped to the tenant)."""
    from events.models import NotificationGroupLocation, NotificationGroupUser

    ids: set[int] = set()
    req = getattr(event, "request", None)
    if req and getattr(req, "rmm_asigned_id", None):
        ids.add(req.rmm_asigned_id)
    loc = getattr(event, "location", None) or (
        getattr(req, "location", None) if req else None
    )
    if loc is not None:
        group_ids = list(
            NotificationGroupLocation.objects.filter(
                location_id=loc.id, notification_group__state=False,
            ).values_list("notification_group_id", flat=True)
        )
        if getattr(loc, "state_id", None):
            group_ids += list(
                NotificationGroupLocation.objects.filter(
                    state_id=loc.state_id, notification_group__state=True,
                ).values_list("notification_group_id", flat=True)
            )
        if group_ids:
            ids.update(
                NotificationGroupUser.objects.filter(
                    notification_group_id__in=group_ids,
                    user__is_active=True,
                    user__tenanted_users__tenant_id=event.tenant_id,
                    user__tenanted_users__is_active=True,
                ).values_list("user_id", flat=True)
            )
    return [i for i in ids if i]


def _spark_admin_user_ids() -> list[int]:
    """Active Ignite (Spark-admin) user ids — the push counterpart to
    _get_spark_admin_emails(). Shift-status (running-late / can't-make-it)
    and extension-request notifications go to the IGNITE ADMIN TEAM ONLY,
    never the client / assigned RMM. Honors the same CC suppression list."""
    from django.contrib.auth import get_user_model
    from tenants.models import Role
    from events.routing import CC_SUPPRESS_EMAILS

    User = get_user_model()
    try:
        rows = list(
            User.objects.filter(
                role__slug=Role.SPARK_ADMIN_SLUG, is_active=True,
            ).values_list("id", "email")
        )
    except Exception:
        return []
    return [
        uid
        for (uid, email) in rows
        if uid and (email or "").strip().lower() not in CC_SUPPRESS_EMAILS
    ]


def _email_admins_extension_request(
    *, ba_name: str, venue: str, minutes: int, reason: str, event_uuid: str,
    extension_id: int | None = None,
) -> None:
    """Email every active Spark admin that a BA requested an extension.

    Synchronous (send_now) so it fires in-request rather than depending on
    an RQ worker. All interpolated values are HTML-escaped. Best-effort:
    the caller wraps this in try/except so a mail failure never blocks the
    extension request itself. When ``extension_id`` is set the email carries
    one-click Approve / Decline buttons (a signed token → the public approval
    page), so an admin can decide straight from their inbox."""
    from html import escape as _esc
    from events.mutations import _get_spark_admin_emails
    from utils.mailer import Envelope, Mailer

    admins = _get_spark_admin_emails()
    if not admins:
        return
    ba_e, venue_e = _esc(ba_name or "A BA"), _esc(venue or "their shift")
    reason_html = (
        f"<p style='margin:8px 0 0;color:#444'>“{_esc(reason[:500])}”</p>"
        if reason
        else ""
    )

    # One-click Approve / Decline buttons → the public token-gated page.
    action_html = ""
    if extension_id:
        try:
            from ambassadors.extensions import (
                make_extension_token,
                public_extension_url,
            )

            url = public_extension_url(make_extension_token(int(extension_id)))
            action_html = (
                "<div style='margin:18px 0 4px'>"
                f"<a href='{url}' style='display:inline-block;background:#c5f546;"
                "color:#0a0d09;text-decoration:none;font-weight:700;"
                "padding:12px 20px;border-radius:10px'>"
                f"Approve +{int(minutes)} min</a>"
                f"<a href='{url}' style='display:inline-block;margin-left:10px;"
                "color:#b23b14;text-decoration:none;font-weight:700;"
                "padding:12px 20px;border-radius:10px;border:1px solid #e2c8be'>"
                "Review / Decline</a></div>"
            )
        except Exception:  # noqa: BLE001 — never block the email on link build
            action_html = ""

    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'color:#111;line-height:1.5">'
        f"<p style='margin:0 0 8px'><strong>{ba_e}</strong> is requesting a "
        "shift extension.</p>"
        f"<p style='margin:0'><strong>Venue:</strong> {venue_e}<br>"
        f"<strong>Extra time requested:</strong> {int(minutes)} minutes</p>"
        f"{reason_html}"
        f"{action_html}"
        "<p style='margin:16px 0 0;color:#666;font-size:13px'>You can also "
        "review in the Spark dashboard — the BA keeps working until you "
        "decide.</p></div>"
    )

    class _ExtMailer(Mailer):
        def envelope(self) -> "Envelope":
            return Envelope(
                subject=f"⏱ Extension requested — {ba_name} @ {venue}",
                html=html,
                to_emails=admins,
            )

    _ExtMailer().send_now()


def _email_admins_shift_status(
    *, ba_name: str, venue: str, status: str,
    eta_minutes: int | None, note: str,
) -> None:
    """Email every active Spark admin that a BA reported running-late /
    can't-make-it. Mirrors _email_admins_extension_request: synchronous
    send_now (no RQ worker dependency), HTML-escaped, best-effort — the
    caller wraps it in try/except so a mail failure never blocks the report."""
    from html import escape as _esc
    from events.mutations import _get_spark_admin_emails
    from utils.mailer import Envelope, Mailer

    admins = _get_spark_admin_emails()
    if not admins:
        return
    ba_e, venue_e = _esc(ba_name or "A BA"), _esc(venue or "their shift")
    cant = status == "cant_make_it"
    headline = (
        f"<strong>{ba_e}</strong> can't make their shift."
        if cant
        else f"<strong>{ba_e}</strong> is running late."
    )
    eta_html = (
        f"<br><strong>ETA:</strong> ~{int(eta_minutes)} min late"
        if (not cant and eta_minutes)
        else ""
    )
    note_html = (
        f"<p style='margin:8px 0 0;color:#444'>“{_esc(note[:500])}”</p>"
        if note
        else ""
    )
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'color:#111;line-height:1.5">'
        f"<p style='margin:0 0 8px'>{headline}</p>"
        f"<p style='margin:0'><strong>Venue:</strong> {venue_e}{eta_html}</p>"
        f"{note_html}"
        "<p style='margin:16px 0 0;color:#666;font-size:13px'>Sent from the "
        "Spark BA app so the team can backfill or adjust coverage.</p></div>"
    )
    subject = (
        f"🚫 Can't make it — {ba_name} @ {venue}"
        if cant
        else f"⏱ Running late — {ba_name} @ {venue}"
    )

    class _StatusMailer(Mailer):
        def envelope(self) -> "Envelope":
            return Envelope(subject=subject, html=html, to_emails=admins)

    _StatusMailer().send_now()


def _auto_confirm_on_attendance(amb_event, kind: str) -> None:
    """Showing up IS confirming — arriving or clocking in flips the
    day-before confirmation stamp so the admin roster goes green even if
    the BA never tapped the confirmation push. No-op on clock-out (the
    shift already happened) and when already confirmed."""
    if kind not in ("arrived", "clock_in"):
        return
    if amb_event.confirmed_at is not None:
        return
    from django.utils import timezone as _dj_tz

    amb_event.confirmed_at = _dj_tz.now()
    amb_event.save(update_fields=["confirmed_at", "updated_at"])


async def _do_attendance(info, input, *, kind: str) -> "ShiftAttendanceResponse":
    actor = info.context.request.user

    def _go():
        amb_event = None
        if getattr(input, "ambassador_event_uuid", None):
            amb_event = _resolve_amb_event_by_uuid(str(input.ambassador_event_uuid))
        elif getattr(input, "event_uuid", None):
            amb_event = _resolve_amb_event_for_actor_by_event(
                str(input.event_uuid), actor
            )
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
        _auto_confirm_on_attendance(amb_event, kind)
        return att, "OK"

    att, msg = await sync_to_async(_go)()
    if att is None:
        return ShiftAttendanceResponse(
            success=False, message=msg,
            client_mutation_id=None, kind=None,
        )

    # BA referral program — a clock-out is the "completed a shift" signal.
    # If this BA was referred and not yet stamped, stamp their first completed
    # shift and ping the referrer. Best-effort: the helper swallows DB errors
    # and push errors are caught here, so clock-out is never affected.
    if kind == "clock_out":
        try:
            from ambassadors.push import send_push_to_user
            from ambassadors.referrals import complete_first_shift_if_referred

            referral = await sync_to_async(complete_first_shift_if_referred)(
                actor
            )
            if referral is not None:
                friend = (
                    (getattr(actor, "first_name", "") or "").strip()
                    or "Your friend"
                )
                await send_push_to_user(
                    referral.referrer,
                    title="Referral complete 🎉",
                    body=(
                        f"{friend} just completed their first shift — "
                        "your referral bonus is unlocked."
                    ),
                    data={"type": "referral_first_shift"},
                )
        except Exception:  # noqa: BLE001 — never break clock-out
            import logging

            logging.getLogger(__name__).exception(
                "Referral first-shift hook failed (clock-out unaffected)."
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


@strawberry.type
class NotificationMutations:
    """Mark Notifications-inbox rows read. Self-scoped: only the caller's own
    rows are ever touched (filter by the JWT user), so passing another user's
    uuid is a silent no-op."""

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def mark_notifications_read(
        self,
        info: strawberry.Info,
        uuids: list[strawberry.ID] | None = None,
    ) -> int:
        """Mark the given notification uuids read, or ALL unread when no uuids
        are passed (the "mark all read" affordance). Returns the count newly
        marked read."""
        from django.utils import timezone as _tz

        from .models import PushNotification

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return 0

        def _mark() -> int:
            qs = PushNotification.objects.filter(user=user, read_at__isnull=True)
            if uuids:
                qs = qs.filter(uuid__in=[str(u) for u in uuids])
            return qs.update(read_at=_tz.now())

        return await sync_to_async(_mark)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_push_preferences(
        self,
        info: strawberry.Info,
        shift_offers: bool | None = None,
        reminders: bool | None = None,
        chat: bool | None = None,
        pay: bool | None = None,
        gigs: bool | None = None,
    ) -> PushPreferences:
        """Update the caller's push opt-ins. Only categories you pass are
        changed; omitted ones keep their current value. Creates the row on
        first use (defaults = everything on). Returns the full resulting set."""
        from .models import PushPreference

        user = info.context.request.user
        _defaults = PushPreferences(
            shift_offers=True, reminders=True, chat=True, pay=True, gigs=True
        )
        if not getattr(user, "is_authenticated", False):
            return _defaults

        updates = {
            "shift_offers": shift_offers,
            "reminders": reminders,
            "chat": chat,
            "pay": pay,
            "gigs": gigs,
        }

        def _save() -> PushPreferences:
            pref, _ = PushPreference.objects.get_or_create(user=user)
            dirty = [f for f, v in updates.items() if v is not None and getattr(pref, f) != v]
            for f in dirty:
                setattr(pref, f, updates[f])
            if dirty:
                pref.save(update_fields=dirty + ["updated_at"])
            return PushPreferences(
                shift_offers=pref.shift_offers,
                reminders=pref.reminders,
                chat=pref.chat,
                pay=pref.pay,
                gigs=pref.gigs,
            )

        return await sync_to_async(_save)()
