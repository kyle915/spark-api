import strawberry
from django.contrib.auth import get_user_model
from .models import Role
from gqlauth.user.queries import UserType
from gqlauth.core.utils import get_token
from asgiref.sync import sync_to_async

User = get_user_model()


@strawberry.type
class RegisterResponse:
    success: bool
    message: str
    user: UserType | None
    activation_token: str | None = None


async def register_user_with_role(
    email: str,
    password1: str,
    password2: str,
    role_id: int,
) -> RegisterResponse:
    if password1 != password2:
        return RegisterResponse(success=False, message="Passwords do not match.", user=None)

    if await sync_to_async(User.objects.filter(email=email).exists)():
        return RegisterResponse(success=False, message="Email already exists.", user=None)

    try:
        role = await sync_to_async(Role.objects.get)(pk=role_id)
    except Role.DoesNotExist:
        return RegisterResponse(success=False, message="Invalid roleId.", user=None)

    try:
        @sync_to_async
        def create_user():
            return User.objects.create_user(
                username=email,
                email=email,
                password=password1,
                role=role,
                is_active=True,
            )

        user = await create_user()
    except Exception as e:
        return RegisterResponse(success=False, message=f"Error creating user: {e}", user=None)

    activation_token = await sync_to_async(get_token)(user, "activation")

    return RegisterResponse(
        success=True,
        message="User registered successfully. Please verify your email.",
        user=user,
        activation_token=activation_token,
    )


# Ambassadors - role_id = 1
@strawberry.type
class AmbassadorsCustomRegister:
    @strawberry.mutation
    async def register(
        self,
        email: str,
        password1: str,
        password2: str,
    ) -> RegisterResponse:
        return await register_user_with_role(email, password1, password2, role_id=1)


# Spark Admin - role_id = 2
@strawberry.type
class SparkCustomRegister:
    @strawberry.mutation
    async def register(
        self,
        email: str,
        password1: str,
        password2: str,
    ) -> RegisterResponse:
        return await register_user_with_role(email, password1, password2, role_id=2)


# Clients - variable role_id
@strawberry.type
class ClientsCustomRegister:
    @strawberry.mutation
    async def register(
        self,
        email: str,
        password1: str,
        password2: str,
        role_id: strawberry.ID,
    ) -> RegisterResponse:
        return await register_user_with_role(email, password1, password2, role_id=int(role_id))