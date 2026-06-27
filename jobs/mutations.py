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
    JobApplicationReceivedMailer,
    JobBookingConfirmationMailer,
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


async def _confirm_booking_for_ambassador_job(
    ambassador_job: models.AmbassadorJob,
    actor,
) -> None:
    """Ensure an is_approved=True AmbassadorEvent exists for an approved
    AmbassadorJob's (ambassador, event). Get-or-creates, and flips an
    existing is_approved=False invite/accept row to True so the shift
    surfaces on the mobile shift screens. Best-effort: a booking failure
    must not undo the approval that already committed."""
    job = getattr(ambassador_job, "job", None)
    event_id = getattr(job, "event_id", None)
    ambassador_id = getattr(ambassador_job, "ambassador_id", None)
    if not event_id or not ambassador_id:
        return

    actor_id = getattr(actor, "id", None)
    creator_id = actor_id or getattr(ambassador_job, "created_by_id", None)

    def _confirm() -> None:
        booking, created = AmbassadorEvent.objects.get_or_create(
            ambassador_id=ambassador_id,
            event_id=event_id,
            defaults={
                "tenant_id": ambassador_job.tenant_id,
                "is_approved": True,
                "created_by_id": creator_id,
                "updated_by_id": creator_id,
            },
        )
        if not created and not booking.is_approved:
            booking.is_approved = True
            booking.updated_by_id = creator_id
            booking.save(
                update_fields=["is_approved", "updated_by", "updated_at"]
            )

    try:
        await sync_to_async(_confirm)()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to confirm booking for ambassador_job=%s: %s",
            getattr(ambassador_job, "id", None),
            exc,
        )


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


def _booking_push_body(job: "models.Job") -> str:
    """Push body for a marketplace booking. Enriches with venue + date
    when available ("You're booked: {venue} {date}"), else falls back to
    the job name so the BA always gets a meaningful nudge."""
    event = getattr(job, "event", None)
    venue = (
        getattr(event, "name", None)
        or getattr(getattr(event, "retailer", None), "name", None)
        or job.name
    )
    when = getattr(event, "start_time", None) or getattr(event, "date", None)
    date_str = ""
    if when is not None:
        try:
            date_str = when.strftime("%b %-d")
        except (ValueError, AttributeError):
            date_str = ""
    label = f"{venue} {date_str}".strip() if venue else date_str
    if label:
        return f"You're booked: {label}. Tap to see details."
    return f"You've been booked for {job.name}. Tap to see details."


def _notify_admins_of_application(application_id: int) -> None:
    """Best-effort staffing alert: a BA just applied to a posted gig.

    Recipients are the Ignite admin team ONLY — every recipient must be an
    ``@igniteproductions.co`` address. We consider the event's assigned RMM
    and the admin who posted the job, but only when they're on the Ignite
    domain, plus the Ignite events inbox as the always-on catch-all. A
    non-Ignite RMM / poster (e.g. a brand client who posted the gig) is never
    copied — applicant alerts are an internal staffing concern. Fire-and-
    forget: the whole body is guarded so a mail/lookup failure never breaks
    the BA's apply. Synchronous (the caller wraps it in sync_to_async).
    """
    try:
        from django.conf import settings

        app = (
            models.JobApplication.objects.select_related(
                "job",
                "job__event",
                "job__event__rmm_asigned",
                "job__created_by",
                "job__tenant",
                "ambassador",
                "ambassador__user",
            )
            .filter(id=application_id)
            .first()
        )
        if app is None or app.job is None:
            return
        job = app.job
        event = getattr(job, "event", None)
        tenant = getattr(job, "tenant", None)

        # ---- Recipients: Ignite admin team only (@igniteproductions.co),
        # case-insensitive dedupe. A non-Ignite RMM / poster is dropped, as is
        # any address on the shared CC suppression list (removed team members). ----
        from events.routing import CC_SUPPRESS_EMAILS

        IGNITE_DOMAIN = "@igniteproductions.co"
        recipients: list[str] = []
        seen: set[str] = set()

        def add(email: str | None) -> None:
            e = (email or "").strip()
            if (
                e
                and e.lower().endswith(IGNITE_DOMAIN)
                and e.lower() not in seen
                and e.lower() not in CC_SUPPRESS_EMAILS
            ):
                seen.add(e.lower())
                recipients.append(e)

        rmm_user = getattr(event, "rmm_asigned", None) if event else None
        add(getattr(rmm_user, "email", None))
        add(getattr(getattr(job, "created_by", None), "email", None))
        # Always include the Ignite staffing inbox + the standing staffing
        # recipients so the team sees every applicant even when no RMM is
        # assigned / the poster was a system user.
        add("events@igniteproductions.co")
        add("keis@igniteproductions.co")
        add("myriant@igniteproductions.co")
        if not recipients:
            return

        amb_user = getattr(getattr(app, "ambassador", None), "user", None)
        applicant_name = ""
        if amb_user is not None:
            applicant_name = (amb_user.get_full_name() or "").strip() or (
                getattr(amb_user, "email", None) or ""
            ).strip()

        when = (
            (getattr(event, "start_time", None) or getattr(event, "date", None))
            if event
            else None
        )
        when_label = None
        if when is not None:
            try:
                when_label = when.strftime("%b %-d, %Y")
            except (ValueError, AttributeError):
                when_label = None

        location_label = (
            (getattr(event, "address", None) if event else None)
            or getattr(job, "address", None)
            or None
        )

        base = (getattr(settings, "ADMIN_FRONTEND_URL", "") or "").rstrip("/")
        job_uuid = getattr(job, "uuid", None)
        applicants_url = None
        if base and job_uuid:
            applicants_url = f"{base}/job/view/{job_uuid}"
        elif base:
            applicants_url = f"{base}/jobs"

        note = (getattr(app, "note", None) or "").strip()
        if len(note) > 300:
            note = note[:297] + "…"

        JobApplicationReceivedMailer(
            to_emails=recipients,
            applicant_name=applicant_name,
            job_name=getattr(job, "name", None) or "a gig",
            event_name=getattr(event, "name", None) if event else None,
            when_label=when_label,
            location_label=location_label,
            tenant_name=getattr(tenant, "name", None),
            note=note or None,
            applicants_url=applicants_url,
            reply_to_email=(getattr(rmm_user, "email", None) or "").strip() or None,
        ).send()
    except Exception:
        logger.exception(
            "apply-to-job admin notification failed for application_id=%s",
            application_id,
        )


