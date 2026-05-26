"""Services for ambassador mutations and queries."""

import asyncio
import sys
import secrets
import string
from decimal import Decimal
from datetime import timedelta, datetime
from typing import Any

import strawberry
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from graphql import GraphQLError

from tenants.models import Role, Tenant, TenantedUser
from tenants.envelopes import EmailVerificationMailer
from gqlauth.core.utils import get_token
from gqlauth.models import UserStatus
from events.models import Location
from utils.utils import ROLE_ID, build_mutation_response
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import (
    SparkGraphQLMixin,
    BaseMutationService,
    resolve_id_to_int,
)
from utils.graphql.relay import CountableConnection

from .models import (
    Ambassador,
    AmbassadorInvitation,
    AmbassadorReview,
    AmbassadorNote,
    AmbassadorFile,
    AmbassadorTrait,
    AmbassadorWorkHistory,
    Skill,
    AmbassadorSkill,
    GroupType,
    AmbassadorGroup,
    AmbassadorGroupJob,
    UserGroup,
    Attendance,
    AttendanceType,
    PushDevice,
    LocationPing,
)
from jobs import models as job_models
from .types import (
    PublicAmbassadorCreationResponse,
    AmbassadorInvitationResponse,
    AcceptInvitationResponse,
    ApproveAmbassadorResponse,
    DisableAmbassadorResponse,
    RegenerateAmbassadorPasswordResult,
    RegenerateAmbassadorPasswordsResponse,
    CreateAmbassadorResponse,
    UpdateAmbassadorResponse,
    DeleteInvitationResponse,
    AmbassadorInvitationType,
    Ambassador as AmbassadorType,
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
    AmbassadorProfile,
    GroupTypeResponse,
    AmbassadorGroupResponse,
    RegisterPushTokenResponse,
    OAuthSignInResponse,
    OAuthTokenType,
    OAuthUserType,
    LocationPingResponse,
    RespondToShiftOfferResponse,
    ShiftOfferDetails,
)
from events.models import Client
from . import inputs
from .constants import INVITATION_EXPIRY_DAYS
from .envelopes import AmbassadorGeneratedPasswordMailer

User = get_user_model()
RESEND_EMAILS_PER_SECOND = 2
RESEND_EMAIL_DELAY_SECONDS = 1 / RESEND_EMAILS_PER_SECOND


async def set_ambassador_job_real_amount_from_clock_out(attendance: Attendance) -> None:
    """Set AmbassadorJob.real_amount to 25% of rate when attendance type is clock_out."""
    if not attendance or not attendance.attendace_type_id:
        return

    attendance_type_slug = await sync_to_async(
        lambda: AttendanceType.objects.filter(id=attendance.attendace_type_id)
        .values_list("slug", flat=True)
        .first()
    )()
    attendance_type_slug = (attendance_type_slug or "").strip().lower()
    if attendance_type_slug != "clock_out":
        return

    if not attendance.ambassador_id or not attendance.job_id:
        return

    ambassador_job = await sync_to_async(
        lambda: job_models.AmbassadorJob.objects.select_related("rate")
        .filter(ambassador_id=attendance.ambassador_id, job_id=attendance.job_id)
        .order_by("-created_at")
        .first()
    )()
    if not ambassador_job or not ambassador_job.rate:
        return

    rate_amount = ambassador_job.rate.amount
    if rate_amount is None:
        return

    ambassador_job.real_amount = rate_amount * Decimal("0.25")
    await sync_to_async(ambassador_job.save)(update_fields=["real_amount", "updated_at"])


def validate_passwords_match(
    input: SparkGraphQLInput, response_class: type
) -> None | Any:
    """
    Validate that passwords match.

    Returns:
        None if passwords match, error response object if they don't match
    """
    if input.password1 != input.password2:
        return build_mutation_response(
            response_class,
            success=False,
            message="Passwords do not match.",
            input_obj=input,
        )
    return None


