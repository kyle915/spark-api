import strawberry
from django.contrib.auth import get_user_model
from gqlauth.core.utils import get_token
from asgiref.sync import sync_to_async
from .models import Role, TenantedUser, Tenant
from gqlauth.models import RefreshToken
from utils.utils import ROLE_ID

from gqlauth.jwt.types_ import TokenType

User = get_user_model()


@strawberry.type
class SocialAuthResponse:
    success: bool
    message: str
    token: str | None = None
    refresh_token: str | None = None
    client_mutation_id: strawberry.ID | None = None


async def authenticate_or_create_social_user(
    email: str,
    first_name: str = "",
    last_name: str = "",
    provider: str = "google",
    role_id: int | None = None,
    tenant_id: int | None = None,
) -> tuple[User, bool]:
    """
    Authenticate existing user or create new one from social auth.
    Returns (user, is_new_user)

    This is based on the original one that was created for the authentication backend.
    The one located in the .mutations.py file.

    @TODO: Probably it would be better to use the same function for both authentication and social auth in the near future.
    """
    user_exists = await sync_to_async(User.objects.filter(email=email).exists)()

    if user_exists:
        user = await sync_to_async(User.objects.get)(email=email)
        return user, False
    else:
        # Create new user
        @sync_to_async
        def create_user():
            default_role_id = role_id or ROLE_ID.Ambassadors
            role = Role.objects.get(pk=default_role_id)

            user = User.objects.create(
                first_name=first_name,
                last_name=last_name,
                username=email,
                email=email,
                role=role,
                is_active=True,
            )
            return user

        user = await create_user()

        # Handle tenant association if needed
        if tenant_id:
            try:
                tenant = await sync_to_async(Tenant.objects.get)(pk=tenant_id)

                @sync_to_async
                def create_tenant_user():
                    return TenantedUser.objects.create(
                        user=user,
                        tenant=tenant,
                        is_active=True
                    )
                await create_tenant_user()
            except Exception:
                pass  # Log error if needed

        return user, True


class BaseSocialAuthMutations:
    @staticmethod
    async def social_auth_google(
        access_token: str,
        role_id: int | None = None,
        tenant_id: int | None = None,
        client_mutation_id: strawberry.ID | None = None,
    ) -> SocialAuthResponse:
        """
        Authenticate with Google OAuth access token.
        Returns JWT tokens compatible with strawberry-django-auth.
        """
        try:
            import httpx

            # Fetch user info from Google
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    'https://www.googleapis.com/oauth2/v2/userinfo',
                    headers={'Authorization': f'Bearer {access_token}'}
                )

                if response.status_code != 200:
                    return SocialAuthResponse(
                        success=False,
                        message="Invalid Google access token",
                        client_mutation_id=client_mutation_id,
                    )

                user_info = response.json()

            email = user_info.get('email')
            if not email:
                return SocialAuthResponse(
                    success=False,
                    message="Unable to get email from Google",
                    client_mutation_id=client_mutation_id,
                )

            # Authenticate or create user
            user, is_new = await authenticate_or_create_social_user(
                email=email,
                first_name=user_info.get('given_name', ''),
                last_name=user_info.get('family_name', ''),
                provider='google',
                role_id=role_id,
                tenant_id=tenant_id,
            )

            # Generate JWT tokens using gqlauth
            # token = await sync_to_async(get_token)(user, "authentication")
            token = TokenType.from_user(user)
            refresh_token_obj = await sync_to_async(RefreshToken.from_user)(user)
            refresh_token = refresh_token_obj.token

            message = "Google authentication successful"
            if is_new:
                message += " - Account created"

            return SocialAuthResponse(
                success=True,
                message=message,
                token=token.token,
                refresh_token=refresh_token,
                client_mutation_id=client_mutation_id,
            )

        except Exception as e:
            return SocialAuthResponse(
                success=False,
                message=f"Error during Google authentication: {str(e)}",
                client_mutation_id=client_mutation_id,
            )

    @staticmethod
    async def social_auth_apple(
        identity_token: str,
        role_id: int | None = None,
        tenant_id: int | None = None,
        client_mutation_id: strawberry.ID | None = None,
    ) -> SocialAuthResponse:
        """
        Authenticate with Apple Sign In identity token.
        Returns JWT tokens compatible with strawberry-django-auth.
        """
        try:
            import jwt

            # Decode identity token (you should verify signature in production)
            decoded_token = jwt.decode(
                identity_token,
                options={"verify_signature": False}
            )

            email = decoded_token.get('email')
            if not email:
                return SocialAuthResponse(
                    success=False,
                    message="Unable to get email from Apple token",
                    client_mutation_id=client_mutation_id,
                )

            # Get name from token if available
            name = decoded_token.get('name', {})
            first_name = name.get('firstName', '') if isinstance(
                name, dict) else ''
            last_name = name.get('lastName', '') if isinstance(
                name, dict) else ''

            # Authenticate or create user
            user, is_new = await authenticate_or_create_social_user(
                email=email,
                first_name=first_name,
                last_name=last_name,
                provider='apple',
                role_id=role_id,
                tenant_id=tenant_id,
            )

            # Generate JWT tokens using gqlauth
            token = await sync_to_async(get_token)(user, "authentication")
            refresh_token = await sync_to_async(get_token)(user, "refresh")

            message = "Apple authentication successful"
            if is_new:
                message += " - Account created"

            return SocialAuthResponse(
                success=True,
                message=message,
                token=token,
                refresh_token=refresh_token,
                client_mutation_id=client_mutation_id,
            )

        except Exception as e:
            return SocialAuthResponse(
                success=False,
                message=f"Error during Apple authentication: {str(e)}",
                client_mutation_id=client_mutation_id,
            )
