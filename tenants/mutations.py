import strawberry
from strawberry import relay
from enum import Enum
from django.contrib.auth import get_user_model
from gqlauth.core.utils import get_token
from asgiref.sync import sync_to_async
import random
import string
from django.utils.text import slugify
from gqlauth.models import UserStatus
from django.db import transaction

from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import ROLE_ID
from utils.gcs import delete_blob, extract_blob_name_from_url
from .models import Role, TenantedUser, Tenant, TenantTheme
from .types import TenantType, TenantThemeType
from .inputs import CreateOrUpdateTenantThemeInput
from .social_auth import BaseSocialAuthMutations, SocialAuthResponse
from events.models import EventStatus, EventType, RequestStatus, RequestType
from jobs.models import Status as JobStatus, RateType
from recaps.models import FileRecapCategory, TypeOfGood
from ambassadors.models import AttendanceStatus

User = get_user_model()
ensure_relay_mutation()

DEFAULT_STATUS_TEMPLATES = [
    {"name": "Pending", "is_default": True},
    {"name": "Approved", "is_default": False},
    {"name": "Declined", "is_default": False},
]

DEFAULT_EVENT_TYPES = [
    {"name": "Sampling", "is_default": True},
    {"name": "Promotion", "is_default": False},
    {"name": "Launch", "is_default": False},
    {"name": "Special Event", "is_default": False},
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
        user.tenanted_users.filter(
            is_active=True).values_list("tenant_id", flat=True)
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
        resolved_tenant_id = int(input.tenant_id) if input.tenant_id else None

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
            resolved_tenant_id = int(
                input.tenant_id) if input.tenant_id else None
        except (TypeError, ValueError):
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
            target_user = (
                await sync_to_async(User.objects.select_related("role").get)(
                    pk=int(input.id)
                )
                if input.id
                else await sync_to_async(User.objects.select_related("role").get)(
                    uuid=input.uuid
                )
            )
        except (User.DoesNotExist, ValueError, TypeError):
            return UpdateUserResponse(
                success=False,
                message="User not found.",
                client_mutation_id=input.client_mutation_id,
            )

        previous_image_name = target_user.image.name if target_user.image else None

        if input.email:
            email_exists = await sync_to_async(
                User.objects.exclude(pk=target_user.pk).filter(
                    email=input.email).exists
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
                resolved_tenant_id = int(input.tenant_id)
            except (TypeError, ValueError):
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
        resolved_tenant_id = int(input.tenant_id) if input.tenant_id else None

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

                    def create_statuses(model_cls, include_default_flag: bool):
                        for status in DEFAULT_STATUS_TEMPLATES:
                            status_slug = slugify(status["name"])
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
                    create_statuses(JobStatus, include_default_flag=False)
                    create_statuses(AttendanceStatus, include_default_flag=False)

                    # Event types
                    for event_type in DEFAULT_EVENT_TYPES:
                        EventType.objects.create(
                            name=event_type["name"],
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
            tenant = await sync_to_async(Tenant.objects.get)(pk=input.id)
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
                        random.choices(string.ascii_letters +
                                       string.digits, k=4)
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
            resolved_tenant_id = int(input.tenant_id)
        except (TypeError, ValueError):
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
