import strawberry
from strawberry import relay
from enum import Enum
from django.contrib.auth import get_user_model
from gqlauth.core.utils import get_token
from asgiref.sync import sync_to_async

from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import ROLE_ID
from .models import Role, TenantedUser, Tenant
from .social_auth import BaseSocialAuthMutations, SocialAuthResponse

User = get_user_model()
ensure_relay_mutation()


@strawberry.type
class RegisterResponse:
    success: bool
    message: str
    activation_token: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class BaseRegisterInput(SparkGraphQLInput):
    first_name: str
    email: str
    password1: str
    password2: str


@strawberry.enum
class UserRoleEnum(Enum):
    AMBASSADOR = "ambassador"


@strawberry.input
class ClientRegisterInput(BaseRegisterInput):
    role: UserRoleEnum
    tenant_id: strawberry.ID | None = None


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


async def register_user_with_role(
    first_name: str,
    email: str,
    password1: str,
    password2: str,
    role_id: int,
    tenant_id: int | None = None,
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
                        user=user,
                        tenant=tenant,
                        is_active=True
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

    activation_token: str = await sync_to_async(get_token)(user, "activation")

    return RegisterResponse(
        success=True,
        message="User registered successfully. Please verify your email.",
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
