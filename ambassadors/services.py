"""Services for ambassador mutations and queries."""

import asyncio
import sys
import secrets
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
from utils.utils import build_mutation_response
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
    UserGroup,
)
from jobs import models as job_models
from .types import (
    PublicAmbassadorCreationResponse,
    AmbassadorInvitationResponse,
    AcceptInvitationResponse,
    ApproveAmbassadorResponse,
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
)
from events.models import Client
from . import inputs
from .constants import INVITATION_EXPIRY_DAYS

User = get_user_model()


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
        # Validate passwords match
        password_error = validate_passwords_match(
            input, PublicAmbassadorCreationResponse
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
                email=input.email,
                role=role,
                password=input.password1,
                is_active=True,
            )

            # Create ambassador (inactive by default)
            ambassador = await Ambassador.objects._create(
                user=user,
                address=input.address,
                phone=input.phone,
                coordinates=input.coordinates or [],
                is_active=ambassador_is_active,  # Requires manual approval by default
                created_by=user,
                updated_by=user,
            )

            # Generate activation token
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

            return build_mutation_response(
                PublicAmbassadorCreationResponse,
                success=True,
                message="Ambassador account created successfully. Please verify your email and wait for approval.",
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

            if input.address is not None:
                ambassador.address = input.address
            if input.phone is not None:
                ambassador.phone = input.phone
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
        except (ValueError, TypeError, GraphQLError):
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
        filters: inputs.AmbassadorFiltersInput | None = None,
    ) -> CountableConnection[AmbassadorType]:
        """General ambassador list with filters for active status, rating, name, and email."""
        # Reuse available_ambassadors logic for tenant resolution and filtering.
        return await self.get_available_ambassadors(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    async def _get_filtered_ambassadors_queryset(
        self,
        tenant_id: int | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
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

                # General search across email, name, and address
                if filters.search:
                    queryset = queryset.filter(
                        Q(user__email__icontains=filters.search)
                        | Q(user__first_name__icontains=filters.search)
                        | Q(user__last_name__icontains=filters.search)
                        | Q(address__icontains=filters.search)
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
        Create ambassador group with job validation and ambassador invitations.

        Validates job exists and has rate, creates group, optionally creates UserGroup
        records and AmbassadorJob invitations for each ambassador.
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

            # Step 1: Validate Job
            job_id = getattr(input, "job_id", None)
            if not job_id:
                raise GraphQLError("Job ID is required.")

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

            if job.rate is None:
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

        user_groups = []
        for ambassador in ambassadors:
            # assign the user to the group
            user_group = UserGroup.objects.create(
                group=group,
                user=ambassador.user,
                ambassador=ambassador,
            )
            user_groups.append(user_group)

            if not job:
                continue
            job_models.AmbassadorJob.objects.create_and_invite(
                job=job, ambassador=ambassador, action_by=self.user
            )

        return user_groups