async def _notify_booked_ambassador_by_email(job: "models.Job", ambassador_pk: int) -> None:
    """Send the booking-confirmation email to the just-booked BA.

    Built from the Job (the marketplace accept flow has no AmbassadorJob),
    guarded by the caller's try/except so mail failure never breaks the
    booking. No-ops when the BA has no email on file."""
    def _resolve_email_and_name() -> tuple[str, str | None]:
        amb = (
            _Ambassador.objects.select_related("user")
            .filter(pk=ambassador_pk)
            .first()
        )
        user = getattr(amb, "user", None) if amb else None
        email = (getattr(user, "email", None) or "").strip()
        first_name = (getattr(user, "first_name", None) or "").strip() or None
        return email, first_name

    email, first_name = await sync_to_async(_resolve_email_and_name)()
    if not email:
        return

    mailer = JobBookingConfirmationMailer(
        job=job,
        to_emails=[email],
        recipient_first_name=first_name,
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

        # Keep the linked Event's dates in lock-step with the job's. The mobile
        # shift screens (my_active_shifts / my_upcoming_shifts) key off
        # event.start_time / event.date — NOT the job's start_date — so without
        # this an edited gig's approved booking silently fails to appear on the
        # BA's Today/Upcoming (the job says one date, the event another). Only
        # fires when the dates actually changed.
        if (
            relevant_fields_before["start_date"] != relevant_fields_after["start_date"]
            or relevant_fields_before["end_date"] != relevant_fields_after["end_date"]
        ):
            def _sync_event_dates() -> None:
                if not updated_job.event_id:
                    return
                from events.models import Event
                event = Event.objects.filter(id=updated_job.event_id).first()
                if event is None:
                    return
                fields: list[str] = []
                if updated_job.start_date is not None:
                    if hasattr(event, "start_time"):
                        event.start_time = updated_job.start_date
                        fields.append("start_time")
                    if hasattr(event, "date"):
                        event.date = updated_job.start_date.date()
                        fields.append("date")
                if updated_job.end_date is not None and hasattr(event, "end_time"):
                    event.end_time = updated_job.end_date
                    fields.append("end_time")
                if fields:
                    event.save(update_fields=fields)

            try:
                await sync_to_async(_sync_event_dates)()
            except Exception as exc:  # noqa: BLE001 — never fail the job update
                logger.warning(
                    "Failed to sync event dates for job=%s: %s",
                    updated_job.id, exc,
                )

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
        # Direct admin assignment IS the hire (AmbassadorJob.status=approved
        # above), so the event booking must be approved too. With is_approved
        # False the BA's upcoming-shifts + clock-in — which both require
        # AmbassadorEvent.is_approved=True — never see the gig, so the BA can't
        # view or clock into the shift they were just hired for. Mirrors
        # assign_ambassador_to_job / approve_ambassador_job.
        if not await AmbassadorEvent.objects.filter(
            ambassador_id=ambassador_job.ambassador_id,
            event_id=job.event_id,
        ).aexists():
            await AmbassadorEvent.objects.acreate(
                ambassador_id=ambassador_job.ambassador_id,
                event_id=job.event_id,
                tenant_id=ambassador_job.tenant_id,
                is_approved=True,
                created_by_id=ambassador_job.created_by_id,
                updated_by_id=ambassador_job.updated_by_id,
            )

        await _notify_assigned_ambassador_by_email(ambassador_job)

        # "You got the gig" push — best-effort, mirrors assign_ambassador_to_job
        # so a directly-assigned BA is actually notified (email alone left them
        # unaware). Never fail the assignment on a push error.
        try:
            from ambassadors.push import enqueue_push

            ba_user_id = getattr(ambassador_job.ambassador, "user_id", None)
            assigned_job = ambassador_job.job
            if ba_user_id and assigned_job is not None:
                event = getattr(assigned_job, "event", None)
                enqueue_push(
                    ba_user_id,
                    title="You got the gig",
                    body=_booking_push_body(assigned_job),
                    data={
                        "kind": "job_assigned",
                        "jobUuid": str(assigned_job.uuid),
                        "eventUuid": str(getattr(event, "uuid", "")),
                    },
                )
        except Exception:
            pass

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
        # Confirm the booking: approving an AmbassadorJob is an admin/RMM
        # confirming the BA onto the shift, so the AmbassadorEvent must end
        # is_approved=True or the shift never surfaces on the mobile
        # "What's on the books" screen (which reads is_approved=True only).
        # Get-or-create, and flip an existing invite/accept row from
        # is_approved=False → True. created_by/updated_by are non-null
        # RESTRICT; stamp the acting admin, fall back to the job creator.
        await _confirm_booking_for_ambassador_job(ambassador_job, user)

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
from jobs.queries import _FavoriteAmbassadorScope  # noqa: E402
from jobs.job_scope import JobScope  # noqa: E402


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

        # Tenant gate: a client may only post their OWN tenant's jobs; a job
        # in another tenant is surfaced as "not found" (no cross-tenant
        # existence leak / write). Admins may post any tenant's job.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return _build_lifecycle_response(False, "Job not found.", input_obj=input)

        def _post():
            try:
                job = models.Job.objects.get(pk=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if allowed is not None and job.tenant_id not in allowed:
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
            # `ongoing` is the BA job-board visibility gate (my_available_jobs
            # filters ongoing=True, closed=False, public=True). It defaults to
            # False at job creation and nothing else flips it on, so a posted
            # job stayed invisible without this. Posting = live on the board.
            job.ongoing = True
            job.save(update_fields=[
                "total_hours", "hourly_rate", "uniform_notes",
                "description", "max_applications", "favorites_only",
                "lifecycle_status", "posted_at", "public", "ongoing",
                "updated_at",
            ])
            # At-post-time geo-proximity push to eligible BAs near the gig
            # (falls back to preferred-state matching when coords are
            # missing). Best-effort: never breaks the post on push failure.
            from jobs.notifications import notify_nearby_bas_of_new_gig
            notify_nearby_bas_of_new_gig(job)
            return job, "Job posted."

        job, msg = await sync_to_async(_post)()
        return _build_lifecycle_response(job is not None, msg, job=job, input_obj=input)

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def post_event_to_board(
        self,
        info: strawberry.Info,
        input: inputs.PostEventToBoardInput,
    ) -> types.JobLifecycleResponse:
        """Master Tracker "Post to board": find-or-create the event's
        Job and post it to the public BA job board, open to all.

        Works even when the event has no Job yet (bulk / born-approved
        events skip the auto-create signal) and when the tenant lacks a
        default JobTitle / Rate (the gap that currently skips Girl Beer)
        — in that case we create sensible defaults so the post never
        silently no-ops.
        """
        try:
            event_pk = _resolve_id(input.event_id)
        except (TypeError, ValueError, GraphQLError):
            return _build_lifecycle_response(
                False, "Invalid event id.", input_obj=input
            )

        actor = info.context.request.user

        # Tenant gate: a client may only post their OWN tenant's events to the
        # board; an event in another tenant is surfaced as "not found" (no
        # cross-tenant existence leak / write). Admins may post any tenant's.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return _build_lifecycle_response(
                False, "Event not found.", input_obj=input
            )

        def _post_event():
            from events.models import Event as _Event
            # Mirror the signal's imports exactly. STATUS_PENDING is a Job
            # class attribute (not a module-level name), so alias it here —
            # importing it from jobs.models raised ImportError and broke this
            # whole resolver for every caller before the tenant gate even ran.
            from jobs.models import Job, JobTitle, Rate
            from jobs.models import RateType
            STATUS_PENDING = Job.STATUS_PENDING

            try:
                event = _Event.objects.select_related("tenant").get(pk=event_pk)
            except _Event.DoesNotExist:
                return None, "Event not found."

            tenant_id = event.tenant_id
            if allowed is not None and tenant_id not in allowed:
                return None, "Event not found."
            actor_id = actor.id if getattr(actor, "id", None) else None

            job = Job.objects.filter(event_id=event.id).first()

            if job is None:
                # No Job yet — create a pending one mirroring
                # auto_create_pending_job_on_request_approval in
                # events/signals.py. Same _first_for default picker.
                def _first_for(model, tid):
                    try:
                        row = model.objects.filter(tenant_id=tid).order_by("id").first()
                        if row:
                            return row
                    except Exception:
                        pass
                    try:
                        return model.objects.order_by("id").first()
                    except Exception:
                        return None

                default_title = _first_for(JobTitle, tenant_id)
                default_rate = _first_for(Rate, tenant_id)

                # Fill the gap that currently skips Girl Beer: if the
                # tenant has no JobTitle / Rate at all, create sensible
                # defaults instead of bailing like the signal does.
                if default_title is None:
                    default_title = JobTitle.objects.create(
                        tenant_id=tenant_id,
                        name="Brand Ambassador",
                        created_by_id=actor_id,
                        updated_by_id=actor_id,
                    )
                if default_rate is None:
                    rate_type = (
                        RateType.objects.filter(tenant_id=tenant_id)
                        .order_by("id")
                        .first()
                    )
                    if rate_type is None:
                        rate_type = RateType.objects.create(
                            tenant_id=tenant_id,
                            name="Hourly",
                            created_by_id=actor_id,
                            updated_by_id=actor_id,
                        )
                    default_rate = Rate.objects.create(
                        tenant_id=tenant_id,
                        rate_type=rate_type,
                        amount=input.hourly_rate,
                        created_by_id=actor_id,
                        updated_by_id=actor_id,
                    )

                job = Job.objects.create(
                    tenant_id=tenant_id,
                    event_id=event.id,
                    name=(event.name or "Activation")[:200],
                    address=event.address or "",
                    start_date=event.start_time,
                    end_date=event.end_time,
                    job_title=default_title,
                    rate=default_rate,
                    lifecycle_status=STATUS_PENDING,
                    favorites_only=True,
                    public=False,
                    closed=False,
                    national=False,
                    ongoing=False,
                    created_by_id=actor_id,
                    updated_by_id=actor_id,
                )

            # Post it — mirror post_job's field writes exactly, but
            # always open to all (favorites_only=False).
            job.total_hours = input.total_hours
            job.hourly_rate = input.hourly_rate
            if input.uniform_notes is not None:
                job.uniform_notes = input.uniform_notes
            job.favorites_only = False
            job.lifecycle_status = Job.STATUS_POSTED
            job.posted_at = _django_tz.now()
            job.public = True
            # ongoing = the BA board visibility gate (see post_job). Without
            # it a posted event stays off my_available_jobs.
            job.ongoing = True
            job.save(update_fields=[
                "total_hours", "hourly_rate", "uniform_notes",
                "favorites_only", "lifecycle_status", "posted_at",
                "public", "ongoing", "updated_at",
            ])
            # At-post-time geo-proximity push to eligible BAs near the gig
            # (state fallback when coords are missing). Best-effort.
            from jobs.notifications import notify_nearby_bas_of_new_gig
            notify_nearby_bas_of_new_gig(job)
            return job, "Event posted to the job board, open to all BAs."

        job, msg = await sync_to_async(_post_event)()
        return _build_lifecycle_response(
            job is not None, msg, job=job, input_obj=input
        )

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

        # Tenant gate: a client may only open their OWN tenant's jobs; a job
        # in another tenant is surfaced as "not found". Admins -> any tenant.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return _build_lifecycle_response(False, "Job not found.", input_obj=input)

        def _open():
            try:
                job = models.Job.objects.get(pk=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if allowed is not None and job.tenant_id not in allowed:
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
    async def delete_job(
        self,
        info: strawberry.Info,
        input: inputs.DeleteJobInput,
    ) -> types.JobLifecycleResponse:
        """Soft-delete a job posting (sets Job.deleted_at) so it drops off the
        admin Jobs board + the BA board. Works for ANY job — request-backed or
        not. Recoverable (NULL deleted_at). Leaves the parent request/event on
        the Master Tracker; use the tracker's delete to remove the whole gig.

        Tenant-gated exactly like assign_ambassador_to_job: a client may only
        delete their own tenant's jobs; a job in another tenant reads as
        'not found'. Admins may delete any tenant's job."""
        try:
            job_pk = _resolve_id(input.id)
        except (TypeError, ValueError, GraphQLError):
            return _build_lifecycle_response(False, "Invalid job id.", input_obj=input)

        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return _build_lifecycle_response(False, "Job not found.", input_obj=input)

        def _delete():
            try:
                job = models.Job.objects.get(pk=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if allowed is not None and job.tenant_id not in allowed:
                return None, "Job not found."
            if job.deleted_at is not None:
                return job, "Job already deleted."
            job.deleted_at = _django_tz.now()
            job.save(update_fields=["deleted_at", "updated_at"])
            return job, "Job deleted."

        job, msg = await sync_to_async(_delete)()
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

        # Tenant gate (the serious one): a client may only staff their OWN
        # tenant's gigs. A job in another tenant is surfaced as "not found"
        # so a client can never assign a BA to another brand's job. Admins
        # may assign on any tenant's job.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return _build_lifecycle_response(
                False, "Job or ambassador not found.", input_obj=input
            )

        def _assign():
            try:
                job = models.Job.objects.get(pk=job_pk)
                amb = _Ambassador.objects.get(pk=ba_pk)
            except (models.Job.DoesNotExist, _Ambassador.DoesNotExist):
                return None, "Job or ambassador not found.", []
            if allowed is not None and job.tenant_id not in allowed:
                return None, "Job or ambassador not found.", []
            if job.lifecycle_status not in (
                models.Job.STATUS_PENDING, models.Job.STATUS_POSTED
            ):
                return None, f"Job is {job.lifecycle_status}; can't reassign.", []

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

            # Snapshot user_ids of every applicant we're about to
            # auto-decline so the resolver can fan out "your application
            # was declined" pushes after the sync helper returns. Done
            # before the bulk-update because once the rows are flipped
            # we lose the ability to scope the query cleanly. Excludes
            # the accepted BA (their user gets the "you got it" push
            # below, not a decline push).
            declined_user_ids = list(
                models.JobApplication.objects.filter(
                    job=job, status=models.JobApplication.STATUS_APPLIED,
                )
                .exclude(pk=app.pk)
                .select_related("ambassador")
                .values_list("ambassador__user_id", flat=True)
                .distinct()
            )

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

            # Create (or flip) the booking so the accepted BA's shift shows
            # up on the mobile "What's on the books" screen, which reads
            # ONLY AmbassadorEvent(is_approved=True). Without this the BA
            # gets the "you got the gig" push but the shift never appears.
            # Mirrors ambassadors/mutations.py's invite pattern; created_by/
            # updated_by are non-null RESTRICT so we always stamp a real
            # user — the acting admin, falling back to job.created_by.
            actor_for_event = actor if getattr(actor, "id", None) else None
            booking_creator_id = (
                actor_for_event.id if actor_for_event else job.created_by_id
            )
            event = job.event
            booking, booking_created = AmbassadorEvent.objects.get_or_create(
                ambassador=amb,
                event=event,
                defaults={
                    "tenant_id": job.tenant_id,
                    "is_approved": True,
                    "created_by_id": booking_creator_id,
                    "updated_by_id": booking_creator_id,
                },
            )
            if not booking_created and not booking.is_approved:
                # A prior invite/accept left an is_approved=False row — flip
                # it to approved so the shift surfaces, and re-stamp updater.
                booking.is_approved = True
                booking.updated_by_id = booking_creator_id
                booking.save(update_fields=["is_approved", "updated_by", "updated_at"])

            # The mobile shift screens key off event.start_time / event.date.
            # If both are null the booking won't surface there yet — still
            # create it (above), but log so the gap is visible.
            if not getattr(event, "start_time", None) and not getattr(
                event, "date", None
            ):
                logger.warning(
                    "Booking created for job=%s ambassador=%s event=%s with no "
                    "start_time/date — shift won't appear on mobile shift "
                    "screens until the event is scheduled.",
                    job.id, amb.id, event.id,
                )

            return (
                job,
                f"{amb.user.get_full_name() if amb.user else 'BA'} assigned to the job.",
                declined_user_ids,
            )

        job, msg, declined_user_ids = await sync_to_async(_assign)()
        # Push the assignment to the BA — admin moved their applied row
        # to accepted (or assigned them outright). Without this the BA
        # has no idea they got the gig until they re-open the app.
        if job is not None:
            # Bind before the push try: the declined-applicant fan-out below
            # references ba_user_id to skip the accepted BA. If the push
            # lookup raised before assigning it (e.g. the Ambassador row
            # vanished mid-request), the except swallowed the error but the
            # later reference threw NameError out of the resolver — AFTER the
            # booking had already committed, so the accept surfaced as a failed
            # mutation. Pre-binding to None keeps the fan-out safe (None never
            # equals a real declined_user_id, so no recipient is skipped).
            ba_user_id = None
            try:
                from ambassadors.push import enqueue_push
                ba_user_id = await sync_to_async(
                    lambda: _Ambassador.objects.values_list(
                        "user_id", flat=True
                    ).get(pk=ba_pk)
                )()
                if ba_user_id:
                    enqueue_push(
                        ba_user_id,
                        title="You got the gig",
                        body=_booking_push_body(job),
                        data={
                            "kind": "job_assigned",
                            "jobUuid": str(job.uuid),
                            "eventUuid": str(getattr(job.event, "uuid", "")),
                        },
                    )
            except Exception:
                # Push is best-effort — don't fail the assignment.
                pass

            # Booking-confirmation email — the BA gets the full event card
            # (venue, date, time, address, pay). Guarded so a mail failure
            # (no Redis, bad template, transient SMTP) never breaks the
            # booking that already committed above.
            try:
                await _notify_booked_ambassador_by_email(job, ba_pk)
            except Exception:
                # Email is best-effort — the booking + push already landed.
                pass
            # Fan-out "your application wasn't selected this time" pushes
            # to the BAs whose applications got auto-declined when this
            # accept fired. Without this they sit in the pending queue
            # silently with no signal that the gig went to someone else
            # — which makes the marketplace feel unresponsive.
            for declined_user_id in declined_user_ids:
                if not declined_user_id or declined_user_id == ba_user_id:
                    continue
                try:
                    from ambassadors.push import enqueue_push as _enqueue_push

                    _enqueue_push(
                        declined_user_id,
                        title="Application update",
                        body=(
                            f"Another BA was selected for {job.name}. "
                            "Keep an eye out — new gigs post regularly."
                        ),
                        data={
                            "kind": "application_declined",
                            "jobUuid": str(job.uuid),
                        },
                    )
                except Exception:
                    # Best-effort per-recipient — keep going on a dead
                    # PushDevice for one user.
                    pass
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

        # Set inside _apply() only on a *fresh* application (newly created or a
        # re-apply from withdrawn/declined) — drives the staffing email so an
        # "already on file" no-op or any error path doesn't re-notify admins.
        notify_holder = {"value": False}

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

            # Rate + contractor-agreement gate. Only enforced when the
            # job actually has a rate AND an active agreement is on file —
            # so brands not yet configured with one aren't blocked.
            from django.utils import timezone as _dj_tz

            agreement = models.ContractorAgreement.active_for_tenant(
                job.tenant_id
            )
            gate_active = bool(agreement) and bool(job.hourly_rate)
            if gate_active:
                if not input.rate_confirmed:
                    return None, (
                        "Confirm the shift rate before applying."
                    )
                if not input.agreement_accepted:
                    return None, (
                        "Accept the contractor agreement before applying."
                    )

            accept_defaults = {}
            if gate_active:
                accept_defaults = {
                    "rate_confirmed_amount": job.hourly_rate,
                    "agreement_id": agreement.id,
                    "agreement_version": agreement.version,
                    "agreement_accepted_at": _dj_tz.now(),
                }

            app, created = models.JobApplication.objects.get_or_create(
                job=job, ambassador=amb,
                defaults={
                    "tenant_id": job.tenant_id,
                    "status": models.JobApplication.STATUS_APPLIED,
                    "note": (input.note or "")[:1000],
                    **accept_defaults,
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
                    fields = ["status", "note", "updated_at"]
                    if gate_active:
                        app.rate_confirmed_amount = job.hourly_rate
                        app.agreement_id = agreement.id
                        app.agreement_version = agreement.version
                        app.agreement_accepted_at = _dj_tz.now()
                        fields += [
                            "rate_confirmed_amount",
                            "agreement",
                            "agreement_version",
                            "agreement_accepted_at",
                        ]
                    app.save(update_fields=fields)
                else:
                    return app, "Application already on file."
            notify_holder["value"] = True
            return app, "Applied."

        app, msg = await sync_to_async(_apply)()
        if app is not None and notify_holder["value"]:
            # Best-effort staffing alert (RMM + job poster + Ignite inbox).
            # _notify_admins_of_application swallows its own errors, so this
            # can never break the BA's apply.
            await sync_to_async(_notify_admins_of_application)(app.id)
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

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def update_job_preferences(
        self,
        info: strawberry.Info,
        input: inputs.UpdateJobPreferencesInput,
    ) -> types.AmbassadorJobPreferenceResponse:
        """Upsert the calling BA's job-board preferences. Partial update:
        only fields present in the input are changed."""
        actor = info.context.request.user

        def _save():
            try:
                amb = _Ambassador.objects.get(user=actor)
            except _Ambassador.DoesNotExist:
                return None, "Only ambassadors have job preferences."

            pref, _created = models.AmbassadorJobPreference.objects.get_or_create(
                ambassador=amb,
            )

            changed: list[str] = []
            if input.notify_new_gigs is not None:
                pref.notify_new_gigs = bool(input.notify_new_gigs)
                changed.append("notify_new_gigs")
            if input.preferred_state_codes is not None:
                # Normalize to upper-case 2-letter-ish codes, dropping blanks.
                pref.preferred_state_codes = [
                    c.strip().upper()
                    for c in input.preferred_state_codes
                    if c and c.strip()
                ]
                changed.append("preferred_state_codes")
            if input.clear_min_hourly_rate:
                pref.min_hourly_rate = None
                changed.append("min_hourly_rate")
            elif input.min_hourly_rate is not None:
                from decimal import Decimal
                pref.min_hourly_rate = Decimal(str(input.min_hourly_rate))
                changed.append("min_hourly_rate")

            if changed:
                changed.append("updated_at")
                pref.save(update_fields=changed)
            return pref, "Preferences saved."

        pref, msg = await sync_to_async(_save)()
        return types.AmbassadorJobPreferenceResponse(
            success=pref is not None,
            message=msg,
            client_mutation_id=input.client_mutation_id,
            preference=(
                types.AmbassadorJobPreference.from_model(pref) if pref else None
            ),
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

        # Tenant-scoped: a client is pinned to their OWN tenant (any
        # tenant_id is ignored), an admin may target the requested tenant.
        try:
            tenant_id = await _FavoriteAmbassadorScope().resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:
            tenant_id = None

        def _add():
            if not tenant_id:
                return False, "No tenant in scope."
            try:
                ba_pk = _resolve_id(input.ambassador_id)
            except (TypeError, ValueError, GraphQLError):
                return False, "Invalid ambassador id."

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
        # Tenant-scoped: a client can only remove from their OWN tenant's
        # roster (any tenant_id is ignored); an admin may target the
        # requested tenant.
        try:
            tenant_id = await _FavoriteAmbassadorScope().resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:
            tenant_id = None

        def _rm():
            if not tenant_id:
                return False, "No tenant in scope."
            try:
                ba_pk = _resolve_id(input.ambassador_id)
            except (TypeError, ValueError, GraphQLError):
                return False, "Invalid ambassador id."

            deleted, _ = models.TenantFavoriteAmbassador.objects.filter(
                tenant_id=tenant_id, ambassador_id=ba_pk,
            ).delete()
            return bool(deleted), "Removed from favorites." if deleted else "Wasn't on the list."

        ok, msg = await sync_to_async(_rm)()
        return types.FavoriteAmbassadorResponse(
            success=ok, message=msg, client_mutation_id=input.client_mutation_id,
        )


# -------------------------------------------------------------------
# BA Briefing mutations
# -------------------------------------------------------------------
#
# Saved templates live per-tenant. Applying a template to a job
# does a one-shot copy: job.briefing_title, job.briefing_body, and a
# fresh set of JobBriefingAttachment rows. The job keeps a pointer to
# the source template so the UI can show "Using template: X".

def _resolve_actor_tenant_id(info, supplied: object | None) -> int | None:
    if supplied:
        try:
            return _resolve_id(supplied)
        except Exception:
            return None
    actor = getattr(info.context, "request", None)
    actor = getattr(actor, "user", None) if actor else None
    if not actor:
        return None
    try:
        t = actor.get_tenant() if hasattr(actor, "get_tenant") else None
        return t.id if t else None
    except Exception:
        return None


def _copy_attachment_dict(att) -> dict:
    return dict(
        name=att.name,
        url=att.url,
        content_type=att.content_type or "",
        size=att.size,
    )


@strawberry.type
class BriefingTemplateMutations:
    """CRUD for reusable BA Briefing templates (per-tenant)."""

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def create_briefing_template(
        self, info, input: inputs.CreateBriefingTemplateInput,
    ) -> types.BriefingTemplateResponse:
        # Tenant-scoped: a client always creates under their OWN tenant (any
        # supplied tenantId is ignored); an admin may target the requested
        # tenant. A client with no tenant, or an admin who passed no usable
        # tenant id, gets a safe success=False rather than a cross-tenant write.
        try:
            scoped_tenant_id = await JobScope().resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:  # noqa: BLE001
            scoped_tenant_id = None

        def _create():
            actor = info.context.request.user
            tenant_id = scoped_tenant_id
            if not tenant_id:
                return None, "No tenant in scope."
            tpl = models.BriefingTemplate.objects.create(
                tenant_id=tenant_id,
                name=input.name.strip(),
                title=(input.title or "").strip(),
                body=input.body or "",
                created_by=actor if getattr(actor, "is_authenticated", False) else None,
                updated_by=actor if getattr(actor, "is_authenticated", False) else None,
            )
            for att in (input.attachments or []):
                models.BriefingTemplateAttachment.objects.create(
                    template=tpl,
                    name=att.name,
                    url=att.url,
                    content_type=att.content_type or "",
                    size=att.size,
                )
            return tpl, "Briefing template created."

        tpl, msg = await sync_to_async(_create)()
        return types.BriefingTemplateResponse(
            success=tpl is not None,
            message=msg,
            briefing_template=tpl,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def save_job_briefing_as_template(
        self, info, input: inputs.SaveJobBriefingAsTemplateInput,
    ) -> types.BriefingTemplateResponse:
        """Snapshot a Job's current briefing (title + body + attachments) into
        a new reusable BriefingTemplate under the JOB's tenant.

        Tenant gate: a client may only save from their OWN tenant's job; a job
        in another tenant is surfaced as "not found" (no cross-tenant read of
        another brand's briefing). Admins -> any tenant. The new template is
        always created under job.tenant_id (not a caller-supplied tenant)."""
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            allowed = set()

        def _save():
            try:
                job_pk = _resolve_id(input.job_id)
            except Exception:
                return None, "Invalid job id."
            name = (input.name or "").strip()
            if not name:
                return None, "Template name is required."
            try:
                job = models.Job.objects.get(id=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if allowed is not None and job.tenant_id not in allowed:
                return None, "Job not found."

            actor = info.context.request.user
            tpl = models.BriefingTemplate.objects.create(
                tenant_id=job.tenant_id,
                name=name,
                title=job.briefing_title or "",
                body=job.briefing_body or "",
                created_by=actor if getattr(actor, "is_authenticated", False) else None,
                updated_by=actor if getattr(actor, "is_authenticated", False) else None,
            )
            # Clone the job's briefing attachments into template attachments
            # (same blob paths — they point at the same GCS objects).
            for att in job.briefing_attachments.all():
                models.BriefingTemplateAttachment.objects.create(
                    template=tpl, **_copy_attachment_dict(att),
                )
            # Re-fetch with attachments prefetched so the type's (sync)
            # attachments resolver reads the cache instead of issuing a DB
            # query in the async response phase (which would 0-out the list).
            tpl = (
                models.BriefingTemplate.objects
                .prefetch_related("attachments")
                .get(pk=tpl.pk)
            )
            return tpl, "Briefing saved as template."

        tpl, msg = await sync_to_async(_save)()
        return types.BriefingTemplateResponse(
            success=tpl is not None,
            message=msg,
            briefing_template=tpl,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def update_briefing_template(
        self, info, input: inputs.UpdateBriefingTemplateInput,
    ) -> types.BriefingTemplateResponse:
        # Tenant gate: a client may only edit their OWN tenant's templates; a
        # template in another tenant is surfaced as "not found" (no
        # cross-tenant existence leak / edit). Admins -> any tenant.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            allowed = set()

        def _update():
            try:
                tpl_pk = _resolve_id(input.template_id)
            except Exception:
                return None, "Invalid template id."
            try:
                tpl = models.BriefingTemplate.objects.get(id=tpl_pk)
            except models.BriefingTemplate.DoesNotExist:
                return None, "Template not found."
            if allowed is not None and tpl.tenant_id not in allowed:
                return None, "Template not found."
            actor = info.context.request.user
            if input.name is not None:
                tpl.name = input.name.strip()
            if input.title is not None:
                tpl.title = input.title.strip()
            if input.body is not None:
                tpl.body = input.body
            tpl.updated_by = actor if getattr(actor, "is_authenticated", False) else None
            tpl.save()
            if input.attachments is not None:
                # Replace-all semantics.
                tpl.attachments.all().delete()
                for att in input.attachments:
                    models.BriefingTemplateAttachment.objects.create(
                        template=tpl,
                        name=att.name,
                        url=att.url,
                        content_type=att.content_type or "",
                        size=att.size,
                    )
            return tpl, "Template updated."

        tpl, msg = await sync_to_async(_update)()
        return types.BriefingTemplateResponse(
            success=tpl is not None,
            message=msg,
            briefing_template=tpl,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def archive_briefing_template(
        self, info, input: inputs.ArchiveBriefingTemplateInput,
    ) -> types.BriefingTemplateResponse:
        # Tenant gate: a client may only archive their OWN tenant's templates;
        # a template in another tenant is surfaced as "not found". Admins ->
        # any tenant.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            allowed = set()

        def _archive():
            try:
                tpl_pk = _resolve_id(input.template_id)
            except Exception:
                return None, "Invalid template id."
            try:
                tpl = models.BriefingTemplate.objects.get(id=tpl_pk)
            except models.BriefingTemplate.DoesNotExist:
                return None, "Template not found."
            if allowed is not None and tpl.tenant_id not in allowed:
                return None, "Template not found."
            tpl.is_archived = True
            tpl.save(update_fields=["is_archived", "updated_at"])
            return tpl, "Template archived."

        tpl, msg = await sync_to_async(_archive)()
        return types.BriefingTemplateResponse(
            success=tpl is not None,
            message=msg,
            briefing_template=tpl,
            client_mutation_id=input.client_mutation_id,
        )


@strawberry.type
class GigTemplateMutations:
    """CRUD for reusable Gig templates (per-tenant Post-Job-modal defaults)."""

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def create_gig_template(
        self, info, input: inputs.CreateGigTemplateInput,
    ) -> types.GigTemplateResponse:
        # Tenant-scoped: a client always creates under their OWN tenant (any
        # supplied tenantId is ignored); an admin may target the requested
        # tenant. A client with no tenant, or an admin who passed no usable
        # tenant id, gets a safe success=False rather than a cross-tenant write.
        try:
            scoped_tenant_id = await JobScope().resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:  # noqa: BLE001
            scoped_tenant_id = None

        def _create():
            actor = info.context.request.user
            tenant_id = scoped_tenant_id
            if not tenant_id:
                return None, "No tenant in scope."
            tpl = models.GigTemplate.objects.create(
                tenant_id=tenant_id,
                name=input.name.strip(),
                hourly_rate=input.hourly_rate,
                total_hours=input.total_hours,
                uniform_notes=(input.uniform_notes or ""),
                default_open_to_all=bool(input.default_open_to_all),
                created_by=actor if getattr(actor, "is_authenticated", False) else None,
                updated_by=actor if getattr(actor, "is_authenticated", False) else None,
            )
            return tpl, "Gig template created."

        tpl, msg = await sync_to_async(_create)()
        return types.GigTemplateResponse(
            success=tpl is not None,
            message=msg,
            gig_template=tpl,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def save_job_post_as_gig_template(
        self, info, input: inputs.SaveJobPostAsGigTemplateInput,
    ) -> types.GigTemplateResponse:
        """Snapshot a Job's current post settings (hourly_rate, total_hours,
        uniform_notes, default_open_to_all = not favorites_only) into a new
        reusable GigTemplate under the JOB's tenant.

        Tenant gate: a client may only save from their OWN tenant's job; a job
        in another tenant is surfaced as "not found" (no cross-tenant read of
        another brand's job). Admins -> any tenant. The new template is always
        created under job.tenant_id (not a caller-supplied tenant)."""
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            allowed = set()

        def _save():
            try:
                job_pk = _resolve_id(input.job_id)
            except Exception:
                return None, "Invalid job id."
            name = (input.name or "").strip()
            if not name:
                return None, "Template name is required."
            try:
                job = models.Job.objects.get(id=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if allowed is not None and job.tenant_id not in allowed:
                return None, "Job not found."

            actor = info.context.request.user
            tpl = models.GigTemplate.objects.create(
                tenant_id=job.tenant_id,
                name=name,
                hourly_rate=job.hourly_rate,
                total_hours=job.total_hours,
                uniform_notes=(job.uniform_notes or ""),
                # favorites_only is the inverse of the modal's "Open to all".
                default_open_to_all=not job.favorites_only,
                created_by=actor if getattr(actor, "is_authenticated", False) else None,
                updated_by=actor if getattr(actor, "is_authenticated", False) else None,
            )
            return tpl, "Job post saved as gig template."

        tpl, msg = await sync_to_async(_save)()
        return types.GigTemplateResponse(
            success=tpl is not None,
            message=msg,
            gig_template=tpl,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def update_gig_template(
        self, info, input: inputs.UpdateGigTemplateInput,
    ) -> types.GigTemplateResponse:
        # Tenant gate: a client may only edit their OWN tenant's templates; a
        # template in another tenant is surfaced as "not found" (no
        # cross-tenant existence leak / edit). Admins -> any tenant.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            allowed = set()

        def _update():
            try:
                tpl_pk = _resolve_id(input.template_id)
            except Exception:
                return None, "Invalid template id."
            try:
                tpl = models.GigTemplate.objects.get(id=tpl_pk)
            except models.GigTemplate.DoesNotExist:
                return None, "Template not found."
            if allowed is not None and tpl.tenant_id not in allowed:
                return None, "Template not found."
            actor = info.context.request.user
            if input.name is not None:
                tpl.name = input.name.strip()
            if input.hourly_rate is not None:
                tpl.hourly_rate = input.hourly_rate
            if input.total_hours is not None:
                tpl.total_hours = input.total_hours
            if input.uniform_notes is not None:
                tpl.uniform_notes = input.uniform_notes
            if input.default_open_to_all is not None:
                tpl.default_open_to_all = input.default_open_to_all
            tpl.updated_by = actor if getattr(actor, "is_authenticated", False) else None
            tpl.save()
            return tpl, "Template updated."

        tpl, msg = await sync_to_async(_update)()
        return types.GigTemplateResponse(
            success=tpl is not None,
            message=msg,
            gig_template=tpl,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def archive_gig_template(
        self, info, input: inputs.ArchiveGigTemplateInput,
    ) -> types.GigTemplateResponse:
        # Tenant gate: a client may only archive their OWN tenant's templates;
        # a template in another tenant is surfaced as "not found". Admins ->
        # any tenant.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            allowed = set()

        def _archive():
            try:
                tpl_pk = _resolve_id(input.template_id)
            except Exception:
                return None, "Invalid template id."
            try:
                tpl = models.GigTemplate.objects.get(id=tpl_pk)
            except models.GigTemplate.DoesNotExist:
                return None, "Template not found."
            if allowed is not None and tpl.tenant_id not in allowed:
                return None, "Template not found."
            tpl.is_archived = True
            tpl.save(update_fields=["is_archived", "updated_at"])
            return tpl, "Template archived."

        tpl, msg = await sync_to_async(_archive)()
        return types.GigTemplateResponse(
            success=tpl is not None,
            message=msg,
            gig_template=tpl,
            client_mutation_id=input.client_mutation_id,
        )


@strawberry.type
class JobBriefingMutations:
    """Per-job briefing edits + template-apply."""

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def set_job_briefing(
        self, info, input: inputs.SetJobBriefingInput,
    ) -> types.JobBriefingResponse:
        # Tenant gate: a client may only brief their OWN tenant's jobs; a job
        # in another tenant is surfaced as "not found" (no cross-tenant
        # existence leak / write). Admins -> any tenant.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            allowed = set()

        def _set():
            try:
                job_pk = _resolve_id(input.job_id)
            except Exception:
                return None, "Invalid job id."
            try:
                job = models.Job.objects.get(id=job_pk)
            except models.Job.DoesNotExist:
                return None, "Job not found."
            if allowed is not None and job.tenant_id not in allowed:
                return None, "Job not found."
            if input.title is not None:
                job.briefing_title = input.title.strip()
            if input.body is not None:
                job.briefing_body = input.body
            job.save(update_fields=["briefing_title", "briefing_body", "updated_at"])
            if input.attachments is not None:
                job.briefing_attachments.all().delete()
                for att in input.attachments:
                    models.JobBriefingAttachment.objects.create(
                        job=job,
                        name=att.name,
                        url=att.url,
                        content_type=att.content_type or "",
                        size=att.size,
                    )
            return job, "Briefing updated."

        job, msg = await sync_to_async(_set)()
        return types.JobBriefingResponse(
            success=job is not None,
            message=msg,
            job_uuid=str(job.uuid) if job else None,
            title=job.briefing_title if job else None,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[_StrictIsAuth])
    async def apply_briefing_template(
        self, info, input: inputs.ApplyBriefingTemplateInput,
    ) -> types.JobBriefingResponse:
        # Tenant gate: a client may only apply their OWN tenant's template to
        # their OWN tenant's job. Either resource living in another tenant is
        # surfaced as "not found" so a client can neither read another brand's
        # template body nor write it onto a foreign job. Admins -> any tenant.
        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            allowed = set()

        def _apply():
            try:
                job_pk = _resolve_id(input.job_id)
                tpl_pk = _resolve_id(input.template_id)
            except Exception:
                return None, "Invalid id."
            try:
                job = models.Job.objects.get(id=job_pk)
                tpl = models.BriefingTemplate.objects.get(id=tpl_pk)
            except (models.Job.DoesNotExist, models.BriefingTemplate.DoesNotExist):
                return None, "Job or template not found."
            if allowed is not None and (
                job.tenant_id not in allowed or tpl.tenant_id not in allowed
            ):
                return None, "Job or template not found."
            job.briefing_title = tpl.title
            job.briefing_body = tpl.body
            job.briefing_template_id = tpl.id
            job.save(update_fields=[
                "briefing_title", "briefing_body", "briefing_template",
                "updated_at",
            ])
            # Replace attachments with a fresh clone of the template's.
            job.briefing_attachments.all().delete()
            for att in tpl.attachments.all():
                models.JobBriefingAttachment.objects.create(
                    job=job, **_copy_attachment_dict(att),
                )
            return job, "Briefing applied from template."

        job, msg = await sync_to_async(_apply)()
        return types.JobBriefingResponse(
            success=job is not None,
            message=msg,
            job_uuid=str(job.uuid) if job else None,
            title=job.briefing_title if job else None,
            client_mutation_id=input.client_mutation_id,
        )
