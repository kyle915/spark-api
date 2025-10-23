import strawberry
from django.contrib.auth import get_user_model
from .models import Role, TenantedUser, Tenant
from gqlauth.core.utils import get_token
from asgiref.sync import sync_to_async
from utils.utils import ROLE_ID

User = get_user_model()


@strawberry.type
class RegisterResponse:
    success: bool
    message: str
    activation_token: str | None = None


async def register_user_with_role(
    first_name: str,
    email: str,
    password1: str,
    password2: str,
    role_id: int,
    tenant_id: int | None = None,
) -> RegisterResponse:
    if password1 != password2:
        return RegisterResponse(success=False, message="Passwords do not match.")

    if await sync_to_async(User.objects.filter(email=email).exists)():
        return RegisterResponse(success=False, message="Email already exists.")

    try:
        role: Role = await sync_to_async(Role.objects.get)(pk=role_id)
    except Role.DoesNotExist:
        return RegisterResponse(success=False, message="Invalid roleId.")

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
                return RegisterResponse(success=False, message=f"Error creating tenant-user: {e}")
    except Exception as e:
        return RegisterResponse(success=False, message=f"Error creating user: {e}")

    activation_token: str = await sync_to_async(get_token)(user, "activation")

    return RegisterResponse(
        success=True,
        message="User registered successfully. Please verify your email.",
        activation_token=activation_token,
    )


# Ambassadors - role_id = 1
@strawberry.type
class AmbassadorsCustomRegister:
    @strawberry.mutation
    async def register(
        self,
        first_name: str,
        email: str,
        password1: str,
        password2: str,
    ) -> RegisterResponse:
        return await register_user_with_role(first_name, email, password1, password2, role_id=ROLE_ID.Ambassadors)


# Spark Admin - role_id = 2
@strawberry.type
class SparkCustomRegister:
    @strawberry.mutation
    async def register(
        self,
        first_name: str,
        email: str,
        password1: str,
        password2: str,
    ) -> RegisterResponse:
        return await register_user_with_role(first_name, email, password1, password2, role_id=ROLE_ID.SparkAdmin)


# Clients - variable role_id
@strawberry.type
class ClientsCustomRegister:
    @strawberry.mutation
    async def register(
        self,
        first_name: str,
        email: str,
        password1: str,
        password2: str,
        role_id: strawberry.ID,
        tenant_id: strawberry.ID
    ) -> RegisterResponse:
        return await register_user_with_role(first_name, email, password1, password2, role_id=int(role_id), tenant_id=int(tenant_id))