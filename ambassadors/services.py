"""Services for ambassador mutations."""

import strawberry
from typing import Any
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
import secrets

from tenants.models import Role, Tenant, TenantedUser
from gqlauth.core.utils import get_token
from utils.utils import build_mutation_response
from utils.graphql.inputs import SparkGraphQLInput

from .models import Ambassador, AmbassadorInvitation
from .types import (
    PublicAmbassadorCreationResponse,
    AmbassadorInvitationResponse,
    AcceptInvitationResponse,
    ApproveAmbassadorResponse,
)
from . import inputs
from .constants import INVITATION_EXPIRY_DAYS

User = get_user_model()


def validate_passwords_match(
    input: SparkGraphQLInput,
    response_class: type
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


class PublicAmbassadorCreationService:
    """Service for public ambassador creation."""

    @classmethod
    async def create(
        cls,
        input: inputs.CreatePublicAmbassadorInput,
        info: strawberry.Info,
    ) -> PublicAmbassadorCreationResponse:
        """Create a public ambassador account (inactive by default)."""
        # Validate passwords match
        password_error = validate_passwords_match(
            input, PublicAmbassadorCreationResponse)
        if password_error:
            return password_error

        # Validate email doesn't exist
        if await sync_to_async(User.objects.filter(email=input.email).exists)():
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
            @sync_to_async
            def create_user():
                user = User.objects.create(
                    first_name=input.first_name,
                    username=input.email,
                    email=input.email,
                    role=role,
                    is_active=False,
                )
                user.set_password(input.password1)
                user.save()
                return user

            user = await create_user()

            # Create ambassador (inactive by default)
            @sync_to_async
            def create_ambassador():
                return Ambassador.objects.create(
                    user=user,
                    address=input.address,
                    coordinates=input.coordinates or [],
                    is_active=False,  # Requires manual approval
                    created_by=user,
                    updated_by=user,
                )

            ambassador = await create_ambassador()

            # Generate activation token
            activation_token = await sync_to_async(get_token)(user, "activation")

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


class AmbassadorInvitationService:
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
        recipient: User | None = None
        try:
            recipient = await sync_to_async(User.objects.get)(email=input.email)
        except User.DoesNotExist:
            pass

        if await sync_to_async(User.objects.filter(email=input.email).exists)():
            return build_mutation_response(
                AmbassadorInvitationResponse,
                success=False,
                message="User with this email already exists.",
                input_obj=input,
            )

        # Validate no active invitation exists
        now = timezone.now()
        active_invitation_exists = await sync_to_async(
            AmbassadorInvitation.objects.filter(
                email=input.email,
                is_used=False,
                expires_at__gt=now,
            ).exists
        )()
        if active_invitation_exists:
            return build_mutation_response(
                AmbassadorInvitationResponse,
                success=False,
                message="An active invitation already exists for this email.",
                input_obj=input,
            )

        try:
            # Get tenant
            tenant = await sync_to_async(Tenant.objects.get)(pk=int(input.tenant_id))
        except (Tenant.DoesNotExist, ValueError, TypeError):
            return build_mutation_response(
                AmbassadorInvitationResponse,
                success=False,
                message="Invalid tenant ID.",
                input_obj=input,
            )

        try:
            # Generate secure token
            token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(days=INVITATION_EXPIRY_DAYS)

            # Create invitation
            @sync_to_async
            def create_invitation():
                return AmbassadorInvitation.objects.create(
                    email=input.email,
                    token=token,
                    expires_at=expires_at,
                    invited_by=user,
                    tenant=tenant,
                    created_by=user,
                    updated_by=user,
                )

            invitation = await create_invitation()

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


class AcceptInvitationService:
    """Service for accepting ambassador invitations."""

    @classmethod
    async def accept(
        cls,
        input: inputs.AcceptAmbassadorInvitationInput,
        info: strawberry.Info,
    ) -> AcceptInvitationResponse:
        """Accept an ambassador invitation and create account."""
        # Validate passwords match
        password_error = validate_passwords_match(
            input, AcceptInvitationResponse)
        if password_error:
            return password_error

        # Get and validate invitation
        try:
            @sync_to_async
            def get_invitation():
                return AmbassadorInvitation.objects.select_related("tenant", "invited_by").get(
                    token=input.token
                )
            invitation = await get_invitation()
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
        if await sync_to_async(User.objects.filter(email=invitation.email).exists)():
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
            @sync_to_async
            def create_user():
                user = User.objects.create(
                    first_name=input.first_name,
                    username=invitation.email,
                    email=invitation.email,
                    role=role,
                    is_active=True,  # Active since invited
                )
                user.set_password(input.password1)
                user.save()
                return user

            user = await create_user()

            # Create TenantedUser
            @sync_to_async
            def create_tenanted_user():
                return TenantedUser.objects.create(
                    user=user,
                    tenant=invitation.tenant,
                    is_active=True,
                    created_by=invitation.invited_by,
                    updated_by=invitation.invited_by,
                )

            await create_tenanted_user()

            # Create ambassador (active by default for invitations)
            @sync_to_async
            def create_ambassador():
                return Ambassador.objects.create(
                    user=user,
                    address=input.address,
                    coordinates=input.coordinates or [],
                    is_active=True,  # Active since invited
                    created_by=invitation.invited_by,
                    updated_by=invitation.invited_by,
                )

            ambassador = await create_ambassador()

            # Mark invitation as used
            @sync_to_async
            def mark_invitation_used():
                invitation.is_used = True
                invitation.used_at = now
                invitation.ambassador = ambassador
                invitation.updated_by = invitation.invited_by
                invitation.save()

            await mark_invitation_used()

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


class ApproveAmbassadorService:
    """Service for approving ambassadors."""

    @classmethod
    async def approve(
        cls,
        input: inputs.ApproveAmbassadorInput,
        info: strawberry.Info,
    ) -> ApproveAmbassadorResponse:
        """Approve an ambassador and optionally assign to tenant."""
        user = info.context.request.user

        # Validate user has permission (client or spark-admin)
        # Access role asynchronously to avoid sync DB calls
        @sync_to_async
        def get_role_slug():
            return getattr(user.role, "slug", "").lower() if user.role else ""
        role_slug = await get_role_slug()
        if role_slug == "ambassador":
            return build_mutation_response(
                ApproveAmbassadorResponse,
                success=False,
                message="You do not have permission to approve ambassadors.",
                input_obj=input,
            )

        try:
            @sync_to_async
            def get_ambassador():
                return Ambassador.objects.select_related("user").get(
                    pk=int(input.ambassador_id)
                )
            ambassador = await get_ambassador()
        except (Ambassador.DoesNotExist, ValueError, TypeError):
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

            # Assign to tenant if provided
            if input.tenant_id:
                try:
                    tenant = await sync_to_async(Tenant.objects.get)(pk=int(input.tenant_id))

                    # Check if TenantedUser already exists
                    tenanted_user_exists = await sync_to_async(
                        TenantedUser.objects.filter(
                            user=ambassador.user, tenant=tenant
                        ).exists
                    )()

                    if not tenanted_user_exists:
                        @sync_to_async
                        def create_tenanted_user():
                            return TenantedUser.objects.create(
                                user=ambassador.user,
                                tenant=tenant,
                                is_active=True,
                                created_by=user,
                                updated_by=user,
                            )

                        await create_tenanted_user()
                except (Tenant.DoesNotExist, ValueError, TypeError):
                    return build_mutation_response(
                        ApproveAmbassadorResponse,
                        success=False,
                        message="Invalid tenant ID.",
                        input_obj=input,
                    )

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
