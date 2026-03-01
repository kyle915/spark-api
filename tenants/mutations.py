import strawberry
from strawberry import relay
from enum import Enum
from datetime import timedelta
from graphql import GraphQLError
from django.contrib.auth import get_user_model
from gqlauth.core.utils import get_token
from asgiref.sync import sync_to_async
import random
import secrets
import string
from django.utils.text import slugify
from django.utils import timezone
from gqlauth.models import UserStatus
from django.conf import settings
from django.db import transaction

from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.relay import ensure_relay_mutation
from utils.graphql.mixins import resolve_id_to_int
from utils.utils import ROLE_ID
from utils.gcs import delete_blob, extract_blob_name_from_url
from .models import Role, TenantedUser, Tenant, TenantTheme, PasswordResetCode
from .types import TenantType, TenantThemeType
from .inputs import CreateOrUpdateTenantThemeInput
from .social_auth import BaseSocialAuthMutations, SocialAuthResponse
from .envelopes import EmailVerificationMailer, ForgotPasswordCodeMailer
from events.models import EventStatus, EventType, RequestStatus, RequestType
from jobs.models import Status as JobStatus, RateType
from recaps.models import FileRecapCategory, TypeOfGood
from ambassadors.models import AttendanceStatus, Skill

User = get_user_model()
ensure_relay_mutation()

DEFAULT_STATUS_TEMPLATES = [
    {"name": "Pending", "is_default": True},
    {"name": "Approved", "is_default": False},
    {"name": "Declined", "is_default": False},
    {"name": "Archived", "is_default": False},
    {"name": "Suspended", "is_default": False},
]
DEFAULT_JOB_STATUS_TEMPLATES = [
    {"name": "Pending", "slug": "pending"},
    {"name": "Approved", "slug": "approved"},
    {"name": "Declined", "slug": "declined"},
    {"name": "Invited", "slug": "invited"},
    {"name": "Complete", "slug": "complete"},
]
DEFAULT_ATTENDANCE_STATUS_TEMPLATES = [
    {"name": "Pending", "slug": "pending"},
    {"name": "Approved", "slug": "approved"},
    {"name": "Declined", "slug": "declined"},
]

DEFAULT_EVENT_TYPES = [
    {"name": "Sampling", "slug": "sampling", "is_default": True},
    {"name": "Promotion", "slug": "promotion", "is_default": False},
    {"name": "Launch", "slug": "launch", "is_default": False},
    {"name": "Special Event", "slug": "special-event", "is_default": False},
]

DEFAULT_REQUEST_TYPES = [
    "Event Activation",
    "On-Premise",
    "Retail Sampling",
    "Bar Sampling",
]

DEFAULT_RATE_TYPES = ["Hour", "Day", "Week"]

DEFAULT_FILE_RECAP_CATEGORIES = [
    "Sampling photos",
    "Table setup",
    "Receipts",
]

DEFAULT_TYPES_OF_GOOD = ["Can", "Pack"]
DEFAULT_SKILLS = [
    "Communication",
    "Teamwork",
    "Leadership",
    "Time Management",
    "Problem Solving",
]


@strawberry.type
class RegisterResponse:
    success: bool
    message: str
    activation_token: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class UpdateUserResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class BaseRegisterInput(SparkGraphQLInput):
    first_name: str
    email: str
    password1: str
    password2: str
    image: str | None = None


@strawberry.enum
class UserRoleEnum(Enum):
    AMBASSADOR = "ambassador"
    CLIENT = "client"
    SPARK = "spark-admin"


@strawberry.input
class ClientRegisterInput(BaseRegisterInput):
    role: UserRoleEnum
    tenant_id: strawberry.ID | None = None


@strawberry.input
class AmbassadorRegisterInput(BaseRegisterInput):
    role: UserRoleEnum
    tenant_id: strawberry.ID | None = None


@strawberry.input
class CreateUserInput(BaseRegisterInput):
    role: UserRoleEnum
    tenant_id: strawberry.ID | None = None


@strawberry.input
class UpdateUserInput(SparkGraphQLInput):
    id: strawberry.ID | None = None
    uuid: strawberry.ID | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    role: UserRoleEnum | None = None
    tenant_id: strawberry.ID | None = None
    image: str | None = None


@strawberry.input
class ChangeUserPasswordInput(SparkGraphQLInput):
    id: strawberry.ID | None = None
    uuid: strawberry.ID | None = None
    password1: str
    password2: str


@strawberry.input
class ChangeOwnPasswordInput(SparkGraphQLInput):
    password1: str
    password2: str


@strawberry.input
class ForgotPasswordInput(SparkGraphQLInput):
    email: str


