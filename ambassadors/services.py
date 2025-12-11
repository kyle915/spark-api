"""Services for ambassador mutations and queries."""

import strawberry
from typing import Any
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta, datetime
import secrets
from django.db.models import Q

from tenants.models import Role, Tenant, TenantedUser
from gqlauth.core.utils import get_token
from utils.utils import build_mutation_response
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.relay import CountableConnection

from .models import Ambassador, AmbassadorInvitation, AmbassadorReview
from .types import (
    PublicAmbassadorCreationResponse,
    AmbassadorInvitationResponse,
    AcceptInvitationResponse,
    ApproveAmbassadorResponse,
    UpdateAmbassadorResponse,
    DeleteInvitationResponse,
    AmbassadorInvitationType,
    Ambassador as AmbassadorType,
    CreateAmbassadorReviewResponse,
    UpdateAmbassadorReviewResponse,
    DeleteAmbassadorReviewResponse,
)
from events.models import Client
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
    ) -> PublicAmbassadorCreationResponse:
        """Create a public ambassador account (inactive by default)."""
        # Validate passwords match
        password_error = validate_passwords_match(
            input, PublicAmbassadorCreationResponse)
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
                is_active=False,
            )

            # Create ambassador (inactive by default)
            ambassador = await Ambassador.objects._create(
                user=user,
                address=input.address,
                coordinates=input.coordinates or [],
                is_active=False,  # Requires manual approval
                created_by=user,
                updated_by=user,
            )

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
        password_error = validate_passwords_match(
            input, AcceptInvitationResponse)
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


class ApproveAmbassadorService(BaseAmbassadorService):
    """Service for approving ambassadors."""

    @classmethod
    async def approve(
        cls,
        input: inputs.ApproveAmbassadorInput,
        info: strawberry.Info,
    ) -> ApproveAmbassadorResponse:
        """Approve an ambassador and optionally assign to tenant."""
        user = info.context.request.user

        try:
            ambassador = await Ambassador.objects._by_id(input.ambassador_id)
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
                    tenant = await Tenant.objects._get(id=input.tenant_id)

                    await cls.assign_user_to_tenant(
                        user=ambassador.user,
                        tenant=tenant,
                        created_by=user,
                    )
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
            ambassador = await Ambassador.objects._by_id(input.ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError):
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
            invitation = await AmbassadorInvitation.objects._by_id(input.invitation_id)
        except (AmbassadorInvitation.DoesNotExist, ValueError, TypeError):
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
            not is_spark_request or tenant_id_input is not None or tenant_uuid_input is not None
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
                "tenant", "invited_by")

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
            not is_spark_request or tenant_id_input is not None or tenant_uuid_input is not None
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

                # Search by user email
                if filters.email:
                    queryset = queryset.filter(
                        user__email__icontains=filters.email)

                # Search by user name
                if filters.name:
                    queryset = queryset.filter(
                        Q(user__first_name__icontains=filters.name)
                        | Q(user__last_name__icontains=filters.name)
                    )

                # Search by address
                if filters.address:
                    queryset = queryset.filter(
                        address__icontains=filters.address)

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
            ambassador = await Ambassador.objects._by_id(input.ambassador_id)
        except (Ambassador.DoesNotExist, ValueError, TypeError):
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
                @sync_to_async
                def get_client():
                    return Client.objects.get(pk=int(input.client_id))
                client = await get_client()
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
            @sync_to_async
            def check_duplicate():
                return AmbassadorReview.objects.filter(
                    ambassador=ambassador,
                    client=client,
                ).exists()
            if await check_duplicate():
                return build_mutation_response(
                    CreateAmbassadorReviewResponse,
                    success=False,
                    message="A review for this ambassador and client already exists.",
                    input_obj=input,
                )

        # Create review
        try:
            @sync_to_async
            def create_review():
                return AmbassadorReview.objects.create(
                    ambassador=ambassador,
                    client=client,
                    tenant=tenant,
                    review=input.review,
                    score=input.score,
                    created_by=user,
                    updated_by=user,
                )

            review = await create_review()

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
            @sync_to_async
            def get_review():
                return AmbassadorReview.objects.select_related(
                    "ambassador", "client", "tenant"
                ).get(pk=int(input.review_id))
            review = await get_review()
        except (AmbassadorReview.DoesNotExist, ValueError, TypeError):
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
            @sync_to_async
            def update_review():
                if input.review is not None:
                    review.review = input.review
                if input.score is not None:
                    review.score = input.score
                review.updated_by = user
                review.save()
                return review

            review = await update_review()

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
            @sync_to_async
            def get_review():
                return AmbassadorReview.objects.get(pk=int(input.review_id))
            review = await get_review()
        except (AmbassadorReview.DoesNotExist, ValueError, TypeError):
            return build_mutation_response(
                DeleteAmbassadorReviewResponse,
                success=False,
                message="Review not found.",
                input_obj=input,
            )

        try:
            @sync_to_async
            def delete_review():
                review.delete()

            await delete_review()

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
            not is_spark_request or tenant_id_input is not None or tenant_uuid_input is not None
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
                    queryset = queryset.filter(
                        ambassador_id=int(filters.ambassador_id))

                # Filter by client
                if filters.client_id:
                    queryset = queryset.filter(
                        client_id=int(filters.client_id))

                # Filter by score range
                if filters.min_score is not None:
                    queryset = queryset.filter(score__gte=filters.min_score)
                if filters.max_score is not None:
                    queryset = queryset.filter(score__lte=filters.max_score)

                # Filter by date range
                if filters.start_date:
                    try:
                        start_datetime = datetime.fromisoformat(
                            filters.start_date.replace('Z', '+00:00'))
                        queryset = queryset.filter(
                            created_at__gte=start_datetime)
                    except (ValueError, AttributeError):
                        pass  # Invalid date format, skip filter
                if filters.end_date:
                    try:
                        end_datetime = datetime.fromisoformat(
                            filters.end_date.replace('Z', '+00:00'))
                        queryset = queryset.filter(
                            created_at__lte=end_datetime)
                    except (ValueError, AttributeError):
                        pass  # Invalid date format, skip filter

                # Search in review text
                if filters.search:
                    queryset = queryset.filter(
                        review__icontains=filters.search)

            return queryset.order_by("-created_at")

        return await get_queryset()