def generate_random_password(length: int = 12) -> str:
    """Generate a readable random password for ambassador onboarding."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class BaseAmbassadorService(SparkGraphQLMixin):
    """Base service class for ambassador operations."""

    @staticmethod
    async def check_email_exists(email: str) -> bool:
        """Check if a user with the given email exists."""
        return await sync_to_async(User.objects.filter(email=email).exists)()

    @staticmethod
    async def check_active_invitation_exists(email: str) -> bool:
        """
        Check if an active invitation exists for the given email.

        Args:
            email: The email to check

        Returns:
            True if an active invitation exists, False otherwise
        """
        now = timezone.now()
        return await sync_to_async(
            AmbassadorInvitation.objects.filter(
                email=email,
                is_used=False,
                expires_at__gt=now,
            ).exists
        )()

    @staticmethod
    async def mark_invitation_used(
        invitation: AmbassadorInvitation,
        ambassador: Ambassador,
        used_by: User,
    ) -> None:
        """
        Mark an invitation as used.

        Args:
            invitation: The invitation to mark as used
            ambassador: The ambassador associated with the invitation
            used_by: The user who used the invitation
        """

        @sync_to_async
        def _mark_invitation_used():
            invitation.is_used = True
            invitation.used_at = timezone.now()
            invitation.ambassador = ambassador
            invitation.updated_by = used_by
            invitation.save()

        await _mark_invitation_used()

    @staticmethod
    async def create_ambassador_user(
        first_name: str,
        last_name: str | None,
        email: str,
        role: Any,
        password: str,
        is_active: bool,
    ) -> User:
        """
        Create a new user for an ambassador.

        Args:
            first_name: The user's first name
            email: The user's email (used as username)
            role: The user's role
            password: The user's password
            is_active: Whether the user is active

        Returns:
            The created user instance
        """

        @sync_to_async
        def _create_ambassador_user():
            user = User.objects.create(
                first_name=first_name,
                last_name=last_name or "",
                username=email,
                email=email,
                role=role,
                is_active=is_active,
            )
            user.set_password(password)
            user.save()
            return user

        return await _create_ambassador_user()

    @staticmethod
    async def resolve_location(
        location_id: strawberry.ID | None,
    ) -> Location | None:
        """Resolve a Location by ID."""
        if location_id is None:
            return None

        resolved_location_id = resolve_id_to_int(location_id)
        return await sync_to_async(Location.objects.select_related("state").get)(
            id=resolved_location_id
        )

    @staticmethod
    async def assign_user_to_tenant(
        user: User,
        tenant: Tenant,
        created_by: User,
    ) -> TenantedUser | None:
        """
        Assign a user to a tenant if not already assigned.

        Args:
            user: The user to assign
            tenant: The tenant to assign to
            created_by: The user performing the action

        Returns:
            The created TenantedUser instance, or None if already exists
        """

        @sync_to_async
        def _assign_user_to_tenant():
            # Check if TenantedUser already exists
            if TenantedUser.objects.filter(user=user, tenant=tenant).exists():
                return None

            return TenantedUser.objects.create(
                user=user,
                tenant=tenant,
                is_active=True,
                created_by=created_by,
                updated_by=created_by,
            )

        return await _assign_user_to_tenant()


class PublicAmbassadorCreationService(BaseAmbassadorService):
    """Service for public ambassador creation."""

    @classmethod
    def resolve_password(
        cls,
        input: inputs.CreatePublicAmbassadorInput,
        response_class: type,
        allow_generated_password: bool = False,
    ) -> tuple[str | None, Any | None, bool]:
        """
        Resolve the password to use for account creation.

        Returns:
            tuple: (password, error_response, password_was_generated)
        """
        password1 = getattr(input, "password1", None)
        password2 = getattr(input, "password2", None)

        if allow_generated_password and not password1 and not password2:
            return generate_random_password(), None, True

        if not password1 or not password2:
            return None, build_mutation_response(
                response_class,
                success=False,
                message="Both password fields are required when setting a password manually.",
                input_obj=input,
            ), False

        password_error = validate_passwords_match(input, response_class)
        if password_error:
            return None, password_error, False

        return password1, None, False

    @classmethod
    async def create(
        cls,
        input: inputs.CreatePublicAmbassadorInput,
        info: strawberry.Info,
        ambassador_is_active: bool | None = None,
    ) -> PublicAmbassadorCreationResponse:
        """Create a public ambassador account (inactive by default)."""
        ambassador_is_active = (
            ambassador_is_active if ambassador_is_active is not None else False
        )
        is_admin_created = isinstance(input, inputs.CreateAmbassadorWithUserInput)
        password, password_error, password_was_generated = cls.resolve_password(
            input=input,
            response_class=PublicAmbassadorCreationResponse,
            allow_generated_password=is_admin_created,
        )
        if password_error:
            return password_error

        # Validate email doesn't exist
        if await cls.check_email_exists(input.email):
            return build_mutation_response(
                PublicAmbassadorCreationResponse,
                success=False,
                message="Email already exists.",
                input_obj=input,
            )

        # Get ambassador role
        role = await Role.get_ambassador_role()

        try:
            # Create user
            user = await cls.create_ambassador_user(
                first_name=input.first_name,
                last_name=getattr(input, "last_name", None),
                email=input.email,
                role=role,
                password=password,
                is_active=True,
            )

            # Admin-created BA with a temp password: force them to
            # change it on first sign-in. Public self-signups picked
            # their own password and don't need the prompt.
            if is_admin_created:
                @sync_to_async
                def _flag_password_change():
                    user.requires_password_change = True
                    user.save(update_fields=["requires_password_change"])

                await _flag_password_change()

            location = await cls.resolve_location(
                location_id=getattr(input, "location_id", None),
            )

            # Create ambassador (inactive by default)
            ambassador = await Ambassador.objects._create(
                user=user,
                address=input.address,
                phone=input.phone,
                about_me=input.about_me,
                coordinates=input.coordinates or [],
                location=location,
                is_active=ambassador_is_active,  # Requires manual approval by default
                created_by=user,
                updated_by=user,
            )

            # Generate activation token
            activation_token: str | None = None
            if is_admin_created:
                await sync_to_async(UserStatus.objects.update_or_create)(
                    user=user,
                    defaults={"verified": True, "archived": False},
                )
            else:
                activation_token = await sync_to_async(get_token)(user, "activation")
                frontend_urls = {
                    "client": settings.CLIENT_FRONTEND_URL,
                    "ambassador": settings.AMBASSADOR_FRONTEND_URL,
                    "spark-admin": settings.ADMIN_FRONTEND_URL,
                }
                activation_url = (
                    f"{frontend_urls.get(role.slug, settings.AMBASSADOR_FRONTEND_URL)}/"
                    f"verify-account?token={activation_token}"
                )
                verification_email = EmailVerificationMailer(user, activation_url)
                await verification_email.send_async()

            if password_was_generated:
                generated_password_email = AmbassadorGeneratedPasswordMailer(
                    user, password
                )
                await generated_password_email.send_async()

            # Best-effort admin alert — same posture as the OAuth path.
            # Only fires for public (non-admin-created) signups; when an
            # admin creates an ambassador they already know about it.
            if not is_admin_created:
                try:
                    from ambassadors.envelopes import NewAmbassadorAlertMailer

                    alert = NewAmbassadorAlertMailer(ambassador, provider="email")
                    await alert.send_async()
                except Exception:
                    import logging

                    logging.getLogger(__name__).exception(
                        "new ambassador alert email failed for ambassador_id=%s",
                        ambassador.id,
                    )

            return build_mutation_response(
                PublicAmbassadorCreationResponse,
                success=True,
                message=(
                    "Ambassador account created successfully."
                    if is_admin_created
                    else "Ambassador account created successfully. Please verify your email and wait for approval."
                ),
                input_obj=input,
                ambassador=ambassador,
                activation_token=activation_token,
            )
        except Exception as e:
            return build_mutation_response(
                PublicAmbassadorCreationResponse,
                success=False,
                message=f"Error creating ambassador: {str(e)}",
                input_obj=input,
            )


class RegenerateAmbassadorPasswordsService(BaseAmbassadorService):
    """Service for regenerating ambassador passwords in batch."""

    @classmethod
    async def regenerate(
        cls,
        input: inputs.RegenerateAmbassadorPasswordsInput,
        info: strawberry.Info,
    ) -> RegenerateAmbassadorPasswordsResponse:
        service = cls()
        user = await service.get_user(info)

        results: list[RegenerateAmbassadorPasswordResult] = []

        for index, ambassador_id in enumerate(input.ambassador_ids):
            try:
                resolved_ambassador_id = resolve_id_to_int(ambassador_id)
            except (TypeError, ValueError, GraphQLError):
                results.append(
                    RegenerateAmbassadorPasswordResult(
                        ambassador_id=None,
                        email=None,
                        success=False,
                        message=f"Invalid ambassador ID: {ambassador_id}",
                    )
                )
                continue

            try:
                ambassador = await sync_to_async(
                    Ambassador.objects.select_related("user").get
                )(id=resolved_ambassador_id)
            except Ambassador.DoesNotExist:
                results.append(
                    RegenerateAmbassadorPasswordResult(
                        ambassador_id=resolved_ambassador_id,
                        email=None,
                        success=False,
                        message="Ambassador not found.",
                    )
                )
                continue

            email = (getattr(ambassador.user, "email", None) or "").strip() or None
            if not email:
                results.append(
                    RegenerateAmbassadorPasswordResult(
                        ambassador_id=ambassador.id,
                        email=None,
                        success=False,
                        message="Ambassador has no email.",
                    )
                )
                continue

            try:
                password = generate_random_password()

                @sync_to_async
                def _persist_password():
                    ambassador.user.set_password(password)
                    ambassador.user.updated_by = user
                    ambassador.user.save(update_fields=["password", "updated_by", "updated_at"])

                await _persist_password()
                await sync_to_async(
                    AmbassadorGeneratedPasswordMailer(
                        ambassador.user,
                        password,
                    ).send
                )(delay_seconds=index * RESEND_EMAIL_DELAY_SECONDS)

                results.append(
                    RegenerateAmbassadorPasswordResult(
                        ambassador_id=ambassador.id,
                        email=email,
                        success=True,
                        message="Password regenerated and emailed successfully.",
                    )
                )
            except Exception as exc:
                results.append(
                    RegenerateAmbassadorPasswordResult(
                        ambassador_id=ambassador.id,
                        email=email,
                        success=False,
                        message=f"Error regenerating password: {exc}",
                    )
                )

        success_count = sum(1 for result in results if result.success)
        failure_count = len(results) - success_count
        message = (
            f"Processed {len(results)} ambassadors. "
            f"{success_count} succeeded, {failure_count} failed."
        )
        return build_mutation_response(
            RegenerateAmbassadorPasswordsResponse,
            success=failure_count == 0,
            message=message,
            input_obj=input,
            results=results,
        )


class AmbassadorInvitationService(BaseAmbassadorService):
    """Service for creating ambassador invitations."""

    @classmethod
    async def create(
        cls,
        input: inputs.CreateAmbassadorInvitationInput,
        info: strawberry.Info,
    ) -> AmbassadorInvitationResponse:
        """Create an ambassador invitation."""
        user = info.context.request.user

        # Validate user doesn't exist
        if await cls.check_email_exists(input.email):
            return build_mutation_response(
                AmbassadorInvitationResponse,
                success=False,
                message="User with this email already exists.",
                input_obj=input,
            )

        # Validate no active invitation exists
        if await cls.check_active_invitation_exists(input.email):
            return build_mutation_response(
                AmbassadorInvitationResponse,
                success=False,
                message="An active invitation already exists for this email.",
                input_obj=input,
            )

        try:
            # Get tenant
            tenant = await Tenant.objects._get(id=input.tenant_id)
        except (Tenant.DoesNotExist, ValueError, TypeError):
            return build_mutation_response(
                AmbassadorInvitationResponse,
                success=False,
                message="Invalid tenant ID.",
                input_obj=input,
            )

        try:
            # Generate secure token
            now = timezone.now()
            token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(days=INVITATION_EXPIRY_DAYS)

            # Create invitation
            invitation = await AmbassadorInvitation.objects._create(
                email=input.email,
                token=token,
                expires_at=expires_at,
                invited_by=user,
                tenant=tenant,
                created_by=user,
                updated_by=user,
            )

            return build_mutation_response(
                AmbassadorInvitationResponse,
                success=True,
                message="Invitation created successfully.",
                input_obj=input,
                invitation=invitation,
            )
        except Exception as e:
            return build_mutation_response(
                AmbassadorInvitationResponse,
                success=False,
                message=f"Error creating invitation: {str(e)}",
                input_obj=input,
            )


class AcceptInvitationService(BaseAmbassadorService):
    """Service for accepting ambassador invitations."""

    @classmethod
    async def accept(
        cls,
        input: inputs.AcceptAmbassadorInvitationInput,
        info: strawberry.Info,
    ) -> AcceptInvitationResponse:
        """Accept an ambassador invitation and create account."""
        # Validate passwords match
        password_error = validate_passwords_match(input, AcceptInvitationResponse)
        if password_error:
            return password_error

        # Get and validate invitation
        try:
            invitation = await AmbassadorInvitation.objects._by_token(input.token)
        except AmbassadorInvitation.DoesNotExist:
            return build_mutation_response(
                AcceptInvitationResponse,
                success=False,
                message="Invalid invitation token.",
                input_obj=input,
            )

        # Check if invitation is used
        if invitation.is_used:
            return build_mutation_response(
                AcceptInvitationResponse,
                success=False,
                message="This invitation has already been used.",
                input_obj=input,
            )

        # Check if invitation is expired
        now = timezone.now()
        if invitation.expires_at <= now:
            return build_mutation_response(
                AcceptInvitationResponse,
                success=False,
                message="This invitation has expired.",
                input_obj=input,
            )

        # Validate email doesn't already have user
        if await cls.check_email_exists(invitation.email):
            return build_mutation_response(
                AcceptInvitationResponse,
                success=False,
                message="User with this email already exists.",
                input_obj=input,
            )

        # Get ambassador role
        role = await Role.get_ambassador_role()

        try:
            # Create user
            user = await cls.create_ambassador_user(
                first_name=input.first_name,
                email=invitation.email,
                role=role,
                password=input.password1,
                is_active=True,
            )

            # Assign to tenant
            await cls.assign_user_to_tenant(
                user=user,
                tenant=invitation.tenant,
                created_by=invitation.invited_by,
            )

            # Create ambassador (active by default for invitations)
            # ambassador = await cls.create_ambassador(
            #     user=user,
            #     address=input.address,
            #     coordinates=input.coordinates,
            #     is_active=True,  # Active since invited
            #     created_by=invitation.invited_by,
            #     updated_by=invitation.invited_by,
            # )
            ambassador = await Ambassador.objects._create(
                user=user,
                address=input.address,
                coordinates=input.coordinates,
                is_active=True,  # Active since invited
                created_by=invitation.invited_by,
                updated_by=invitation.invited_by,
            )

            if invitation.job:
                await cls.accept_job_invitation(
                    invitation=invitation,
                    ambassador=ambassador,
                )

            # Mark invitation as used
            await cls.mark_invitation_used(
                invitation=invitation,
                ambassador=ambassador,
                used_by=invitation.invited_by,
            )

            # Generate activation token
            activation_token = await sync_to_async(get_token)(user, "activation")

            return build_mutation_response(
                AcceptInvitationResponse,
                success=True,
                message="Invitation accepted successfully. Please verify your email.",
                input_obj=input,
                ambassador=ambassador,
                activation_token=activation_token,
            )
        except Exception as e:
            return build_mutation_response(
                AcceptInvitationResponse,
                success=False,
                message=f"Error accepting invitation: {str(e)}",
                input_obj=input,
            )

    @classmethod
    async def accept_job_invitation(
        cls,
        invitation: AmbassadorInvitation,
        ambassador: Ambassador,
    ) -> None:
        """Accept or create the AmbassadorJob linked to an invitation."""

        @sync_to_async
        def _accept_job_invitation():
            accepted_status = job_models.Status.objects.get_accepted(
                tenant_id=invitation.tenant_id,
                user=invitation.invited_by,
            )

            ambassador_job = None
            if invitation.ambassador_id:
                ambassador_job = job_models.AmbassadorJob.objects.filter(
                    ambassador_id=invitation.ambassador_id,
                    job_id=invitation.job_id,
                ).first()

            if ambassador_job:
                ambassador_job.ambassador = ambassador
                ambassador_job.status = accepted_status
                ambassador_job.updated_by = invitation.invited_by
                ambassador_job.save(
                    update_fields=["ambassador", "status", "updated_by", "updated_at"]
                )
                return

            if not invitation.job or not invitation.job.rate_id:
                raise ValueError("Job not found or has no rate.")

            job_models.AmbassadorJob.objects.create(
                ambassador=ambassador,
                job=invitation.job,
                tenant=invitation.tenant,
                status=accepted_status,
                rate=invitation.job.rate,
                appear_as_rfp=True,
                created_by=invitation.invited_by,
                updated_by=invitation.invited_by,
            )

        await _accept_job_invitation()

    @classmethod
    async def accept_by_token(
        cls,
        input: inputs.AcceptByTokenInput,
        info: strawberry.Info,
    ) -> AcceptInvitationResponse:
        """Accept an ambassador invitation by token."""
        try:
            user = info.context.request.user
            token = input.token
            invitation = await AmbassadorInvitation.objects._by_token(token)
            invitation.is_usable(raise_exception=True)

            def get_ambassador():
                try:
                    ambassador = Ambassador.objects.get(user=user)
                except Ambassador.DoesNotExist:
                    ambassador = Ambassador.objects.create(
                        user=user,
                        created_by=user,
                        updated_by=user,
                    )
                return ambassador

            ambassador = await sync_to_async(get_ambassador)()
            invitation.ambassador = ambassador

            def update_invitation_job():
                from jobs.models import AmbassadorJob

                if not invitation.job:
                    return
                AmbassadorJob.objects.accept_from_invitation(invitation)

            await sync_to_async(update_invitation_job)()

            await cls.mark_invitation_used(invitation, ambassador, user)
            return build_mutation_response(
                AcceptInvitationResponse,
                success=True,
                message="Invitation accepted successfully.",
                input_obj=input,
                ambassador=ambassador,
            )
        except (
            ValueError,
            TypeError,
            GraphQLError,
            AmbassadorInvitation.DoesNotExist,
        ) as e:
            return build_mutation_response(
                AcceptInvitationResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class ApproveAmbassadorService(BaseAmbassadorService):
    """Service for approving ambassadors."""

    @classmethod
    async def approve(
        cls,
        input: inputs.ApproveAmbassadorInput,
        info: strawberry.Info,
    ) -> ApproveAmbassadorResponse:
        """Approve an ambassador."""
        user = info.context.request.user

        try:
            ambassador_id = resolve_id_to_int(input.ambassador_id)
            ambassador = await Ambassador.objects._by_id(ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                ApproveAmbassadorResponse,
                success=False,
                message="Ambassador not found.",
                input_obj=input,
            )

        try:
            # Set ambassador as active
            @sync_to_async
            def approve_ambassador():
                ambassador.is_active = True
                ambassador.updated_by = user
                ambassador.save()
                return ambassador

            ambassador = await approve_ambassador()

            return build_mutation_response(
                ApproveAmbassadorResponse,
                success=True,
                message="Ambassador approved successfully.",
                input_obj=input,
                ambassador=ambassador,
            )
        except Exception as e:
            return build_mutation_response(
                ApproveAmbassadorResponse,
                success=False,
                message=f"Error approving ambassador: {str(e)}",
                input_obj=input,
            )


class CreateAmbassadorService(BaseAmbassadorService):
    """Service for creating ambassadors (client/spark-admin)."""

    @classmethod
    async def create(
        cls,
        input: inputs.CreateAmbassadorInput,
        info: strawberry.Info,
    ) -> CreateAmbassadorResponse:
        user = info.context.request.user

        try:
            resolved_user_id = resolve_id_to_int(input.user_id)
            target_user = await sync_to_async(User.objects.get)(pk=resolved_user_id)
        except (User.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                CreateAmbassadorResponse,
                success=False,
                message="User not found.",
                input_obj=input,
            )

        existing = await Ambassador.objects.filter(user=target_user).aexists()
        if existing:
            return build_mutation_response(
                CreateAmbassadorResponse,
                success=False,
                message="This user already has an ambassador profile.",
                input_obj=input,
            )

        @sync_to_async
        @transaction.atomic
        def _create():
            ambassador = Ambassador(
                user=target_user,
                address=input.address,
                about_me=input.about_me,
                coordinates=input.coordinates or [],
                is_active=input.is_active or False,
                rating=input.rating or 0,
                created_by=user,
                updated_by=user,
            )
            ambassador.save()
            return ambassador

        ambassador = await _create()

        return build_mutation_response(
            CreateAmbassadorResponse,
            success=True,
            message="Ambassador successfully created.",
            input_obj=input,
            ambassador=ambassador,
        )


class DisableAmbassadorService(BaseAmbassadorService):
    """Service for disabling ambassadors and associated user accounts."""

    @classmethod
    async def disable(
        cls,
        input: inputs.DisableAmbassadorInput,
        info: strawberry.Info,
    ) -> DisableAmbassadorResponse:
        """Disable an ambassador and their associated user account."""
        user = info.context.request.user
        role_slug = cls().get_role_slug(user)

        try:
            ambassador_id = resolve_id_to_int(input.ambassador_id)
            ambassador = await Ambassador.objects.select_related("user").aget(
                id=ambassador_id
            )
        except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                DisableAmbassadorResponse,
                success=False,
                message="Ambassador not found.",
                input_obj=input,
            )

        is_client_or_spark = role_slug in {"client", "spark-admin"}
        is_own_ambassador = role_slug == "ambassador" and ambassador.user_id == user.id
        if not (is_client_or_spark or is_own_ambassador):
            return build_mutation_response(
                DisableAmbassadorResponse,
                success=False,
                message="You do not have permission to disable this ambassador.",
                input_obj=input,
            )

        try:
            @sync_to_async
            @transaction.atomic
            def disable_ambassador():
                ambassador.is_active = False
                ambassador.updated_by = user
                ambassador.save()

                ambassador_user = ambassador.user
                ambassador_user.is_active = False
                ambassador_user.updated_by = user
                ambassador_user.save()

                return ambassador

            ambassador = await disable_ambassador()

            return build_mutation_response(
                DisableAmbassadorResponse,
                success=True,
                message="Ambassador disabled successfully.",
                input_obj=input,
                ambassador=ambassador,
            )
        except Exception as e:
            return build_mutation_response(
                DisableAmbassadorResponse,
                success=False,
                message=f"Error disabling ambassador: {str(e)}",
                input_obj=input,
            )

    @classmethod
    async def disable_mobile(
        cls,
        info: strawberry.Info,
    ) -> DisableAmbassadorResponse:
        """Disable only the currently logged-in ambassador account."""
        user = info.context.request.user
        role_slug = cls().get_role_slug(user)
        if role_slug != "ambassador":
            return build_mutation_response(
                DisableAmbassadorResponse,
                success=False,
                message="Only ambassadors can perform this action.",
            )

        try:
            ambassador = await Ambassador.objects.select_related("user").aget(user=user)
        except Ambassador.DoesNotExist:
            return build_mutation_response(
                DisableAmbassadorResponse,
                success=False,
                message="Ambassador profile not found.",
            )

        try:
            @sync_to_async
            @transaction.atomic
            def disable_current_ambassador():
                ambassador.is_active = False
                ambassador.updated_by = user
                ambassador.save()

                ambassador_user = ambassador.user
                ambassador_user.is_active = False
                ambassador_user.updated_by = user
                ambassador_user.save()

                return ambassador

            ambassador = await disable_current_ambassador()

            return build_mutation_response(
                DisableAmbassadorResponse,
                success=True,
                message="Ambassador disabled successfully.",
                ambassador=ambassador,
            )
        except Exception as e:
            return build_mutation_response(
                DisableAmbassadorResponse,
                success=False,
                message=f"Error disabling ambassador: {str(e)}",
            )


class UpdateAmbassadorService(BaseAmbassadorService):
    """Service for updating ambassadors."""

    @classmethod
    async def update(
        cls,
        input: inputs.UpdateAmbassadorInput,
        info: strawberry.Info,
    ) -> UpdateAmbassadorResponse:
        """Update an ambassador (client/spark-admin only)."""
        user = info.context.request.user
        try:
            ambassador_id = resolve_id_to_int(input.ambassador_id)
            ambassador = await Ambassador.objects._by_id(ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                UpdateAmbassadorResponse,
                success=False,
                message="Ambassador not found.",
                input_obj=input,
            )

        try:
            # Update fields if provided
            @sync_to_async
            def update_ambassador():
                if input.address is not None:
                    ambassador.address = input.address
                if input.about_me is not None:
                    ambassador.about_me = input.about_me
                if input.coordinates is not None:
                    ambassador.coordinates = input.coordinates
                if input.is_active is not None:
                    ambassador.is_active = input.is_active
                ambassador.updated_by = user
                ambassador.save()
                return ambassador

            ambassador = await update_ambassador()

            return build_mutation_response(
                UpdateAmbassadorResponse,
                success=True,
                message="Ambassador updated successfully.",
                input_obj=input,
                ambassador=ambassador,
            )
        except Exception as e:
            return build_mutation_response(
                UpdateAmbassadorResponse,
                success=False,
                message=f"Error updating ambassador: {str(e)}",
                input_obj=input,
            )


class UpsertAmbassadorProfileService(BaseAmbassadorService):
    """Service to update ambassador and related profile data in one call."""

    @classmethod
    async def upsert(
        cls,
        input: inputs.UpsertAmbassadorProfileInput,
        info: strawberry.Info,
    ) -> UpsertAmbassadorProfileResponse:
        user = info.context.request.user

        ambassador = None
        if input.ambassador_id or input.ambassador_uuid:
            filters = {}
            if input.ambassador_id is not None:
                try:
                    filters["id"] = resolve_id_to_int(input.ambassador_id)
                except (ValueError, TypeError, GraphQLError):
                    return build_mutation_response(
                        UpsertAmbassadorProfileResponse,
                        success=False,
                        message="Invalid ambassador_id.",
                        input_obj=input,
                    )
            if input.ambassador_uuid is not None:
                filters["uuid"] = input.ambassador_uuid

            try:
                ambassador = await Ambassador.objects.select_related("user").aget(
                    **filters
                )
            except Ambassador.DoesNotExist:
                return build_mutation_response(
                    UpsertAmbassadorProfileResponse,
                    success=False,
                    message="Ambassador not found.",
                    input_obj=input,
                )
        else:
            # If no identifier is provided, reuse or create ambassador for the current user.
            ambassador = (
                await Ambassador.objects.select_related("user")
                .filter(user=user)
                .afirst()
            )
            if ambassador is None:
                ambassador = (
                    # Explicit for clarity; will create inside transaction.
                    None
                )

        role_slug = cls().get_role_slug(user)
        if ambassador and role_slug == "ambassador" and ambassador.user_id != user.id:
            return build_mutation_response(
                UpsertAmbassadorProfileResponse,
                success=False,
                message="Not authorized to update this ambassador.",
                input_obj=input,
            )

        tenant = None
        needs_tenant = input.notes is not None
        if needs_tenant:
            try:
                tenant = await cls().get_user_tenant(
                    info,
                    tenant_id=input.tenant_id,
                    tenant_uuid=None,
                    user=user,
                )
            except Exception as exc:
                return build_mutation_response(
                    UpsertAmbassadorProfileResponse,
                    success=False,
                    message=f"The tenant could not be resolved.: {exc}",
                    input_obj=input,
                )

        if input.notes:
            for note_input in input.notes:
                if not note_input.tenant_id and tenant is None:
                    return build_mutation_response(
                        UpsertAmbassadorProfileResponse,
                        success=False,
                        message="Notes require tenant_id.",
                        input_obj=input,
                    )

        @sync_to_async
        @transaction.atomic
        def _persist():
            nonlocal ambassador
            if ambassador is None:
                ambassador = Ambassador(
                    user=user,
                    created_by=user,
                    updated_by=user,
                )

            user_fields_to_update: list[str] = []
            if input.email is not None:
                normalized_email = input.email.strip().lower()
                if (
                    normalized_email
                    and User.objects.exclude(pk=ambassador.user_id)
                    .filter(email=normalized_email)
                    .exists()
                ):
                    raise GraphQLError("Email already exists.")
                ambassador.user.email = normalized_email
                user_fields_to_update.append("email")
            if input.first_name is not None:
                ambassador.user.first_name = input.first_name
                user_fields_to_update.append("first_name")
            if input.last_name is not None:
                ambassador.user.last_name = input.last_name
                user_fields_to_update.append("last_name")
            if user_fields_to_update:
                ambassador.user.updated_by = user
                ambassador.user.save(
                    update_fields=[*user_fields_to_update, "updated_by", "updated_at"]
                )

            if input.address is not None:
                ambassador.address = input.address
            if input.phone is not None:
                ambassador.phone = input.phone
            if input.about_me is not None:
                ambassador.about_me = input.about_me
            if input.image is not None:
                ambassador.user.image = input.image
                ambassador.user.updated_by = user
                ambassador.user.save(update_fields=["image", "updated_by", "updated_at"])
            if input.location_id is not None:
                ambassador.location_id = resolve_id_to_int(input.location_id)
            if input.t_shirt_size is not None:
                ambassador.t_shirt_size = input.t_shirt_size
            if input.coordinates is not None:
                ambassador.coordinates = input.coordinates
            if input.is_active is not None:
                ambassador.is_active = input.is_active
            if input.rating is not None:
                ambassador.rating = input.rating
            ambassador.updated_by = user
            ambassador.save()

            if input.files is not None:
                AmbassadorFile.objects.filter(ambassador=ambassador).delete()
                new_files = []
                for file_input in input.files:
                    new_files.append(
                        AmbassadorFile(
                            ambassador=ambassador,
                            name=file_input.name,
                            url=file_input.url,
                            main_resume=bool(file_input.main_resume),
                            profile_pic=bool(file_input.profile_pic),
                            is_public=bool(file_input.is_public),
                            file_type_id=(
                                resolve_id_to_int(file_input.file_type_id)
                                if file_input.file_type_id is not None
                                else None
                            ),
                            created_by=user,
                            updated_by=user,
                        )
                    )
                if new_files:
                    AmbassadorFile.objects.bulk_create(new_files, batch_size=50)

            if input.traits is not None:
                AmbassadorTrait.objects.filter(ambassador=ambassador).delete()
                new_traits = []
                for trait_input in input.traits:
                    new_traits.append(
                        AmbassadorTrait(
                            ambassador=ambassador,
                            user_id=resolve_id_to_int(trait_input.user_id),
                            created_by=user,
                            updated_by=user,
                        )
                    )
                if new_traits:
                    AmbassadorTrait.objects.bulk_create(new_traits, batch_size=50)

            if input.skills is not None:
                AmbassadorSkill.objects.filter(ambassador=ambassador).delete()
                new_skills = []
                seen_skill_ids: set[int] = set()
                for skill_input in input.skills:
                    skill_id_int = resolve_id_to_int(skill_input.skill_id)
                    if skill_id_int in seen_skill_ids:
                        continue
                    seen_skill_ids.add(skill_id_int)
                    new_skills.append(
                        AmbassadorSkill(
                            ambassador=ambassador,
                            skill_id=skill_id_int,
                            created_by=user,
                            updated_by=user,
                        )
                    )
                if new_skills:
                    AmbassadorSkill.objects.bulk_create(new_skills, batch_size=50)

            if input.notes is not None:
                AmbassadorNote.objects.filter(ambassador=ambassador).delete()
                new_notes = []
                for note_input in input.notes:
                    note_tenant_id = (
                        resolve_id_to_int(note_input.tenant_id)
                        if note_input.tenant_id is not None
                        else (tenant.id if tenant else None)
                    )
                    new_notes.append(
                        AmbassadorNote(
                            ambassador=ambassador,
                            tenant_id=note_tenant_id,
                            note=note_input.note,
                            created_by=user,
                            updated_by=user,
                        )
                    )
                if new_notes:
                    AmbassadorNote.objects.bulk_create(new_notes, batch_size=50)

            if input.work_history is not None:
                AmbassadorWorkHistory.objects.filter(ambassador=ambassador).delete()
                new_work = []
                for work_input in input.work_history:
                    new_work.append(
                        AmbassadorWorkHistory(
                            ambassador=ambassador,
                            user_id=resolve_id_to_int(work_input.user_id),
                            created_by=user,
                            updated_by=user,
                        )
                    )
                if new_work:
                    AmbassadorWorkHistory.objects.bulk_create(new_work, batch_size=50)

            return ambassador

        try:
            ambassador = await _persist()
        except GraphQLError as exc:
            return build_mutation_response(
                UpsertAmbassadorProfileResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
        except (ValueError, TypeError):
            return build_mutation_response(
                UpsertAmbassadorProfileResponse,
                success=False,
                message="One or more IDs are invalid.",
                input_obj=input,
            )

        async def fetch_reviews():
            qs = AmbassadorReview.objects.filter(ambassador_id=ambassador.id)
            return await sync_to_async(list)(qs)

        async def fetch_files():
            qs = AmbassadorFile.objects.select_related("file_type").filter(
                ambassador_id=ambassador.id
            )
            return await sync_to_async(list)(qs)

        async def fetch_traits():
            qs = AmbassadorTrait.objects.filter(ambassador_id=ambassador.id)
            return await sync_to_async(list)(qs)

        async def fetch_skills():
            qs = AmbassadorSkill.objects.select_related("skill").filter(
                ambassador_id=ambassador.id
            )
            return await sync_to_async(list)(qs)

        async def fetch_notes():
            qs = AmbassadorNote.objects.filter(ambassador_id=ambassador.id)
            return await sync_to_async(list)(qs)

        async def fetch_work_history():
            qs = AmbassadorWorkHistory.objects.filter(ambassador_id=ambassador.id)
            return await sync_to_async(list)(qs)

        (
            reviews,
            files,
            traits,
            skills,
            notes,
            work_history,
        ) = await asyncio.gather(
            fetch_reviews(),
            fetch_files(),
            fetch_traits(),
            fetch_skills(),
            fetch_notes(),
            fetch_work_history(),
        )

        profile = AmbassadorProfile(
            ambassador=ambassador,
            reviews=reviews,
            files=files,
            traits=traits,
            skills=skills,
            notes=notes,
            work_history=work_history,
        )

        return build_mutation_response(
            UpsertAmbassadorProfileResponse,
            success=True,
            message="Ambassador profile saved.",
            input_obj=input,
            profile=profile,
        )


class DeleteInvitationService(BaseAmbassadorService):
    """Service for deleting invitations."""

    @classmethod
    async def delete(
        cls,
        input: inputs.DeleteInvitationInput,
        info: strawberry.Info,
    ) -> DeleteInvitationResponse:
        """Delete an invitation (client/spark-admin only)."""
        user = info.context.request.user

        # Validate user has permission (client or spark-admin)
        # No need to validate because we have IsClientOrSparkAdmin permission class

        try:
            invitation_id = resolve_id_to_int(input.invitation_id)
            invitation = await AmbassadorInvitation.objects._by_id(invitation_id)
        except (AmbassadorInvitation.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                DeleteInvitationResponse,
                success=False,
                message="Invitation not found.",
                input_obj=input,
            )

        # Warn if invitation is used, but allow deletion
        if invitation.is_used:
            # Still allow deletion but log it
            pass

        try:
            await invitation._delete()

            return build_mutation_response(
                DeleteInvitationResponse,
                success=True,
                message="Invitation deleted successfully.",
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                DeleteInvitationResponse,
                success=False,
                message=f"Error deleting invitation: {str(e)}",
                input_obj=input,
            )


class AmbassadorInvitationQueriesService(SparkGraphQLMixin):
    """Service for ambassador invitation queries."""

    async def get_sent_invitations(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorInvitationFiltersInput | None = None,
    ) -> CountableConnection[AmbassadorInvitationType]:
        """Get sent invitations for a tenant (client/spark-admin only)."""
        user = await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)

        # Resolve tenant
        tenant_id: int | None = None
        tenant_id_input = filters.tenant_id if filters else None
        tenant_uuid_input = filters.tenant_uuid if filters else None

        should_filter_by_tenant = (
            not is_spark_request
            or tenant_id_input is not None
            or tenant_uuid_input is not None
        )
        if should_filter_by_tenant:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=tenant_id_input,
                tenant_uuid=tenant_uuid_input,
                user=user,
            )
            tenant_id = tenant.id

        queryset = await self._get_filtered_invitations_queryset(tenant_id, filters)

        from utils.graphql.relay import connection_from_queryset_async

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=10,
            max_limit=50,
        )

    async def _get_filtered_invitations_queryset(
        self,
        tenant_id: int | None = None,
        filters: inputs.AmbassadorInvitationFiltersInput | None = None,
    ):
        """Get filtered queryset for invitations."""

        @sync_to_async
        def get_queryset():
            queryset = AmbassadorInvitation.objects.select_related(
                "tenant", "invited_by"
            )

            # Filter by tenant
            if tenant_id:
                queryset = queryset.filter(tenant_id=tenant_id)

            if filters:
                if filters.job_id:
                    try:
                        job_id = resolve_id_to_int(filters.job_id)
                    except (TypeError, ValueError, GraphQLError) as exc:
                        raise GraphQLError("Invalid job ID.") from exc
                    queryset = queryset.filter(job_id=job_id)

                if filters.job_uuid:
                    queryset = queryset.filter(job__uuid=filters.job_uuid)

                # Filter by expired status
                if filters.is_expired is not None:
                    now = timezone.now()
                    if filters.is_expired:
                        queryset = queryset.filter(expires_at__lt=now)
                    else:
                        queryset = queryset.filter(expires_at__gte=now)

                # Filter by used status
                if filters.is_used is not None:
                    queryset = queryset.filter(is_used=filters.is_used)

                # Search by email
                if filters.email:
                    queryset = queryset.filter(email__icontains=filters.email)

                # General search (email or invited_by name)
                if filters.search:
                    queryset = queryset.filter(
                        Q(email__icontains=filters.search)
                        | Q(invited_by__first_name__icontains=filters.search)
                        | Q(invited_by__last_name__icontains=filters.search)
                    )

            return queryset.order_by("-created_at")

        return await get_queryset()

    async def get_invited_groups_by_job(
        self,
        info: strawberry.Info,
        filters: inputs.AmbassadorGroupFiltersInput | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection["AmbassadorGroup"]:
        """Return groups that contain ambassadors invited to the given job."""
        user = await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)

        job_id = filters.job_id if filters else None
        if not job_id:
            raise GraphQLError("Job ID is required in filters.")

        try:
            resolved_job_id = resolve_id_to_int(job_id)
        except (TypeError, ValueError, GraphQLError) as exc:
            raise GraphQLError("Invalid job ID.") from exc

        tenant_id: int | None = None
        if not is_spark_request:
            tenant = await self.get_user_tenant(info, user=user)
            tenant_id = tenant.id

        @sync_to_async
        def get_queryset():
            queryset = AmbassadorGroup.objects.select_related("group_type").prefetch_related(
                "members",
                "members__user",
                "members__ambassador",
            )

            if tenant_id is not None:
                queryset = queryset.filter(tenant_id=tenant_id)

            return queryset.filter(
                job_links__job_id=resolved_job_id
            ).distinct().order_by("-created_at")

        queryset = await get_queryset()

        from utils.graphql.relay import connection_from_queryset_async

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=10,
            max_limit=50,
        )


class AmbassadorQueriesService(SparkGraphQLMixin):
    """Service for ambassador queries."""

    async def get_available_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
    ) -> CountableConnection[AmbassadorType]:
        """Get available ambassadors for a tenant (client/spark-admin only)."""
        user = await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)

        # Resolve tenant
        tenant_id: int | None = None
        tenant_id_input = filters.tenant_id if filters else None
        tenant_uuid_input = filters.tenant_uuid if filters else None

        should_filter_by_tenant = (
            not is_spark_request
            or tenant_id_input is not None
            or tenant_uuid_input is not None
        )
        if should_filter_by_tenant:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=tenant_id_input,
                tenant_uuid=tenant_uuid_input,
                user=user,
            )
            tenant_id = tenant.id

        queryset = await self._get_filtered_ambassadors_queryset(tenant_id, filters)

        from utils.graphql.relay import connection_from_queryset_async

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=10,
            max_limit=50,
        )

    async def get_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
    ) -> CountableConnection[AmbassadorType]:
        """General ambassador list with filters for active status, rating, name, and email."""
        user = await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)

        tenant_id: int | None = None
        tenant_id_input = filters.tenant_id if filters else None
        tenant_uuid_input = filters.tenant_uuid if filters else None

        should_filter_by_tenant = (
            not is_spark_request
            or tenant_id_input is not None
            or tenant_uuid_input is not None
        )
        if should_filter_by_tenant:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=tenant_id_input,
                tenant_uuid=tenant_uuid_input,
                user=user,
            )
            tenant_id = tenant.id

        queryset = await self._get_filtered_ambassadors_queryset(
            tenant_id=tenant_id,
            filters=filters,
            q=q,
        )

        from utils.graphql.relay import connection_from_queryset_async

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=10,
            max_limit=50,
        )

    async def _get_filtered_ambassadors_queryset(
        self,
        tenant_id: int | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
        q: str | None = None,
    ):
        """Get filtered queryset for ambassadors."""

        @sync_to_async
        def get_queryset():
            queryset = Ambassador.objects.select_related("user")

            # Filter by tenant via TenantedUser
            if tenant_id:
                queryset = queryset.filter(
                    user__tenanted_users__tenant_id=tenant_id,
                    user__tenanted_users__is_active=True,
                ).distinct()

            if filters:
                # Filter by active status
                if filters.is_active is not None:
                    queryset = queryset.filter(is_active=filters.is_active)

                if filters.rating_min is not None:
                    queryset = queryset.filter(rating__gte=filters.rating_min)
                if filters.rating_max is not None:
                    queryset = queryset.filter(rating__lte=filters.rating_max)

                # Search by user email
                if filters.email:
                    queryset = queryset.filter(user__email__icontains=filters.email)

                # Search by user name
                if filters.name:
                    queryset = queryset.filter(
                        Q(user__first_name__icontains=filters.name)
                        | Q(user__last_name__icontains=filters.name)
                    )

                # Search by address
                if filters.address:
                    queryset = queryset.filter(address__icontains=filters.address)
                if filters.about_me:
                    queryset = queryset.filter(about_me__icontains=filters.about_me)

                # General search across email, name, address and about_me
                if filters.search:
                    queryset = queryset.filter(
                        Q(user__email__icontains=filters.search)
                        | Q(user__first_name__icontains=filters.search)
                        | Q(user__last_name__icontains=filters.search)
                        | Q(address__icontains=filters.search)
                        | Q(about_me__icontains=filters.search)
                    )

            if q:
                queryset = queryset.filter(
                    Q(user__email__icontains=q)
                    | Q(user__first_name__icontains=q)
                    | Q(user__last_name__icontains=q)
                    | Q(address__icontains=q)
                    | Q(about_me__icontains=q)
                )

            return queryset.order_by("-created_at")

        return await get_queryset()

    async def get_active_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: inputs.ActiveAmbassadorFiltersInput | None = None,
    ) -> CountableConnection[AmbassadorType]:
        """Get all active ambassadors."""

        @sync_to_async
        def get_queryset():
            queryset = Ambassador.objects.select_related("user").filter(is_active=True)

            if filters:
                if filters.email:
                    queryset = queryset.filter(user__email__icontains=filters.email)
                if filters.name:
                    queryset = queryset.filter(
                        Q(user__first_name__icontains=filters.name)
                        | Q(user__last_name__icontains=filters.name)
                    )
            if q:
                queryset = queryset.filter(
                    Q(user__email__icontains=q)
                    | Q(user__first_name__icontains=q)
                    | Q(user__last_name__icontains=q)
                    | Q(address__icontains=q)
                    | Q(about_me__icontains=q)
                )

            return queryset.order_by("-created_at")

        queryset = await get_queryset()

        from utils.graphql.relay import connection_from_queryset_async

        no_pagination = all(value is None for value in (first, after, last, before))
        default_limit = sys.maxsize if no_pagination else 10
        max_limit = sys.maxsize if no_pagination else 50

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=default_limit,
            max_limit=max_limit,
        )


class CreateAmbassadorReviewService(BaseAmbassadorService):
    """Service for creating ambassador reviews."""

    @classmethod
    async def create(
        cls,
        input: inputs.CreateAmbassadorReviewInput,
        info: strawberry.Info,
    ) -> CreateAmbassadorReviewResponse:
        """Create an ambassador review (client/spark-admin only)."""
        user = info.context.request.user

        # Validate score range if provided
        if input.score is not None:
            if input.score < 1 or input.score > 5:
                return build_mutation_response(
                    CreateAmbassadorReviewResponse,
                    success=False,
                    message="Score must be between 1 and 5.",
                    input_obj=input,
                )

        # Validate ambassador exists
        try:
            ambassador_id = resolve_id_to_int(input.ambassador_id)
            ambassador = await Ambassador.objects._by_id(ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                CreateAmbassadorReviewResponse,
                success=False,
                message="Ambassador not found.",
                input_obj=input,
            )

        # Validate client exists if provided
        client = None
        if input.client_id:
            try:
                client = await Client.objects._get(pk=int(input.client_id))
            except (Client.DoesNotExist, ValueError, TypeError):
                return build_mutation_response(
                    CreateAmbassadorReviewResponse,
                    success=False,
                    message="Client not found.",
                    input_obj=input,
                )

        # Resolve tenant
        service_instance = cls()
        tenant = await service_instance.get_user_tenant(
            info,
            tenant_id=input.tenant_id,
            tenant_uuid=None,
            user=user,
        )

        # Check for duplicate review (same client + ambassador)
        if client:
            if await AmbassadorReview.objects._exists_by_ambassador_and_client(
                ambassador.id,
                client.id,
            ):
                return build_mutation_response(
                    CreateAmbassadorReviewResponse,
                    success=False,
                    message="A review for this ambassador and client already exists.",
                    input_obj=input,
                )

        # Create review
        try:
            review = await AmbassadorReview.objects._create(
                ambassador=ambassador,
                client=client,
                tenant=tenant,
                review=input.review,
                score=input.score,
                created_by=user,
                updated_by=user,
            )

            return build_mutation_response(
                CreateAmbassadorReviewResponse,
                success=True,
                message="Review created successfully.",
                input_obj=input,
                ambassador_review=review,
            )
        except Exception as e:
            return build_mutation_response(
                CreateAmbassadorReviewResponse,
                success=False,
                message=f"Error creating review: {str(e)}",
                input_obj=input,
            )


class UpdateAmbassadorReviewService(BaseAmbassadorService):
    """Service for updating ambassador reviews."""

    @classmethod
    async def update(
        cls,
        input: inputs.UpdateAmbassadorReviewInput,
        info: strawberry.Info,
    ) -> UpdateAmbassadorReviewResponse:
        """Update an ambassador review (client/spark-admin only)."""
        user = info.context.request.user

        # Validate review exists
        try:
            review_id = resolve_id_to_int(input.review_id)
            review = await AmbassadorReview.objects._by_id(review_id)
        except (AmbassadorReview.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                UpdateAmbassadorReviewResponse,
                success=False,
                message="Review not found.",
                input_obj=input,
            )

        # Validate score range if provided
        if input.score is not None:
            if input.score < 1 or input.score > 5:
                return build_mutation_response(
                    UpdateAmbassadorReviewResponse,
                    success=False,
                    message="Score must be between 1 and 5.",
                    input_obj=input,
                )

        # Update fields if provided
        try:
            if input.review is not None:
                review.review = input.review
            if input.score is not None:
                review.score = input.score
            review.updated_by = user
            await review._save()

            return build_mutation_response(
                UpdateAmbassadorReviewResponse,
                success=True,
                message="Review updated successfully.",
                input_obj=input,
                ambassador_review=review,
            )
        except Exception as e:
            return build_mutation_response(
                UpdateAmbassadorReviewResponse,
                success=False,
                message=f"Error updating review: {str(e)}",
                input_obj=input,
            )


class DeleteAmbassadorReviewService(BaseAmbassadorService):
    """Service for deleting ambassador reviews."""

    @classmethod
    async def delete(
        cls,
        input: inputs.DeleteAmbassadorReviewInput,
        info: strawberry.Info,
    ) -> DeleteAmbassadorReviewResponse:
        """Delete an ambassador review (client/spark-admin only)."""
        user = info.context.request.user

        # Validate review exists
        try:
            review_id = resolve_id_to_int(input.review_id)
            review = await AmbassadorReview.objects._by_id(review_id)
        except (AmbassadorReview.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                DeleteAmbassadorReviewResponse,
                success=False,
                message="Review not found.",
                input_obj=input,
            )

        try:
            await review._delete()

            return build_mutation_response(
                DeleteAmbassadorReviewResponse,
                success=True,
                message="Review deleted successfully.",
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                DeleteAmbassadorReviewResponse,
                success=False,
                message=f"Error deleting review: {str(e)}",
                input_obj=input,
            )


class AmbassadorReviewQueriesService(SparkGraphQLMixin):
    """Service for ambassador review queries."""

    async def get_ambassador_reviews(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorReviewFiltersInput | None = None,
    ) -> CountableConnection:
        """Get ambassador reviews with filters (authenticated users only)."""
        user = await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)

        # Resolve tenant
        tenant_id: int | None = None
        tenant_id_input = filters.tenant_id if filters else None
        tenant_uuid_input = filters.tenant_uuid if filters else None

        should_filter_by_tenant = (
            not is_spark_request
            or tenant_id_input is not None
            or tenant_uuid_input is not None
        )
        if should_filter_by_tenant:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=tenant_id_input,
                tenant_uuid=tenant_uuid_input,
                user=user,
            )
            tenant_id = tenant.id

        queryset = await self._get_filtered_reviews_queryset(tenant_id, filters)

        from utils.graphql.relay import connection_from_queryset_async
        from ambassadors.types import AmbassadorReviewType

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=10,
            max_limit=50,
        )

    async def _get_filtered_reviews_queryset(
        self,
        tenant_id: int | None = None,
        filters: inputs.AmbassadorReviewFiltersInput | None = None,
    ):
        """Get filtered queryset for reviews."""

        @sync_to_async
        def get_queryset():
            queryset = AmbassadorReview.objects.select_related(
                "ambassador", "client", "tenant"
            )

            # Filter by tenant
            if tenant_id:
                queryset = queryset.filter(tenant_id=tenant_id)

            if filters:
                # Filter by ambassador
                if filters.ambassador_id:
                    queryset = queryset.filter(ambassador_id=int(filters.ambassador_id))

                # Filter by client
                if filters.client_id:
                    queryset = queryset.filter(client_id=int(filters.client_id))

                # Filter by score range
                if filters.min_score is not None:
                    queryset = queryset.filter(score__gte=filters.min_score)
                if filters.max_score is not None:
                    queryset = queryset.filter(score__lte=filters.max_score)

                # Filter by date range
                if filters.start_date:
                    try:
                        start_datetime = datetime.fromisoformat(
                            filters.start_date.replace("Z", "+00:00")
                        )
                        queryset = queryset.filter(created_at__gte=start_datetime)
                    except (ValueError, AttributeError):
                        pass  # Invalid date format, skip filter
                if filters.end_date:
                    try:
                        end_datetime = datetime.fromisoformat(
                            filters.end_date.replace("Z", "+00:00")
                        )
                        queryset = queryset.filter(created_at__lte=end_datetime)
                    except (ValueError, AttributeError):
                        pass  # Invalid date format, skip filter

                # Search in review text
                if filters.search:
                    queryset = queryset.filter(review__icontains=filters.search)

            return queryset.order_by("-created_at")

        return await get_queryset()


class CreateAmbassadorNoteService(BaseAmbassadorService):
    """Service for creating ambassador notes."""

    @classmethod
    async def create(
        cls,
        input: inputs.CreateAmbassadorNoteInput,
        info: strawberry.Info,
    ) -> CreateAmbassadorNoteResponse:
        """Create an ambassador note (authenticated users only)."""
        user = info.context.request.user

        # Validate ambassador exists
        try:
            ambassador_id = resolve_id_to_int(input.ambassador_id)
            ambassador = await Ambassador.objects._by_id(ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                CreateAmbassadorNoteResponse,
                success=False,
                message="Ambassador not found.",
                input_obj=input,
            )

        # Resolve tenant
        service_instance = cls()
        tenant = await service_instance.get_user_tenant(
            info,
            tenant_id=input.tenant_id,
            tenant_uuid=None,
            user=user,
        )

        # Create note
        try:
            note = await AmbassadorNote.objects.acreate(
                ambassador=ambassador,
                tenant=tenant,
                note=input.note,
                created_by=user,
                updated_by=user,
            )

            return build_mutation_response(
                CreateAmbassadorNoteResponse,
                success=True,
                message="Note created successfully.",
                input_obj=input,
                ambassador_note=note,
            )
        except Exception as e:
            return build_mutation_response(
                CreateAmbassadorNoteResponse,
                success=False,
                message=f"Error creating note: {str(e)}",
                input_obj=input,
            )


class UpdateAmbassadorNoteService(BaseAmbassadorService):
    """Service for updating ambassador notes."""

    @classmethod
    async def update(
        cls,
        input: inputs.UpdateAmbassadorNoteInput,
        info: strawberry.Info,
    ) -> UpdateAmbassadorNoteResponse:
        """Update an ambassador note (authenticated users only)."""
        user = info.context.request.user

        # Validate note exists
        try:
            note = await AmbassadorNote.objects.aget(id=int(input.note_id))
        except (AmbassadorNote.DoesNotExist, ValueError, TypeError):
            return build_mutation_response(
                UpdateAmbassadorNoteResponse,
                success=False,
                message="Note not found.",
                input_obj=input,
            )

        # Update fields if provided
        try:
            if input.note is not None:
                note.note = input.note
            note.updated_by = user
            await sync_to_async(note.save)()

            return build_mutation_response(
                UpdateAmbassadorNoteResponse,
                success=True,
                message="Note updated successfully.",
                input_obj=input,
                ambassador_note=note,
            )
        except Exception as e:
            return build_mutation_response(
                UpdateAmbassadorNoteResponse,
                success=False,
                message=f"Error updating note: {str(e)}",
                input_obj=input,
            )


class DeleteAmbassadorNoteService(BaseAmbassadorService):
    """Service for deleting ambassador notes."""

    @classmethod
    async def delete(
        cls,
        input: inputs.DeleteAmbassadorNoteInput,
        info: strawberry.Info,
    ) -> DeleteAmbassadorNoteResponse:
        """Delete an ambassador note (authenticated users only)."""
        user = info.context.request.user

        # Validate note exists
        try:
            note = await AmbassadorNote.objects.aget(id=int(input.note_id))
        except (AmbassadorNote.DoesNotExist, ValueError, TypeError):
            return build_mutation_response(
                DeleteAmbassadorNoteResponse,
                success=False,
                message="Note not found.",
                input_obj=input,
            )

        try:
            await sync_to_async(note.delete)()

            return build_mutation_response(
                DeleteAmbassadorNoteResponse,
                success=True,
                message="Note deleted successfully.",
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                DeleteAmbassadorNoteResponse,
                success=False,
                message=f"Error deleting note: {str(e)}",
                input_obj=input,
            )


class AmbassadorNoteQueriesService(SparkGraphQLMixin):
    """Service for ambassador note queries."""

    async def get_ambassador_notes(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorNoteFiltersInput | None = None,
    ) -> CountableConnection:
        """Get ambassador notes with filters (authenticated users only)."""
        user = await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)

        # Resolve tenant
        tenant_id: int | None = None
        tenant_id_input = filters.tenant_id if filters else None
        tenant_uuid_input = filters.tenant_uuid if filters else None

        should_filter_by_tenant = (
            not is_spark_request
            or tenant_id_input is not None
            or tenant_uuid_input is not None
        )
        if should_filter_by_tenant:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=tenant_id_input,
                tenant_uuid=tenant_uuid_input,
                user=user,
            )
            tenant_id = tenant.id

        queryset = await self._get_filtered_notes_queryset(tenant_id, filters)

        from utils.graphql.relay import connection_from_queryset_async
        from ambassadors.types import AmbassadorNoteType

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=10,
            max_limit=50,
        )

    async def _get_filtered_notes_queryset(
        self,
        tenant_id: int | None = None,
        filters: inputs.AmbassadorNoteFiltersInput | None = None,
    ):
        """Get filtered queryset for notes."""

        @sync_to_async
        def get_queryset():
            queryset = AmbassadorNote.objects.select_related(
                "ambassador", "tenant", "created_by", "updated_by"
            )

            # Filter by tenant
            if tenant_id:
                queryset = queryset.filter(tenant_id=tenant_id)

            if filters:
                # Filter by ambassador
                if filters.ambassador_id:
                    queryset = queryset.filter(ambassador_id=int(filters.ambassador_id))

                # Filter by created_by
                if filters.created_by_id:
                    queryset = queryset.filter(created_by_id=int(filters.created_by_id))

                # Filter by date range
                if filters.start_date:
                    try:
                        start_datetime = datetime.fromisoformat(
                            filters.start_date.replace("Z", "+00:00")
                        )
                        queryset = queryset.filter(created_at__gte=start_datetime)
                    except (ValueError, AttributeError):
                        pass  # Invalid date format, skip filter
                if filters.end_date:
                    try:
                        end_datetime = datetime.fromisoformat(
                            filters.end_date.replace("Z", "+00:00")
                        )
                        queryset = queryset.filter(created_at__lte=end_datetime)
                    except (ValueError, AttributeError):
                        pass  # Invalid date format, skip filter

                # Search in note text
                if filters.search:
                    queryset = queryset.filter(note__icontains=filters.search)

            return queryset.order_by("-created_at")

        return await get_queryset()


class CreateSkillService(BaseAmbassadorService):
    """Service for creating skills."""

    @classmethod
    async def create(
        cls,
        input: inputs.CreateSkillInput,
        info: strawberry.Info,
    ) -> CreateSkillResponse:
        """Create a skill (authenticated users only)."""
        user = info.context.request.user

        # Create skill
        try:
            skill = await Skill.objects._create(
                name=input.name,
                created_by=user,
                updated_by=user,
            )

            return build_mutation_response(
                CreateSkillResponse,
                success=True,
                message="Skill created successfully.",
                input_obj=input,
                skill=skill,
            )
        except Exception as e:
            return build_mutation_response(
                CreateSkillResponse,
                success=False,
                message=f"Error creating skill: {str(e)}",
                input_obj=input,
            )


class UpdateSkillService(BaseAmbassadorService):
    """Service for updating skills."""

    @classmethod
    async def update(
        cls,
        input: inputs.UpdateSkillInput,
        info: strawberry.Info,
    ) -> UpdateSkillResponse:
        """Update a skill (authenticated users only)."""
        user = info.context.request.user

        # Validate skill exists
        try:
            skill_id = resolve_id_to_int(input.id)
            skill = await Skill.objects._by_id(skill_id)
        except (Skill.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                UpdateSkillResponse,
                success=False,
                message="Skill not found.",
                input_obj=input,
            )

        # Update fields if provided
        try:
            if input.name is not None:
                skill.name = input.name
            skill.updated_by = user
            await skill._save()

            return build_mutation_response(
                UpdateSkillResponse,
                success=True,
                message="Skill updated successfully.",
                input_obj=input,
                skill=skill,
            )
        except Exception as e:
            return build_mutation_response(
                UpdateSkillResponse,
                success=False,
                message=f"Error updating skill: {str(e)}",
                input_obj=input,
            )


class DeleteSkillService(BaseAmbassadorService):
    """Service for deleting skills."""

    @classmethod
    async def delete(
        cls,
        input: inputs.DeleteSkillInput,
        info: strawberry.Info,
    ) -> DeleteSkillResponse:
        """Delete a skill (authenticated users only)."""
        user = info.context.request.user

        # Validate skill exists
        try:
            skill_id = resolve_id_to_int(input.id)
            skill = await Skill.objects._by_id(skill_id)
        except (Skill.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                DeleteSkillResponse,
                success=False,
                message="Skill not found.",
                input_obj=input,
            )

        try:
            await skill._delete()

            return build_mutation_response(
                DeleteSkillResponse,
                success=True,
                message="Skill deleted successfully.",
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                DeleteSkillResponse,
                success=False,
                message=f"Error deleting skill: {str(e)}",
                input_obj=input,
            )


class SkillQueriesService(SparkGraphQLMixin):
    """Service for skill queries."""

    async def get_skills(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.SkillFiltersInput | None = None,
    ) -> CountableConnection:
        """Get skills with filters (authenticated users only)."""
        user = await self.get_user(info)
        role_slug = self.get_role_slug(user)

        # Resolve tenant
        tenant_id: int | None = None
        tenant_id_input = filters.tenant_id if filters else None
        tenant_uuid_input = filters.tenant_uuid if filters else None

        if role_slug == "client":
            tenant = await self.get_user_tenant(
                info,
                tenant_id=tenant_id_input,
                tenant_uuid=tenant_uuid_input,
                user=user,
            )
            tenant_id = tenant.id
        elif tenant_id_input is not None or tenant_uuid_input is not None:
            tenant = await self._get_tenant_without_membership(
                tenant_id=tenant_id_input,
                tenant_uuid=tenant_uuid_input,
            )
            tenant_id = tenant.id

        queryset = await self._get_filtered_skills_queryset(tenant_id, filters)

        from utils.graphql.relay import connection_from_queryset_async
        from ambassadors.types import SkillType

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=10,
            max_limit=50,
        )

    async def _get_filtered_skills_queryset(
        self,
        tenant_id: int | None = None,
        filters: inputs.SkillFiltersInput | None = None,
    ):
        """Get filtered queryset for skills."""

        @sync_to_async
        def get_queryset():
            queryset = Skill.objects.all()

            # Skill is global; tenant filter scopes through ambassador-skill assignments.
            if tenant_id:
                queryset = queryset.filter(
                    ambassadors_skills__ambassador__user__tenanted_users__tenant_id=tenant_id,
                    ambassadors_skills__ambassador__user__tenanted_users__is_active=True,
                ).distinct()

            if filters:
                # Search in name
                if filters.search:
                    queryset = queryset.filter(name__icontains=filters.search)

            return queryset.order_by("name")

        return await get_queryset()


class CreateAmbassadorSkillService(BaseAmbassadorService):
    """Service for creating ambassador skills."""

    @classmethod
    async def create(
        cls,
        input: inputs.CreateAmbassadorSkillInput,
        info: strawberry.Info,
    ) -> CreateAmbassadorSkillResponse:
        """Create an ambassador skill (client/spark-admin only)."""
        user = info.context.request.user

        # Validate ambassador exists
        try:
            ambassador_id = resolve_id_to_int(input.ambassador_id)
            ambassador = await Ambassador.objects._by_id(ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                CreateAmbassadorSkillResponse,
                success=False,
                message="Ambassador not found.",
                input_obj=input,
            )

        # Validate skill exists
        try:
            skill_id = resolve_id_to_int(input.skill_id)
            skill = await Skill.objects._by_id(skill_id)
        except (Skill.DoesNotExist, ValueError, TypeError, GraphQLError):
            return build_mutation_response(
                CreateAmbassadorSkillResponse,
                success=False,
                message="Skill not found.",
                input_obj=input,
            )

        # Resolve tenant
        service_instance = cls()
        tenant = await service_instance.get_user_tenant(
            info,
            tenant_id=input.tenant_id,
            tenant_uuid=None,
            user=user,
        )

        # Validate ambassador and skill belong to same tenant
        # Check if ambassador's user belongs to the resolved tenant
        @sync_to_async
        def check_ambassador_tenant():
            return TenantedUser.objects.filter(
                user=ambassador.user,
                tenant=tenant,
                is_active=True,
            ).exists()

        ambassador_belongs_to_tenant = await check_ambassador_tenant()
        if not ambassador_belongs_to_tenant:
            return build_mutation_response(
                CreateAmbassadorSkillResponse,
                success=False,
                message="Ambassador must belong to the selected tenant.",
                input_obj=input,
            )

        # Check for duplicate (ambassador + skill combination)
        if await AmbassadorSkill.objects._exists_by_ambassador_and_skill(
            ambassador.id,
            skill.id,
        ):
            return build_mutation_response(
                CreateAmbassadorSkillResponse,
                success=False,
                message="This ambassador already has this skill.",
                input_obj=input,
            )

        # Create ambassador skill
        try:
            ambassador_skill = await AmbassadorSkill.objects._create(
                ambassador=ambassador,
                skill=skill,
                created_by=user,
                updated_by=user,
            )

            return build_mutation_response(
                CreateAmbassadorSkillResponse,
                success=True,
                message="Ambassador skill created successfully.",
                input_obj=input,
                ambassador_skill=ambassador_skill,
            )
        except Exception as e:
            return build_mutation_response(
                CreateAmbassadorSkillResponse,
                success=False,
                message=f"Error creating ambassador skill: {str(e)}",
                input_obj=input,
            )


class DeleteAmbassadorSkillService(BaseAmbassadorService):
    """Service for deleting ambassador skills."""

    @classmethod
    async def delete(
        cls,
        input: inputs.DeleteAmbassadorSkillInput,
        info: strawberry.Info,
    ) -> DeleteAmbassadorSkillResponse:
        """Delete an ambassador skill (client/spark-admin only)."""
        user = info.context.request.user

        # Validate ambassador skill exists
        try:
            ambassador_skill = await AmbassadorSkill.objects._by_id(
                input.ambassador_skill_id
            )
        except (AmbassadorSkill.DoesNotExist, ValueError, TypeError):
            return build_mutation_response(
                DeleteAmbassadorSkillResponse,
                success=False,
                message="Ambassador skill not found.",
                input_obj=input,
            )

        try:
            await ambassador_skill._delete()

            return build_mutation_response(
                DeleteAmbassadorSkillResponse,
                success=True,
                message="Ambassador skill deleted successfully.",
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                DeleteAmbassadorSkillResponse,
                success=False,
                message=f"Error deleting ambassador skill: {str(e)}",
                input_obj=input,
            )


class AmbassadorSkillQueriesService(SparkGraphQLMixin):
    """Service for ambassador skill queries."""

    async def get_ambassador_skills(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorSkillFiltersInput | None = None,
    ) -> CountableConnection:
        """Get ambassador skills with filters (authenticated users only)."""
        user = await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)

        # Resolve tenant
        tenant_id: int | None = None
        tenant_id_input = filters.tenant_id if filters else None
        tenant_uuid_input = filters.tenant_uuid if filters else None

        should_filter_by_tenant = (
            not is_spark_request
            or tenant_id_input is not None
            or tenant_uuid_input is not None
        )
        if should_filter_by_tenant:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=tenant_id_input,
                tenant_uuid=tenant_uuid_input,
                user=user,
            )
            tenant_id = tenant.id

        queryset = await self._get_filtered_ambassador_skills_queryset(
            tenant_id, filters
        )

        from utils.graphql.relay import connection_from_queryset_async
        from ambassadors.types import AmbassadorSkillType

        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            default_limit=10,
            max_limit=50,
        )

    async def _get_filtered_ambassador_skills_queryset(
        self,
        tenant_id: int | None = None,
        filters: inputs.AmbassadorSkillFiltersInput | None = None,
    ):
        """Get filtered queryset for ambassador skills."""

        @sync_to_async
        def get_queryset():
            queryset = AmbassadorSkill.objects.select_related(
                "ambassador", "skill"
            )

            # Filter by tenant
            if tenant_id:
                queryset = queryset.filter(
                    ambassador__user__tenanted_users__tenant_id=tenant_id,
                    ambassador__user__tenanted_users__is_active=True,
                ).distinct()

            if filters:
                # Filter by ambassador
                if filters.ambassador_id:
                    queryset = queryset.filter(ambassador_id=int(filters.ambassador_id))

                # Filter by skill
                if filters.skill_id:
                    queryset = queryset.filter(skill_id=int(filters.skill_id))

            return queryset.order_by("-created_at")

        return await get_queryset()


class GroupTypeMutationService(BaseMutationService):
    """Service for creating group types (client/spark-admin only)."""

    response_class = GroupTypeResponse
    model_field_name = "group_type"

    def get_model(self) -> GroupType:
        """Get the model for the service."""
        return GroupType


class AmbassadorGroupMutationService(BaseMutationService):
    """Service for creating ambassador groups (client/spark-admin only)."""

    response_class = AmbassadorGroupResponse
    model_field_name = "ambassador_group"

    def get_model(self) -> AmbassadorGroup:
        """Get the model for the service."""
        return AmbassadorGroup

    @staticmethod
    def _upsert_group_job_link(
        *,
        group: AmbassadorGroup,
        job: job_models.Job,
        user,
    ) -> None:
        """Ensure group-job link exists and keep one explicit job per group."""
        AmbassadorGroupJob.objects.filter(group=group).exclude(job=job).delete()
        AmbassadorGroupJob.objects.update_or_create(
            group=group,
            job=job,
            defaults={
                "tenant": group.tenant,
                "created_by": user,
                "updated_by": user,
            },
        )

    @classmethod
    async def create(
        cls,
        input: SparkGraphQLInput,
        info: strawberry.Info,
        *,
        response_class: type | None = None,
        model_field_name: str | None = None,
        create_message: str | None = None,
    ) -> Any:
        """
        Create ambassador group with optional job linkage and invitations.

        If job_id is provided, validates job exists/has rate and creates invitations.
        Without job_id, only group and UserGroup membership records are created.
        """
        from graphql import GraphQLError

        response_cls = response_class or cls.response_class
        field_name = model_field_name or cls.model_field_name
        message = create_message or cls.create_message

        if not response_cls:
            raise ValueError(
                "response_class must be provided either as class attribute or parameter"
            )

        try:
            # Create service instance and set user/tenant
            service = cls.with_input(input)
            await service.set_user_and_tenant(info)

            # Step 1: Validate job when provided
            job_id = getattr(input, "job_id", None)
            job = None
            if job_id:
                try:
                    resolved_job_id = resolve_id_to_int(job_id)
                except (TypeError, ValueError, GraphQLError) as exc:
                    raise GraphQLError(f"Invalid job ID: {job_id}") from exc

                try:
                    # Fetch job with select_related for rate and tenant
                    job = await sync_to_async(
                        lambda: job_models.Job.objects.select_related("rate", "tenant").get(
                            id=resolved_job_id
                        )
                    )()
                except job_models.Job.DoesNotExist:
                    raise GraphQLError("Job not found.")

                if job.rate_id is None:
                    raise GraphQLError("Job must have a rate assigned.")

            # Step 2-4: Create Group, UserGroups, and AmbassadorJobs (within transaction)
            def create_group_with_extras():
                with transaction.atomic():
                    # Create the group using base mutation service logic
                    model_class = service.get_model()
                    model = model_class()
                    if service.user:
                        setattr(model, "created_by", service.user)

                    # Set parameters from input (excluding job_id and ambassador_ids)
                    params = input.to_dict(["tenant_id", "job_id", "ambassador_ids"])
                    group_type_id = params.get("group_type_id")
                    if group_type_id is not None:
                        try:
                            params["group_type_id"] = resolve_id_to_int(group_type_id)
                        except (TypeError, ValueError, GraphQLError) as exc:
                            raise GraphQLError(
                                f"Invalid group type ID: {group_type_id}"
                            ) from exc
                    for key, value in params.items():
                        setattr(model, key, value)

                    # Set tenant_id from service
                    setattr(model, "tenant_id", service.tenant_id)
                    model.save()
                    if job:
                        service._upsert_group_job_link(
                            group=model,
                            job=job,
                            user=service.user,
                        )

                    # Step 3 and 4: Create UserGroup and AmbassadorJob  records if ambassador_ids provided
                    service.create_user_groups(model, job)

                    return model

            model_instance = await sync_to_async(create_group_with_extras)()

            # Generate message if not provided
            if not message:
                message = cls._get_default_message(field_name, "create")

            return cls._build_mutation_response(
                response_class=response_cls,
                success=True,
                message=message,
                input_obj=input,
                **{field_name: model_instance},
            )
        except GraphQLError as e:
            return cls._build_mutation_response(
                response_class=response_cls,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except Exception as e:
            return cls._build_mutation_response(
                response_class=response_cls,
                success=False,
                message=f"Error creating ambassador group: {str(e)}",
                input_obj=input,
            )

    @classmethod
    async def add_ambassadors_to_group(
        cls,
        input: inputs.AddAmbassadorsToGroupInput,
        info: strawberry.Info,
        *,
        response_class: type | None = None,
    ) -> Any:
        try:
            from graphql import GraphQLError
            from utils.graphql.mixins import resolve_id_to_int

            resolved_group_id = resolve_id_to_int(input.group_id)
            try:
                group = await sync_to_async(
                    AmbassadorGroup.objects.select_related("group_type", "tenant").get
                )(id=resolved_group_id)
            except AmbassadorGroup.DoesNotExist:
                raise GraphQLError("Group not found.")
            service = cls.with_input(input)
            await service.set_user_and_tenant(info)

            job_id = getattr(input, "job_id", None)
            job = None
            if job_id:
                from utils.graphql.mixins import resolve_id_to_int

                resolved_job_id = resolve_id_to_int(job_id)
                job = await sync_to_async(
                    job_models.Job.objects.select_related("rate", "tenant").get
                )(id=resolved_job_id)

            def create_user_groups():
                with transaction.atomic():
                    if job:
                        service._upsert_group_job_link(group=group, job=job, user=service.user)
                    return service.create_user_groups(group, job)

            user_groups = await sync_to_async(create_user_groups)()
            return cls._build_mutation_response(
                response_class=response_class,
                success=True,
                message="Ambassadors added to group successfully.",
                input_obj=input,
                members=user_groups,
            )

        except (
            job_models.Job.DoesNotExist,
            ValueError,
            TypeError,
            GraphQLError,
        ) as exc:
            return cls._build_mutation_response(
                response_class=response_class,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @classmethod
    async def update(
        cls,
        input: SparkGraphQLInput,
        info: strawberry.Info,
        *,
        response_class: type | None = None,
        model_field_name: str | None = None,
        update_message: str | None = None,
    ) -> Any:
        """Update ambassador group and sync explicit group-job relation."""
        from graphql import GraphQLError

        response_cls = response_class or cls.response_class
        field_name = model_field_name or cls.model_field_name
        message = update_message or cls.update_message
        if not response_cls:
            raise ValueError(
                "response_class must be provided either as class attribute or parameter"
            )

        try:
            service = cls.with_input(input)
            await service.set_user_and_tenant(info)

            model_id = getattr(input, "id", None)
            if not model_id:
                raise GraphQLError("Group ID is required.")

            try:
                resolved_group_id = resolve_id_to_int(model_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid group ID.") from exc

            group = await sync_to_async(AmbassadorGroup.objects.get)(id=resolved_group_id)

            params = input.to_dict(["tenant_id", "id", "job_id", "ambassador_ids"])
            group_type_id = params.get("group_type_id")
            if group_type_id is not None:
                try:
                    params["group_type_id"] = resolve_id_to_int(group_type_id)
                except (TypeError, ValueError, GraphQLError) as exc:
                    raise GraphQLError(f"Invalid group type ID: {group_type_id}") from exc

            for key, value in params.items():
                setattr(group, key, value)
            group.updated_by = service.user
            await sync_to_async(group.save)()

            job_id = getattr(input, "job_id", None)
            if job_id:
                try:
                    resolved_job_id = resolve_id_to_int(job_id)
                except (TypeError, ValueError, GraphQLError) as exc:
                    raise GraphQLError(f"Invalid job ID: {job_id}") from exc

                job = await sync_to_async(
                    job_models.Job.objects.select_related("tenant").get
                )(id=resolved_job_id)
                await sync_to_async(service._upsert_group_job_link)(
                    group=group,
                    job=job,
                    user=service.user,
                )

            if not message:
                message = cls._get_default_message(field_name, "update")

            return cls._build_mutation_response(
                response_class=response_cls,
                success=True,
                message=message,
                input_obj=input,
                **{field_name: group},
            )
        except (GraphQLError, AmbassadorGroup.DoesNotExist, job_models.Job.DoesNotExist) as exc:
            return cls._build_mutation_response(
                response_class=response_cls,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @classmethod
    async def remove_ambassadors_from_group(
        cls,
        input: inputs.RemoveAmbassadorsFromGroupInput,
        info: strawberry.Info,
        *,
        response_class: type | None = None,
    ) -> Any:
        try:
            from graphql import GraphQLError
            from utils.graphql.mixins import resolve_id_to_int
            from django.db import transaction

            resolved_group_id = resolve_id_to_int(input.group_id)
            try:
                group = await sync_to_async(AmbassadorGroup.objects.get)(
                    id=resolved_group_id
                )
            except AmbassadorGroup.DoesNotExist:
                raise GraphQLError("Group not found.")

            user_group_ids = getattr(input, "user_group_ids", None)
            if not user_group_ids:
                raise GraphQLError("User group IDs are required.")

            @sync_to_async
            def delete_user_groups():
                with transaction.atomic():
                    for user_group_id in user_group_ids:
                        resolved_user_group_id = resolve_id_to_int(user_group_id)
                        try:
                            user_group = UserGroup.objects.get(
                                id=resolved_user_group_id, group=group
                            )
                            user_group.delete()
                        except UserGroup.DoesNotExist:
                            raise GraphQLError(
                                f"UserGroup with ID {user_group_id} not found in this group."
                            )

            await delete_user_groups()

            return cls._build_mutation_response(
                response_class=response_class,
                success=True,
                message="Ambassadors removed from group successfully.",
                input_obj=input,
            )
        except (
            AmbassadorGroup.DoesNotExist,
            ValueError,
            TypeError,
            GraphQLError,
        ) as exc:
            return cls._build_mutation_response(
                response_class=response_class,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @classmethod
    async def assign_group_to_job(
        cls,
        input: inputs.AssignGroupToJobInput,
        info: strawberry.Info,
        *,
        response_class: type | None = None,
    ) -> Any:
        """Assign an existing ambassador group to a job."""
        try:
            from graphql import GraphQLError

            service = cls.with_input(input)
            await service.set_user_and_tenant(info)

            resolved_group_id = resolve_id_to_int(input.group_id)
            resolved_job_id = resolve_id_to_int(input.job_id)

            group = await sync_to_async(
                AmbassadorGroup.objects.select_related("tenant").get
            )(id=resolved_group_id)
            job = await sync_to_async(
                job_models.Job.objects.select_related("tenant").get
            )(id=resolved_job_id)

            if group.tenant_id != job.tenant_id:
                raise GraphQLError("Group and job must belong to the same tenant.")
            if job.rate_id is None:
                raise GraphQLError("Job must have a rate assigned.")

            if service.tenant_id is not None and group.tenant_id != service.tenant_id:
                raise GraphQLError("Group not found.")

            def assign_and_invite():
                with transaction.atomic():
                    service._upsert_group_job_link(
                        group=group,
                        job=job,
                        user=service.user,
                    )

                    members = list(
                        group.members.select_related("ambassador__user")
                        .exclude(ambassador__isnull=True)
                    )
                    ambassadors = [member.ambassador for member in members if member.ambassador]
                    if not ambassadors:
                        return

                    existing_ids = set(
                        job_models.AmbassadorJob.objects.filter(
                            job=job,
                            ambassador_id__in=[amb.id for amb in ambassadors],
                        ).values_list("ambassador_id", flat=True)
                    )

                    for ambassador in ambassadors:
                        if ambassador.id in existing_ids:
                            continue
                        try:
                            job_models.AmbassadorJob.objects.create_and_invite(
                                job=job, ambassador=ambassador, action_by=service.user
                            )
                        except ValueError as exc:
                            error_message = str(exc)
                            if "already has an invitation for this job" in error_message.lower():
                                continue

                            if "An active invitation already exists for this email." not in error_message:
                                raise

                            invited_status = job_models.Status.objects.get_invited(
                                tenant_id=job.tenant_id, user=service.user
                            )
                            ambassador_job = (
                                job_models.AmbassadorJob.objects.filter(
                                    ambassador=ambassador,
                                    job=job,
                                )
                                .order_by("-created_at")
                                .first()
                            )
                            if ambassador_job:
                                ambassador_job.status = invited_status
                                ambassador_job.rate = job.rate
                                ambassador_job.updated_by = service.user
                                ambassador_job.save(
                                    update_fields=["status", "rate", "updated_by", "updated_at"]
                                )
                            else:
                                job_models.AmbassadorJob.objects.create(
                                    ambassador=ambassador,
                                    job=job,
                                    tenant=job.tenant,
                                    status=invited_status,
                                    rate=job.rate,
                                    appear_as_rfp=True,
                                    created_by=service.user,
                                    updated_by=service.user,
                                )

            await sync_to_async(assign_and_invite)()

            return cls._build_mutation_response(
                response_class=response_class,
                success=True,
                message="Group assigned to job and invitations sent successfully.",
                input_obj=input,
                ambassador_group=group,
            )
        except (
            AmbassadorGroup.DoesNotExist,
            job_models.Job.DoesNotExist,
            ValueError,
            TypeError,
            GraphQLError,
        ) as exc:
            return cls._build_mutation_response(
                response_class=response_class,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    def create_user_groups(
        self, group: AmbassadorGroup, job: job_models.Job | None = None
    ) -> list[UserGroup]:
        from graphql import GraphQLError

        ambassador_ids = getattr(self.input, "ambassador_ids", None)
        if not ambassador_ids:
            return []

        # Resolve ambassador IDs from strawberry.ID to integers
        resolved_ids = []
        for ambassador_id in ambassador_ids:
            try:
                resolved_id = resolve_id_to_int(ambassador_id)
                resolved_ids.append(resolved_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError(f"Invalid ambassador ID: {ambassador_id}") from exc

        ambassadors = list(
            Ambassador.objects.select_related("user").filter(id__in=resolved_ids)
        )

        if len(ambassadors) != len(resolved_ids):
            found_ids = {amb.id for amb in ambassadors}
            missing_ids = set(resolved_ids) - found_ids
            raise GraphQLError(f"Ambassadors with IDs {missing_ids} not found.")

        ambassador_label_by_id = {}
        for ambassador in ambassadors:
            ambassador_label_by_id[ambassador.id] = (
                f"{(ambassador.user.first_name or '').strip()} {(ambassador.user.last_name or '').strip()}".strip()
                or ambassador.user.email
                or f"ID {ambassador.id}"
            )

        if job:
            existing_ids = set(
                job_models.AmbassadorJob.objects.filter(
                    job=job,
                    ambassador_id__in=[amb.id for amb in ambassadors],
                ).values_list("ambassador_id", flat=True)
            )
            if existing_ids:
                duplicates = ", ".join(
                    ambassador_label_by_id[amb_id] for amb_id in sorted(existing_ids)
                )
                raise GraphQLError(
                    f"Cannot invite these ambassadors because they already have an invitation for this job: {duplicates}."
                )

        user_groups = []
        for ambassador in ambassadors:
            ambassador_display_name = ambassador_label_by_id[ambassador.id]
            # assign the user to the group
            user_group = UserGroup.objects.create(
                group=group,
                user=ambassador.user,
                ambassador=ambassador,
            )
            user_groups.append(user_group)

            if not job:
                continue
            try:
                job_models.AmbassadorJob.objects.create_and_invite(
                    job=job, ambassador=ambassador, action_by=self.user
                )
            except ValueError as exc:
                error_message = str(exc)
                if "already has an invitation for this job" in error_message.lower():
                    raise GraphQLError(
                        f"Cannot invite {ambassador_display_name}: this ambassador already has an invitation for this job."
                    ) from exc

                if "An active invitation already exists for this email." not in error_message:
                    raise

                invited_status = job_models.Status.objects.get_invited(
                    tenant_id=job.tenant_id, user=self.user
                )
                ambassador_job = (
                    job_models.AmbassadorJob.objects.filter(
                        ambassador=ambassador,
                        job=job,
                    )
                    .order_by("-created_at")
                    .first()
                )
                if ambassador_job:
                    ambassador_job.status = invited_status
                    ambassador_job.rate = job.rate
                    ambassador_job.updated_by = self.user
                    ambassador_job.save(update_fields=["status", "rate", "updated_by", "updated_at"])
                else:
                    job_models.AmbassadorJob.objects.create(
                        ambassador=ambassador,
                        job=job,
                        tenant=job.tenant,
                        status=invited_status,
                        rate=job.rate,
                        appear_as_rfp=True,
                        created_by=self.user,
                        updated_by=self.user,
                    )

        return user_groups


class RegisterPushTokenService(BaseAmbassadorService):
    """Service for registering a mobile device's Expo push token.

    Idempotent on `token`: re-registering the same token just updates
    the device metadata + `last_used_at`. If the token previously
    belonged to a different user (account switch on the same device),
    we move ownership to the current user — the new user is now the
    one we should target.
    """

    PLATFORMS = {"ios", "android", "web"}

    @classmethod
    async def register(
        cls,
        input: "inputs.RegisterPushTokenInput",
        info: strawberry.Info,
    ) -> RegisterPushTokenResponse:
        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return build_mutation_response(
                RegisterPushTokenResponse,
                success=False,
                message="Authentication required.",
                input_obj=input,
            )

        token = (input.token or "").strip()
        platform = (input.platform or "").strip().lower()
        if not token:
            return build_mutation_response(
                RegisterPushTokenResponse,
                success=False,
                message="Token is required.",
                input_obj=input,
            )
        if platform not in cls.PLATFORMS:
            return build_mutation_response(
                RegisterPushTokenResponse,
                success=False,
                message=f"Unsupported platform: {input.platform!r}.",
                input_obj=input,
            )

        now = timezone.now()

        @sync_to_async
        def upsert():
            device, _ = PushDevice.objects.update_or_create(
                token=token,
                defaults={
                    "user": user,
                    "platform": platform,
                    "device_name": input.device_name or None,
                    "app_version": input.app_version or None,
                    "is_active": True,
                    "last_used_at": now,
                },
            )
            return device

        try:
            await upsert()
        except Exception as e:
            return build_mutation_response(
                RegisterPushTokenResponse,
                success=False,
                message=f"Error registering push token: {e}",
                input_obj=input,
            )

        return build_mutation_response(
            RegisterPushTokenResponse,
            success=True,
            message="Push token registered.",
            input_obj=input,
        )


class OAuthSignInService(BaseAmbassadorService):
    """Sign in / sign up via Apple or Google id_tokens.

    Pattern:
      1. Verify the platform-issued id_token cryptographically.
      2. Look up an existing User by email.
      3. If new: create User (role = Ambassador, is_active=True,
         UserStatus.verified=True) and an Ambassador profile row tied
         to that user. Profile starts ``is_active=False`` so admins
         still gate who can pick up shifts — but the account itself
         is signed in and usable for browsing.
      4. Issue gqlauth TokenType + RefreshToken.
      5. Return the same shape mobile expects.

    Errors are returned in the response envelope, never raised — so
    the mobile client gets a clean message string to surface.
    """

    @classmethod
    async def _issue_tokens(cls, user) -> tuple[str, str | None]:
        from gqlauth.jwt.types_ import TokenType
        from gqlauth.models import RefreshToken

        token_obj = await sync_to_async(TokenType.from_user)(user)
        try:
            refresh_obj = await sync_to_async(RefreshToken.from_user)(user)
            refresh_token = refresh_obj.token
        except Exception:
            refresh_token = None
        return token_obj.token, refresh_token

    @classmethod
    async def _find_or_create_user(
        cls,
        *,
        email: str,
        first_name: str | None,
        last_name: str | None,
        provider: str,
    ) -> tuple[Any, bool]:
        """Return (user, is_new). Creates User + Ambassador on first sign-in."""
        existing = await sync_to_async(
            User.objects.filter(email__iexact=email).first
        )()
        if existing:
            return existing, False

        @sync_to_async
        @transaction.atomic
        def make_user():
            role = Role.objects.get(pk=ROLE_ID.Ambassadors)
            user = User.objects.create(
                first_name=(first_name or "").strip()[:150],
                last_name=(last_name or "").strip()[:150],
                username=email,
                email=email,
                role=role,
                is_active=True,
            )
            UserStatus.objects.update_or_create(
                user=user,
                defaults={"verified": True, "archived": False},
            )
            # Ambassador profile — starts pending admin approval.
            ambassador = Ambassador.objects.create(
                user=user,
                is_active=False,
                created_by=user,
                updated_by=user,
            )
            return user, ambassador

        user, ambassador = await make_user()

        # Best-effort admin alert. Swallow failures so the user
        # still gets signed in if email is down.
        try:
            from ambassadors.envelopes import NewAmbassadorAlertMailer

            mailer = NewAmbassadorAlertMailer(ambassador, provider=provider)
            await mailer.send_async()
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "new ambassador alert email failed for user_id=%s", user.id
            )

        return user, True

    @classmethod
    async def sign_in_with_apple(
        cls,
        input: "inputs.AppleSignInInput",
        info: strawberry.Info,
    ) -> OAuthSignInResponse:
        from tenants.oauth import (
            OAuthVerificationError,
            verify_apple_id_token,
        )

        try:
            identity = await sync_to_async(verify_apple_id_token)(
                input.id_token,
                name_hint={
                    "first_name": input.first_name,
                    "last_name": input.last_name,
                },
            )
        except OAuthVerificationError as exc:
            return build_mutation_response(
                OAuthSignInResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

        return await cls._finish(input, identity)

    @classmethod
    async def sign_in_with_google(
        cls,
        input: "inputs.GoogleSignInInput",
        info: strawberry.Info,
    ) -> OAuthSignInResponse:
        from tenants.oauth import (
            OAuthVerificationError,
            verify_google_id_token,
        )

        try:
            identity = await sync_to_async(verify_google_id_token)(input.id_token)
        except OAuthVerificationError as exc:
            return build_mutation_response(
                OAuthSignInResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

        return await cls._finish(input, identity)

    @classmethod
    async def _finish(cls, input, identity) -> OAuthSignInResponse:
        try:
            user, is_new = await cls._find_or_create_user(
                email=identity.email,
                first_name=identity.first_name,
                last_name=identity.last_name,
                provider=identity.provider,
            )
        except Exception as exc:
            return build_mutation_response(
                OAuthSignInResponse,
                success=False,
                message=f"Could not provision account: {exc}",
                input_obj=input,
            )

        if not user.is_active:
            return build_mutation_response(
                OAuthSignInResponse,
                success=False,
                message="Account is disabled. Contact your RMM at Ignite.",
                input_obj=input,
            )

        try:
            token, refresh_token = await cls._issue_tokens(user)
        except Exception as exc:
            return build_mutation_response(
                OAuthSignInResponse,
                success=False,
                message=f"Could not issue token: {exc}",
                input_obj=input,
            )

        return build_mutation_response(
            OAuthSignInResponse,
            success=True,
            message=(
                "Welcome! Your account is pending approval."
                if is_new
                else "Signed in."
            ),
            input_obj=input,
            token=OAuthTokenType(token=token, refresh_token=refresh_token),
            user=OAuthUserType(
                uuid=strawberry.ID(str(getattr(user, "uuid", user.id))),
                email=user.email,
                first_name=user.first_name or None,
                last_name=user.last_name or None,
            ),
            is_new_account=is_new,
        )


class LocationPingService(BaseAmbassadorService):
    """Ingest GPS pings the spark-mobile activation tracker sends every
    ~2 min. The mutation is intentionally cheap — no validation beyond
    "the BA is on this event" — so the mobile client can fire-and-forget
    on a flaky cellular connection without backpressuring the user.
    """

    @classmethod
    async def record(
        cls,
        input: "inputs.LocationPingInput",
        info: strawberry.Info,
    ) -> LocationPingResponse:
        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return build_mutation_response(
                LocationPingResponse,
                success=False,
                message="Authentication required.",
                input_obj=input,
            )

        # The ping is BA-scoped — anybody else hitting this is misuse.
        try:
            ambassador = await sync_to_async(
                lambda: Ambassador.objects.filter(user=user).first()
            )()
        except Exception:
            ambassador = None
        if not ambassador:
            return build_mutation_response(
                LocationPingResponse,
                success=False,
                message="No ambassador profile.",
                input_obj=input,
            )

        # Event lookup — accept opaque relay IDs or raw uuids.
        from events.models import Event as EventModel

        event_uuid = str(input.event_uuid or "")
        try:
            # Strip Relay base64 prefix if present.
            try:
                from utils.graphql.mixins import resolve_id_to_int

                # If it's a Relay id, resolve_id_to_int returns an int —
                # which means it isn't a uuid. We fall back to id lookup.
                int_id = resolve_id_to_int(event_uuid)
            except Exception:
                int_id = None

            if int_id is not None:
                event = await sync_to_async(
                    lambda: EventModel.objects.filter(id=int_id).only("id").first()
                )()
            else:
                event = await sync_to_async(
                    lambda: EventModel.objects.filter(uuid=event_uuid).only("id").first()
                )()
        except Exception:
            event = None
        if not event:
            return build_mutation_response(
                LocationPingResponse,
                success=False,
                message="Event not found.",
                input_obj=input,
            )

        # Timestamp — trust client when supplied, fall back to server.
        recorded_at = timezone.now()
        if input.recorded_at:
            try:
                # parse iso datetime (with or without trailing Z)
                dt_str = input.recorded_at.replace("Z", "+00:00")
                recorded_at = datetime.fromisoformat(dt_str)
            except Exception:
                # Bad timestamp from the device — keep server time.
                pass

        source = (input.source or "background").strip().lower()
        if source not in {"foreground", "background", "clock_in", "clock_out"}:
            source = "background"

        try:
            await sync_to_async(
                lambda: LocationPing.objects.create(
                    ambassador=ambassador,
                    event=event,
                    lat=float(input.lat),
                    lng=float(input.lng),
                    accuracy_meters=input.accuracy_meters,
                    recorded_at=recorded_at,
                    source=source,
                )
            )()
        except Exception as exc:
            return build_mutation_response(
                LocationPingResponse,
                success=False,
                message=f"Could not record ping: {exc}",
                input_obj=input,
            )

        return build_mutation_response(
            LocationPingResponse,
            success=True,
            message="ok",
            input_obj=input,
        )


class ShiftOfferService(BaseAmbassadorService):
    """Accept / decline an AmbassadorEvent invitation from the mobile
    app. Spawned by the admin app creating an AmbassadorEvent row
    (is_approved=False). The push notification carries the
    ambassadorEventUuid; mobile fetches the offer here and lets the
    BA respond.

    Accept → is_approved=True. Decline → delete the row so the
    admin can re-invite a different BA without conflict.
    """

    @classmethod
    async def get_offer(
        cls, ambassador_event_uuid: str, info: strawberry.Info
    ) -> ShiftOfferDetails | None:
        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return None

        @sync_to_async
        def fetch():
            from ambassadors.models import (
                Ambassador as A,
                AmbassadorEvent as AE,
            )

            try:
                ambassador = A.objects.get(user=user)
            except A.DoesNotExist:
                return None
            ae = (
                AE.objects.select_related(
                    "event",
                    "event__retailer",
                    "event__state",
                )
                .filter(uuid=str(ambassador_event_uuid), ambassador=ambassador)
                .first()
            )
            if not ae:
                return None
            ev = ae.event
            venue = (
                getattr(ev, "name", None)
                or getattr(getattr(ev, "retailer", None), "name", None)
            )
            state_code = getattr(getattr(ev, "state", None), "code", None)
            date = (
                ev.date.isoformat()
                if getattr(ev, "date", None)
                else None
            )
            return ShiftOfferDetails(
                ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                event_uuid=strawberry.ID(str(ev.uuid)),
                event_name=venue or "(shift)",
                venue=venue,
                address=getattr(ev, "address", None),
                date=date,
                start_time=(
                    ev.start_time.isoformat()
                    if getattr(ev, "start_time", None)
                    else None
                ),
                end_time=(
                    ev.end_time.isoformat()
                    if getattr(ev, "end_time", None)
                    else None
                ),
                state_code=state_code,
                is_approved=bool(ae.is_approved),
            )

        return await fetch()

    @classmethod
    async def respond(
        cls,
        input: "inputs.RespondToShiftOfferInput",
        info: strawberry.Info,
    ) -> RespondToShiftOfferResponse:
        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return build_mutation_response(
                RespondToShiftOfferResponse,
                success=False,
                message="Authentication required.",
                input_obj=input,
            )

        from ambassadors.models import AmbassadorEvent as AE

        @sync_to_async
        def lookup():
            try:
                ambassador = Ambassador.objects.get(user=user)
            except Ambassador.DoesNotExist:
                return None
            return (
                AE.objects.select_related("event")
                .filter(
                    uuid=str(input.ambassador_event_uuid),
                    ambassador=ambassador,
                )
                .first()
            )

        ae = await lookup()
        if not ae:
            return build_mutation_response(
                RespondToShiftOfferResponse,
                success=False,
                message="Offer not found.",
                input_obj=input,
            )

        if input.accepted:
            @sync_to_async
            def accept():
                ae.is_approved = True
                ae.updated_by = user
                ae.save(update_fields=["is_approved", "updated_by", "updated_at"])
                return ae

            try:
                await accept()
            except Exception as exc:
                return build_mutation_response(
                    RespondToShiftOfferResponse,
                    success=False,
                    message=f"Could not accept: {exc}",
                    input_obj=input,
                )
            return build_mutation_response(
                RespondToShiftOfferResponse,
                success=True,
                message="Shift accepted.",
                input_obj=input,
                accepted=True,
            )

        # Decline → delete so admin can re-invite cleanly.
        try:
            await sync_to_async(ae.delete)()
        except Exception as exc:
            return build_mutation_response(
                RespondToShiftOfferResponse,
                success=False,
                message=f"Could not decline: {exc}",
                input_obj=input,
            )
        return build_mutation_response(
            RespondToShiftOfferResponse,
            success=True,
            message="Shift declined.",
            input_obj=input,
            accepted=False,
        )