@strawberry.input
class ResetPasswordWithCodeInput(SparkGraphQLInput):
    email: str
    code: str
    password1: str
    password2: str


@strawberry.input
class GoogleSocialAuthInput(SparkGraphQLInput):
    access_token: str


@strawberry.input
class AppleSocialAuthInput(SparkGraphQLInput):
    identity_token: str


@strawberry.input
class ClientAppleSocialAuthInput(AppleSocialAuthInput):
    role_id: strawberry.ID
    tenant_id: strawberry.ID


async def _check_client_or_spark_admin(request_user):
    """Allow spark-admins and clients; return tuple (allowed, is_spark_admin, is_client, error_message)."""
    if not request_user.is_authenticated:
        return False, False, False, "User not authenticated."

    try:
        role_slug = request_user.role.slug if request_user.role else None
        is_spark_admin = role_slug == Role.SPARK_ADMIN_SLUG
        is_client = role_slug == Role.CLIENT_SLUG
    except Exception as exc:
        return False, False, False, f"Error checking permissions: {exc}"

    if not (is_spark_admin or is_client):
        return (
            False,
            is_spark_admin,
            is_client,
            "You do not have permission to perform this action.",
        )

    return True, is_spark_admin, is_client, None


async def _get_active_tenant_ids(user) -> list[int]:
    return await sync_to_async(list)(
        user.tenanted_users.filter(is_active=True).values_list("tenant_id", flat=True)
    )


async def register_user_with_role(
    first_name: str,
    email: str,
    password1: str,
    password2: str,
    role_id: int,
    tenant_id: int | None = None,
    image: str | None = None,
    auto_verify: bool = False,
    client_mutation_id: strawberry.ID | None = None,
) -> RegisterResponse:
    if password1 != password2:
        return RegisterResponse(
            success=False,
            message="Passwords do not match.",
            client_mutation_id=client_mutation_id,
        )

    if await sync_to_async(User.objects.filter(email=email).exists)():
        return RegisterResponse(
            success=False,
            message="Email already exists.",
            client_mutation_id=client_mutation_id,
        )

    try:
        role: Role = await sync_to_async(Role.objects.get)(pk=role_id)
    except Role.DoesNotExist:
        return RegisterResponse(
            success=False,
            message="Invalid roleId.",
            client_mutation_id=client_mutation_id,
        )

    try:

        @sync_to_async
        def create_user():
            user = User.objects.create(
                first_name=first_name,
                username=email,
                email=email,
                image=image,
                role=role,
                is_active=True,
            )
            user.set_password(password1)
            user.save()
            return user

        user = await create_user()

        if user and tenant_id:
            try:
                tenant: Tenant = await sync_to_async(Tenant.objects.get)(pk=tenant_id)

                @sync_to_async
                def create_tenant_user():
                    tenant_user: TenantedUser = TenantedUser.objects.create(
                        user=user, tenant=tenant, is_active=True
                    )
                    tenant_user.save()
                    return tenant_user

                await create_tenant_user()
            except Exception as e:
                return RegisterResponse(
                    success=False,
                    message=f"Error creating tenant-user: {e}",
                    client_mutation_id=client_mutation_id,
                )
    except Exception as e:
        return RegisterResponse(
            success=False,
            message=f"Error creating user: {e}",
            client_mutation_id=client_mutation_id,
        )

    activation_token: str | None = None

    if auto_verify:
        await sync_to_async(UserStatus.objects.update_or_create)(
            user=user, defaults={"verified": True, "archived": False}
        )
    else:
        activation_token = await sync_to_async(get_token)(user, "activation")
        frontend_url = {
            "client": settings.CLIENT_FRONTEND_URL,
            "ambassador": settings.AMBASSADOR_FRONTEND_URL,
            "spark-admin": settings.ADMIN_FRONTEND_URL,
        }
        activation_url = (
            f"{frontend_url[role.slug]}/verify-account?token={activation_token}"
        )
        verification_email = EmailVerificationMailer(user, activation_url)
        await verification_email.send_async()

    message = (
        "User registered successfully."
        if auto_verify
        else "User registered successfully. Please verify your email."
    )

    return RegisterResponse(
        success=True,
        message=message,
        activation_token=activation_token,
        client_mutation_id=client_mutation_id,
    )


