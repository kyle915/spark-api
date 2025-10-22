import strawberry
from django.contrib.auth import get_user_model
from django.db import transaction
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


@strawberry.type
class CustomMutation:
    @strawberry.mutation
    async def register(
        self,
        info,
        username: str,
        email: str,
        password1: str,
        password2: str,
        role_id: strawberry.ID,
    ) -> RegisterResponse:
        if password1 != password2:
            return RegisterResponse(success=False, message="Passwords do not match.", user=None)

        if await sync_to_async(User.objects.filter(username=username).exists)():
            return RegisterResponse(success=False, message="Username already exists.", user=None)

        try:
            role = await sync_to_async(Role.objects.get)(pk=role_id)
        except Role.DoesNotExist:
            return RegisterResponse(success=False, message="Invalid roleId.", user=None)

        try:
            @sync_to_async
            def create_user():
                return User.objects.create_user(
                    username=username,
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

# Register Ambasadour Mutation -> Setear el valor correcto que quede en produccion