# Ambassadors - role_id = 1
@strawberry.type
class AmbassadorsCustomRegister:
    @relay.mutation
    async def register(
        self,
        info: strawberry.Info,
        input: AmbassadorRegisterInput,
    ) -> RegisterResponse:
        # Resolve role_id from the enum slug
        try:
            role = await sync_to_async(Role.objects.get)(slug=input.role.value)
            resolved_role_id = role.id
        except Role.DoesNotExist:
            return RegisterResponse(
                success=False,
                message=f"Invalid role: {input.role.value}",
                client_mutation_id=input.client_mutation_id,
            )

        # Handle optional tenant_id
        resolved_tenant_id = (
            resolve_id_to_int(input.tenant_id) if input.tenant_id else None
        )

        return await register_user_with_role(
            first_name=input.first_name,
            email=input.email,
            password1=input.password1,
            password2=input.password2,
            role_id=resolved_role_id,
            image=input.image,
            tenant_id=resolved_tenant_id,
            auto_verify=True,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def social_auth_google(
        self,
        info: strawberry.Info,
        input: GoogleSocialAuthInput,
    ) -> SocialAuthResponse:
        return await BaseSocialAuthMutations.social_auth_google(
            access_token=input.access_token,
            role_id=ROLE_ID.Ambassadors,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def social_auth_apple(
        self,
        info: strawberry.Info,
        input: AppleSocialAuthInput,
    ) -> SocialAuthResponse:
        return await BaseSocialAuthMutations.social_auth_apple(
            identity_token=input.identity_token,
            role_id=ROLE_ID.Ambassadors,
            client_mutation_id=input.client_mutation_id,
        )


# Spark Admin - role_id = 2
@strawberry.type
class SparkCustomRegister:
    @relay.mutation
    async def register(
        self,
        info: strawberry.Info,
        input: BaseRegisterInput,
    ) -> RegisterResponse:
        return await register_user_with_role(
            first_name=input.first_name,
            email=input.email,
            password1=input.password1,
            password2=input.password2,
            role_id=ROLE_ID.SparkAdmin,
            image=input.image,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def social_auth_google(
        self,
        info: strawberry.Info,
        input: GoogleSocialAuthInput,
    ) -> SocialAuthResponse:
        return await BaseSocialAuthMutations.social_auth_google(
            access_token=input.access_token,
            role_id=ROLE_ID.SparkAdmin,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def social_auth_apple(
        self,
        info: strawberry.Info,
        input: AppleSocialAuthInput,
    ) -> SocialAuthResponse:
        return await BaseSocialAuthMutations.social_auth_apple(
            identity_token=input.identity_token,
            role_id=ROLE_ID.SparkAdmin,
            client_mutation_id=input.client_mutation_id,
        )


@strawberry.type
class SparkUserMutations:
    @relay.mutation
    async def forgot_password(
        self,
        info: strawberry.Info,
        input: ForgotPasswordInput,
    ) -> UpdateUserResponse:
        email = input.email.strip().lower()
        generic_message = (
            "If the email exists, we have sent a 4-digit verification code."
        )

        if not email:
            return UpdateUserResponse(
                success=False,
                message="Email is required.",
                client_mutation_id=input.client_mutation_id,
            )

        user = await sync_to_async(
            lambda: User.objects.filter(email__iexact=email)
            .select_related("role")
            .first()
        )()
        if not user:
            return UpdateUserResponse(
                success=True,
                message=generic_message,
                client_mutation_id=input.client_mutation_id,
            )

        expires_minutes = int(
            getattr(settings, "PASSWORD_RESET_CODE_EXPIRY_MINUTES", 15)
        )
        code = f"{secrets.randbelow(10000):04d}"
        expires_at = timezone.now() + timedelta(minutes=expires_minutes)

        @sync_to_async
        def create_reset_code():
            with transaction.atomic():
                PasswordResetCode.objects.filter(user=user, is_used=False).update(
                    is_used=True,
                    used_at=timezone.now(),
                )
                return PasswordResetCode.objects.create(
                    user=user,
                    code=code,
                    expires_at=expires_at,
                )

        await create_reset_code()

        try:
            forgot_password_mailer = ForgotPasswordCodeMailer(
                user=user,
                code=code,
                expires_minutes=expires_minutes,
            )
            await forgot_password_mailer.send_async()
        except Exception:
            return UpdateUserResponse(
                success=False,
                message="Unable to send verification code. Please try again.",
                client_mutation_id=input.client_mutation_id,
            )

        return UpdateUserResponse(
            success=True,
            message=generic_message,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def reset_password_with_code(
        self,
        info: strawberry.Info,
        input: ResetPasswordWithCodeInput,
    ) -> UpdateUserResponse:
        email = input.email.strip().lower()
        code = input.code.strip()
        if input.password1 != input.password2:
            return UpdateUserResponse(
                success=False,
                message="Passwords do not match.",
                client_mutation_id=input.client_mutation_id,
            )

        if not (len(code) == 4 and code.isdigit()):
            return UpdateUserResponse(
                success=False,
                message="Code must be a 4-digit number.",
                client_mutation_id=input.client_mutation_id,
            )

        @sync_to_async
        def reset_password():
            user = User.objects.filter(email__iexact=email).first()
            if not user:
                return False

            reset_code = (
                PasswordResetCode.objects.filter(
                    user=user,
                    code=code,
                    is_used=False,
                    expires_at__gt=timezone.now(),
                )
                .order_by("-created_at")
                .first()
            )
            if not reset_code:
                return False

            with transaction.atomic():
                user.set_password(input.password1)
                user.save(update_fields=["password"])
                reset_code.is_used = True
                reset_code.used_at = timezone.now()
                reset_code.save(update_fields=["is_used", "used_at"])
                PasswordResetCode.objects.filter(user=user, is_used=False).exclude(
                    pk=reset_code.pk
                ).update(is_used=True, used_at=timezone.now())
            return True

        was_reset = await reset_password()
        if not was_reset:
            return UpdateUserResponse(
                success=False,
                message="Invalid code or email.",
                client_mutation_id=input.client_mutation_id,
            )

        return UpdateUserResponse(
            success=True,
            message="Password updated successfully.",
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def create_user(
        self,
        info: strawberry.Info,
        input: CreateUserInput,
    ) -> RegisterResponse:
        user = info.context.request.user

        allowed, is_spark_admin, is_client, error = await _check_client_or_spark_admin(
            user
        )
        if not allowed:
            return RegisterResponse(
                success=False,
                message=error,
                client_mutation_id=input.client_mutation_id,
            )

        try:
            role = await sync_to_async(Role.objects.get)(slug=input.role.value)
            resolved_role_id = role.id
        except Role.DoesNotExist:
            return RegisterResponse(
                success=False,
                message=f"Invalid role: {input.role.value}",
                client_mutation_id=input.client_mutation_id,
            )

        try:
            resolved_tenant_id = (
                resolve_id_to_int(input.tenant_id) if input.tenant_id else None
            )
        except (TypeError, ValueError, GraphQLError):
            return RegisterResponse(
                success=False,
                message="Invalid tenantId.",
                client_mutation_id=input.client_mutation_id,
            )

        if is_client and input.role == UserRoleEnum.SPARK:
            return RegisterResponse(
                success=False,
                message="Clients cannot assign spark-admin role.",
                client_mutation_id=input.client_mutation_id,
            )

        if input.role == UserRoleEnum.CLIENT:
            if not resolved_tenant_id:
                return RegisterResponse(
                    success=False,
                    message="tenantId is required for client users.",
                    client_mutation_id=input.client_mutation_id,
                )

            tenant_exists = await sync_to_async(
                Tenant.objects.filter(pk=resolved_tenant_id).exists
            )()
            if not tenant_exists:
                return RegisterResponse(
                    success=False,
                    message="Tenant not found.",
                    client_mutation_id=input.client_mutation_id,
                )

        if not is_spark_admin:
            if not resolved_tenant_id:
                return RegisterResponse(
                    success=False,
                    message="tenantId is required for client mutations.",
                    client_mutation_id=input.client_mutation_id,
                )
            requester_tenants = await _get_active_tenant_ids(user)
            if resolved_tenant_id not in requester_tenants:
                return RegisterResponse(
                    success=False,
                    message="You do not have permission to manage this tenant.",
                    client_mutation_id=input.client_mutation_id,
                )

        return await register_user_with_role(
            first_name=input.first_name,
            email=input.email,
            password1=input.password1,
            password2=input.password2,
            role_id=resolved_role_id,
            image=input.image,
            tenant_id=resolved_tenant_id,
            auto_verify=True,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def update_user(
        self,
        info: strawberry.Info,
        input: UpdateUserInput,
    ) -> UpdateUserResponse:
        requester = info.context.request.user

        allowed, is_spark_admin, is_client, error = await _check_client_or_spark_admin(
            requester
        )
        if not allowed:
            return UpdateUserResponse(
                success=False,
                message=error,
                client_mutation_id=input.client_mutation_id,
            )

        if not input.id and not input.uuid:
            return UpdateUserResponse(
                success=False,
                message="Provide id or uuid to update a user.",
                client_mutation_id=input.client_mutation_id,
            )

        try:
            target_user_id = resolve_id_to_int(input.id) if input.id else None
            target_user = (
                await sync_to_async(User.objects.select_related("role").get)(
                    pk=target_user_id
                )
                if input.id
                else await sync_to_async(User.objects.select_related("role").get)(
                    uuid=input.uuid
                )
            )
        except (User.DoesNotExist, ValueError, TypeError, GraphQLError):
            return UpdateUserResponse(
                success=False,
                message="User not found.",
                client_mutation_id=input.client_mutation_id,
            )

        previous_image_name = target_user.image.name if target_user.image else None

        if input.email:
            email_exists = await sync_to_async(
                User.objects.exclude(pk=target_user.pk).filter(email=input.email).exists
            )()
            if email_exists:
                return UpdateUserResponse(
                    success=False,
                    message="Email already exists.",
                    client_mutation_id=input.client_mutation_id,
                )

        resolved_role = target_user.role
        if input.role:
            try:
                resolved_role = await sync_to_async(Role.objects.get)(
                    slug=input.role.value
                )
            except Role.DoesNotExist:
                return UpdateUserResponse(
                    success=False,
                    message=f"Invalid role: {input.role.value}",
                    client_mutation_id=input.client_mutation_id,
                )

        if is_client and resolved_role.slug == UserRoleEnum.SPARK.value:
            return UpdateUserResponse(
                success=False,
                message="Clients cannot assign spark-admin role.",
                client_mutation_id=input.client_mutation_id,
            )

        resolved_tenant_id: int | None = None
        if input.tenant_id:
            try:
                resolved_tenant_id = resolve_id_to_int(input.tenant_id)
            except (TypeError, ValueError, GraphQLError):
                return UpdateUserResponse(
                    success=False,
                    message="Invalid tenantId.",
                    client_mutation_id=input.client_mutation_id,
                )

        if resolved_role.slug == UserRoleEnum.CLIENT.value and not resolved_tenant_id:
            return UpdateUserResponse(
                success=False,
                message="tenantId is required for client users.",
                client_mutation_id=input.client_mutation_id,
            )

        if resolved_tenant_id:
            tenant_exists = await sync_to_async(
                Tenant.objects.filter(pk=resolved_tenant_id).exists
            )()
            if not tenant_exists:
                return UpdateUserResponse(
                    success=False,
                    message="Tenant not found.",
                    client_mutation_id=input.client_mutation_id,
                )

        requester_tenant_ids = await _get_active_tenant_ids(requester)
        target_user_tenant_ids = await sync_to_async(list)(
            target_user.tenanted_users.filter(is_active=True).values_list(
                "tenant_id", flat=True
            )
        )

        if not is_spark_admin:
            if resolved_tenant_id and resolved_tenant_id not in requester_tenant_ids:
                return UpdateUserResponse(
                    success=False,
                    message="You do not have permission to manage this tenant.",
                    client_mutation_id=input.client_mutation_id,
                )

            if not set(target_user_tenant_ids).intersection(requester_tenant_ids):
                return UpdateUserResponse(
                    success=False,
                    message="You do not have permission to update this user.",
                    client_mutation_id=input.client_mutation_id,
                )

        try:

            @sync_to_async
            def persist_updates():
                if input.first_name is not None:
                    target_user.first_name = input.first_name
                if input.last_name is not None:
                    target_user.last_name = input.last_name
                if input.email is not None:
                    target_user.email = input.email
                    target_user.username = input.email
                if input.image is not None:
                    target_user.image = input.image
                target_user.role = resolved_role
                target_user.save()
                return target_user

            await persist_updates()

            if (
                input.image is not None
                and previous_image_name
                and previous_image_name != input.image
            ):
                old_blob = extract_blob_name_from_url(previous_image_name)
                if old_blob:
                    await sync_to_async(delete_blob)(old_blob)

            if resolved_tenant_id:
                tenant = await sync_to_async(Tenant.objects.get)(pk=resolved_tenant_id)

                @sync_to_async
                def upsert_tenant_user():
                    return TenantedUser.objects.update_or_create(
                        user=target_user,
                        tenant=tenant,
                        defaults={
                            "is_active": True,
                            "created_by": requester,
                            "updated_by": requester,
                        },
                    )

                await upsert_tenant_user()

            return UpdateUserResponse(
                success=True,
                message="User updated successfully.",
                client_mutation_id=input.client_mutation_id,
            )
        except Exception as exc:
            return UpdateUserResponse(
                success=False,
                message=f"Error updating user: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

    @relay.mutation
    async def change_user_password(
        self,
        info: strawberry.Info,
        input: ChangeUserPasswordInput,
    ) -> UpdateUserResponse:
        requester = info.context.request.user

        allowed, is_spark_admin, _, error = await _check_client_or_spark_admin(
            requester
        )
        if not allowed:
            return UpdateUserResponse(
                success=False,
                message=error,
                client_mutation_id=input.client_mutation_id,
            )

        if not input.id and not input.uuid:
            return UpdateUserResponse(
                success=False,
                message="Provide id or uuid to change a user's password.",
                client_mutation_id=input.client_mutation_id,
            )

        if input.password1 != input.password2:
            return UpdateUserResponse(
                success=False,
                message="Passwords do not match.",
                client_mutation_id=input.client_mutation_id,
            )

        try:
            target_user_id = resolve_id_to_int(input.id) if input.id else None
            target_user = (
                await sync_to_async(User.objects.get)(pk=target_user_id)
                if input.id
                else await sync_to_async(User.objects.get)(uuid=input.uuid)
            )
        except (User.DoesNotExist, ValueError, TypeError, GraphQLError):
            return UpdateUserResponse(
                success=False,
                message="User not found.",
                client_mutation_id=input.client_mutation_id,
            )

        if not is_spark_admin:
            requester_tenant_ids = await _get_active_tenant_ids(requester)
            target_user_tenant_ids = await sync_to_async(list)(
                target_user.tenanted_users.filter(is_active=True).values_list(
                    "tenant_id", flat=True
                )
            )
            if not set(target_user_tenant_ids).intersection(requester_tenant_ids):
                return UpdateUserResponse(
                    success=False,
                    message="You do not have permission to update this user.",
                    client_mutation_id=input.client_mutation_id,
                )

        try:

            @sync_to_async
            def persist_password():
                target_user.set_password(input.password1)
                target_user.save()

            await persist_password()

            return UpdateUserResponse(
                success=True,
                message="Password updated successfully.",
                client_mutation_id=input.client_mutation_id,
            )
        except Exception as exc:
            return UpdateUserResponse(
                success=False,
                message=f"Error updating password: {exc}",
                client_mutation_id=input.client_mutation_id,
            )


@strawberry.type
class AmbassadorUserMutations:
    @relay.mutation
    async def change_user_password(
        self,
        info: strawberry.Info,
        input: ChangeOwnPasswordInput,
    ) -> UpdateUserResponse:
        requester = info.context.request.user

        if not requester.is_authenticated:
            return UpdateUserResponse(
                success=False,
                message="User not authenticated.",
                client_mutation_id=input.client_mutation_id,
            )

        if input.password1 != input.password2:
            return UpdateUserResponse(
                success=False,
                message="Passwords do not match.",
                client_mutation_id=input.client_mutation_id,
            )

        try:

            @sync_to_async
            def persist_password():
                requester.set_password(input.password1)
                requester.save()

            await persist_password()

            return UpdateUserResponse(
                success=True,
                message="Password updated successfully.",
                client_mutation_id=input.client_mutation_id,
            )
        except Exception as exc:
            return UpdateUserResponse(
                success=False,
                message=f"Error updating password: {exc}",
                client_mutation_id=input.client_mutation_id,
            )


# Clients - variable role_id
@strawberry.type
class ClientsCustomRegister:
    @relay.mutation
    async def register(
        self,
        info: strawberry.Info,
        input: ClientRegisterInput,
    ) -> RegisterResponse:
        # Resolve role_id from the enum slug
        try:
            role = await sync_to_async(Role.objects.get)(slug=input.role.value)
            resolved_role_id = role.id
        except Role.DoesNotExist:
            return RegisterResponse(
                success=False,
                message=f"Invalid role: {input.role.value}",
                client_mutation_id=input.client_mutation_id,
            )

        # Handle optional tenant_id
        resolved_tenant_id = (
            resolve_id_to_int(input.tenant_id) if input.tenant_id else None
        )

        return await register_user_with_role(
            first_name=input.first_name,
            email=input.email,
            password1=input.password1,
            password2=input.password2,
            role_id=resolved_role_id,
            image=input.image,
            tenant_id=resolved_tenant_id,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def social_auth_google(
        self,
        info: strawberry.Info,
        input: GoogleSocialAuthInput,
    ) -> SocialAuthResponse:
        return await BaseSocialAuthMutations.social_auth_google(
            access_token=input.access_token,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation
    async def social_auth_apple(
        self,
        info: strawberry.Info,
        input: AppleSocialAuthInput,
    ) -> SocialAuthResponse:
        return await BaseSocialAuthMutations.social_auth_apple(
            identity_token=input.identity_token,
            client_mutation_id=input.client_mutation_id,
        )


@strawberry.input
class CreateTenantInput(SparkGraphQLInput):
    name: str
    image: str | None = None


@strawberry.type
class CreateTenantResponse:
    success: bool
    message: str
    tenant: TenantType | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class UpdateTenantInput(SparkGraphQLInput):
    id: strawberry.ID
    name: str | None = None
    image: str | None = None


@strawberry.type
class UpdateTenantResponse:
    success: bool
    message: str
    tenant: TenantType | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class TenantThemeResponse:
    success: bool
    message: str
    theme: TenantThemeType | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class SparkTenantMutations:
    @relay.mutation
    async def create_tenant(
        self,
        info: strawberry.Info,
        input: CreateTenantInput,
    ) -> CreateTenantResponse:
        user = info.context.request.user

        if not user.is_authenticated:
            return CreateTenantResponse(
                success=False,
                message="User not authenticated.",
                client_mutation_id=input.client_mutation_id,
            )

        # Check if user is spark-admin
        try:
            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                return CreateTenantResponse(
                    success=False,
                    message="You do not have permission to perform this action.",
                    client_mutation_id=input.client_mutation_id,
                )
        except Exception as e:
            return CreateTenantResponse(
                success=False,
                message=f"Error checking permissions: {e}",
                client_mutation_id=input.client_mutation_id,
            )

        random_chars = "".join(
            random.choices(string.ascii_letters + string.digits, k=4)
        )
        slugified_name = slugify(input.name)
        request_url_name = f"{random_chars}-{slugified_name}".lower()

        try:

            @sync_to_async
            def create_tenant_record():
                with transaction.atomic():
                    tenant = Tenant.objects.create(
                        name=input.name,
                        request_url_name=request_url_name,
                        image=input.image,
                        created_by=user,
                    )

                    def create_statuses(
                        model_cls,
                        include_default_flag: bool,
                        templates=DEFAULT_STATUS_TEMPLATES,
                    ):
                        for status in templates:
                            status_slug = status.get("slug") or slugify(status["name"])
                            payload = {
                                "name": status["name"],
                                "slug": status_slug,
                                "tenant": tenant,
                                "created_by": user,
                            }
                            if include_default_flag:
                                payload["is_default"] = status["is_default"]
                            model_cls.objects.create(**payload)

                    # Status templates
                    create_statuses(RequestStatus, include_default_flag=True)
                    create_statuses(EventStatus, include_default_flag=True)
                    create_statuses(
                        JobStatus,
                        include_default_flag=False,
                        templates=DEFAULT_JOB_STATUS_TEMPLATES,
                    )
                    create_statuses(
                        AttendanceStatus,
                        include_default_flag=False,
                        templates=DEFAULT_ATTENDANCE_STATUS_TEMPLATES,
                    )

                    # Event types
                    for event_type in DEFAULT_EVENT_TYPES:
                        EventType.objects.create(
                            name=event_type["name"],
                            slug=event_type.get("slug") or slugify(event_type["name"]),
                            tenant=tenant,
                            created_by=user,
                            is_default=event_type["is_default"],
                        )

                    # Request types
                    for request_type in DEFAULT_REQUEST_TYPES:
                        RequestType.objects.create(
                            name=request_type,
                            tenant=tenant,
                            created_by=user,
                        )

                    # Rate types
                    for rate_type in DEFAULT_RATE_TYPES:
                        RateType.objects.create(
                            name=rate_type,
                            tenant=tenant,
                            created_by=user,
                        )

                    # Recap categories
                    for recap_category in DEFAULT_FILE_RECAP_CATEGORIES:
                        FileRecapCategory.objects.create(
                            name=recap_category,
                            tenant=tenant,
                            created_by=user,
                        )

                    # Types of good
                    for type_of_good in DEFAULT_TYPES_OF_GOOD:
                        TypeOfGood.objects.create(
                            name=type_of_good,
                            tenant=tenant,
                            created_by=user,
                        )

                    # Skills
                    for skill in DEFAULT_SKILLS:
                        Skill.objects.create(
                            name=skill,
                            tenant=tenant,
                            created_by=user,
                        )

                return tenant

            tenant = await create_tenant_record()

            return CreateTenantResponse(
                success=True,
                message="Tenant created successfully.",
                tenant=tenant,
                client_mutation_id=input.client_mutation_id,
            )

        except Exception as e:
            return CreateTenantResponse(
                success=False,
                message=f"Error creating tenant: {e}",
                client_mutation_id=input.client_mutation_id,
            )

    @relay.mutation
    async def update_tenant(
        self,
        info: strawberry.Info,
        input: UpdateTenantInput,
    ) -> UpdateTenantResponse:
        user = info.context.request.user

        if not user.is_authenticated:
            return UpdateTenantResponse(
                success=False,
                message="User not authenticated.",
                client_mutation_id=input.client_mutation_id,
            )

        # Check if user is spark-admin
        try:
            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                return UpdateTenantResponse(
                    success=False,
                    message="You do not have permission to perform this action.",
                    client_mutation_id=input.client_mutation_id,
                )
        except Exception as e:
            return UpdateTenantResponse(
                success=False,
                message=f"Error checking permissions: {e}",
                client_mutation_id=input.client_mutation_id,
            )

        try:
            tenant_id = resolve_id_to_int(input.id)
            tenant = await sync_to_async(Tenant.objects.get)(pk=tenant_id)
        except Tenant.DoesNotExist:
            return UpdateTenantResponse(
                success=False,
                message="Tenant not found.",
                client_mutation_id=input.client_mutation_id,
            )

        previous_image_name = tenant.image.name if tenant.image else None

        try:

            @sync_to_async
            def update_tenant_record():
                if input.name:
                    tenant.name = input.name
                    # Generate new request_url_name when name is updated
                    random_chars = "".join(
                        random.choices(string.ascii_letters + string.digits, k=4)
                    )
                    slugified_name = slugify(input.name)
                    tenant.request_url_name = f"{slugified_name}-{random_chars}".lower()
                if input.image is not None:
                    tenant.image = input.image

                tenant.updated_by = user
                tenant.save()
                return tenant

            updated_tenant = await update_tenant_record()

            if (
                input.image is not None
                and previous_image_name
                and previous_image_name != input.image
            ):
                old_blob = extract_blob_name_from_url(previous_image_name)
                if old_blob:
                    await sync_to_async(delete_blob)(old_blob)

            return UpdateTenantResponse(
                success=True,
                message="Tenant updated successfully.",
                tenant=updated_tenant,
                client_mutation_id=input.client_mutation_id,
            )
        except Exception as e:
            return UpdateTenantResponse(
                success=False,
                message=f"Error updating tenant: {e}",
                client_mutation_id=input.client_mutation_id,
            )


@strawberry.type
class TenantThemeMutations:
    @relay.mutation
    async def upsert_tenant_theme(
        self,
        info: strawberry.Info,
        input: CreateOrUpdateTenantThemeInput,
    ) -> TenantThemeResponse:
        """
        Create or update a TenantTheme for a given tenant and color scheme.

        Spark-admins can manage any tenant theme. Clients can manage themes for their own tenant(s).
        """
        user = info.context.request.user

        if not user.is_authenticated:
            return TenantThemeResponse(
                success=False,
                message="User not authenticated.",
                client_mutation_id=input.client_mutation_id,
                theme=None,
            )

        # Check if user is spark-admin or client
        try:
            is_spark_admin = await user.role.is_spark_admin
            is_client = await user.role.is_client
            if not (is_spark_admin or is_client):
                return TenantThemeResponse(
                    success=False,
                    message="You do not have permission to manage tenant themes.",
                    client_mutation_id=input.client_mutation_id,
                    theme=None,
                )
        except Exception as e:
            return TenantThemeResponse(
                success=False,
                message=f"Error checking permissions: {e}",
                client_mutation_id=input.client_mutation_id,
                theme=None,
            )

        try:
            resolved_tenant_id = resolve_id_to_int(input.tenant_id)
        except (TypeError, ValueError, GraphQLError):
            return TenantThemeResponse(
                success=False,
                message="Invalid tenantId.",
                client_mutation_id=input.client_mutation_id,
                theme=None,
            )

        if is_client:
            active_tenant_ids = await _get_active_tenant_ids(user)
            if resolved_tenant_id not in active_tenant_ids:
                return TenantThemeResponse(
                    success=False,
                    message="You do not have permission to manage this tenant theme.",
                    client_mutation_id=input.client_mutation_id,
                    theme=None,
                )

        # Resolve target tenant
        try:
            tenant = await sync_to_async(Tenant.objects.get)(pk=resolved_tenant_id)
        except Tenant.DoesNotExist:
            return TenantThemeResponse(
                success=False,
                message="Tenant not found.",
                client_mutation_id=input.client_mutation_id,
                theme=None,
            )

        # Upsert theme by (tenant, color_scheme)
        def _upsert_theme():
            defaults = {
                "name": input.name if input.name is not None else "default",
                "updated_by": user,
            }
            if input.css_variables is not None:
                defaults["css_variables"] = input.css_variables

            theme, created = TenantTheme.objects.update_or_create(
                tenant=tenant,
                color_scheme=input.color_scheme.value,
                defaults=defaults,
            )
            if created and theme.created_by_id is None:
                theme.created_by = user
                theme.save(update_fields=["created_by"])
            return theme

        try:
            theme = await sync_to_async(_upsert_theme)()
        except Exception as e:
            return TenantThemeResponse(
                success=False,
                message=f"Error saving tenant theme: {e}",
                client_mutation_id=input.client_mutation_id,
                theme=None,
            )

        return TenantThemeResponse(
            success=True,
            message="Tenant theme saved successfully.",
            client_mutation_id=input.client_mutation_id,
            theme=theme,
        )
