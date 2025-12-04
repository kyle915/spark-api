"""
GraphQL mutations for Google Calendar OAuth integration.
"""
import logging
import secrets
import strawberry
from strawberry import relay
from graphql import GraphQLError
from django.conf import settings
from django.core.cache import cache
from asgiref.sync import sync_to_async
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import build_mutation_response
from .models import GoogleCalendarConnection, User

ensure_relay_mutation()

logger = logging.getLogger(__name__)

# Google OAuth 2.0 scopes
SCOPES = ['https://www.googleapis.com/auth/calendar.events']


@strawberry.type
class ConnectGoogleCalendarResponse:
    """Response for connecting Google Calendar."""
    success: bool
    message: str
    authorization_url: str | None = None
    state: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class GoogleCalendarCallbackResponse:
    """Response for Google Calendar OAuth callback."""
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class DisconnectGoogleCalendarResponse:
    """Response for disconnecting Google Calendar."""
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class ConnectGoogleCalendarInput(SparkGraphQLInput):
    """Input for connecting Google Calendar."""
    pass


@strawberry.input
class GoogleCalendarCallbackInput(SparkGraphQLInput):
    """Input for Google Calendar OAuth callback."""
    code: str
    state: str


@strawberry.input
class DisconnectGoogleCalendarInput(SparkGraphQLInput):
    """Input for disconnecting Google Calendar."""
    pass


@strawberry.type
class GoogleCalendarMutations:
    """Mutations for Google Calendar integration."""

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def connect_google_calendar(
        self,
        info: strawberry.Info,
        input: ConnectGoogleCalendarInput,
    ) -> ConnectGoogleCalendarResponse:
        """
        Initiate Google Calendar OAuth connection.
        Returns authorization URL that user should visit.
        """
        try:
            user: User = info.context.request.user

            # Check if user already has an active connection
            existing_connection = await sync_to_async(
                GoogleCalendarConnection.objects.filter(
                    user=user,
                    is_active=True
                ).first
            )()

            if existing_connection:
                return build_mutation_response(
                    ConnectGoogleCalendarResponse,
                    success=False,
                    message="You already have an active Google Calendar connection. Please disconnect first.",
                    input_obj=input,
                )

            # Create OAuth flow
            flow = Flow.from_client_config(
                {
                    "web": {
                        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
                    }
                },
                scopes=SCOPES,
            )
            flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI

            # Generate state for CSRF protection
            state = secrets.token_urlsafe(32)

            # Store state in cache with user ID for validation
            cache_key = f"google_calendar_oauth_state_{state}"
            cache.set(cache_key, user.id, timeout=600)  # 10 minutes

            # Get authorization URL
            authorization_url, _ = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='true',
                state=state,
                prompt='consent',  # Force consent to get refresh token
            )

            return build_mutation_response(
                ConnectGoogleCalendarResponse,
                success=True,
                message="Please visit the authorization URL to connect your Google Calendar.",
                input_obj=input,
                authorization_url=authorization_url,
                state=state,
            )
        except Exception as e:
            logger.error(f"Error initiating Google Calendar connection: {e}")
            return build_mutation_response(
                ConnectGoogleCalendarResponse,
                success=False,
                message=f"Failed to initiate Google Calendar connection: {str(e)}",
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def google_calendar_callback(
        self,
        info: strawberry.Info,
        input: GoogleCalendarCallbackInput,
    ) -> GoogleCalendarCallbackResponse:
        """
        Handle Google Calendar OAuth callback.
        Exchanges authorization code for tokens and stores them.
        """
        try:
            user: User = info.context.request.user

            # Validate state
            cache_key = f"google_calendar_oauth_state_{input.state}"
            cached_user_id = cache.get(cache_key)

            if not cached_user_id or cached_user_id != user.id:
                return build_mutation_response(
                    GoogleCalendarCallbackResponse,
                    success=False,
                    message="Invalid state parameter. Please try connecting again.",
                    input_obj=input,
                )

            # Delete state from cache
            cache.delete(cache_key)

            # Create OAuth flow
            flow = Flow.from_client_config(
                {
                    "web": {
                        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
                    }
                },
                scopes=SCOPES,
            )
            flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI

            # Exchange authorization code for tokens
            flow.fetch_token(code=input.code)
            credentials = flow.credentials

            # Deactivate any existing connections
            await sync_to_async(
                GoogleCalendarConnection.objects.filter(
                    user=user,
                    is_active=True
                ).update
            )(is_active=False)

            # Create new connection
            connection = GoogleCalendarConnection(
                user=user,
                created_by=user,
                updated_by=user,
                calendar_id='primary',
                is_active=True,
                token_expiry=credentials.expiry if credentials.expiry else None,
            )
            connection.set_access_token(credentials.token)
            if credentials.refresh_token:
                connection.set_refresh_token(credentials.refresh_token)

            await sync_to_async(connection.save)()

            logger.info(f"Google Calendar connected for user {user.id}")

            return build_mutation_response(
                GoogleCalendarCallbackResponse,
                success=True,
                message="Google Calendar connected successfully.",
                input_obj=input,
            )
        except Exception as e:
            logger.error(f"Error in Google Calendar callback: {e}")
            return build_mutation_response(
                GoogleCalendarCallbackResponse,
                success=False,
                message=f"Failed to connect Google Calendar: {str(e)}",
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def disconnect_google_calendar(
        self,
        info: strawberry.Info,
        input: DisconnectGoogleCalendarInput,
    ) -> DisconnectGoogleCalendarResponse:
        """
        Disconnect Google Calendar by revoking access and deactivating connection.
        """
        try:
            user: User = info.context.request.user

            # Get active connection
            connection = await sync_to_async(
                GoogleCalendarConnection.objects.filter(
                    user=user,
                    is_active=True
                ).first
            )()

            if not connection:
                return build_mutation_response(
                    DisconnectGoogleCalendarResponse,
                    success=False,
                    message="No active Google Calendar connection found.",
                    input_obj=input,
                )

            # Revoke token with Google
            try:
                credentials = Credentials(
                    token=connection.get_access_token(),
                    refresh_token=connection.get_refresh_token(),
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
                    client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
                )
                credentials.revoke(Request())
            except Exception as e:
                logger.warning(f"Failed to revoke Google token: {e}")
                # Continue with deactivation even if revocation fails

            # Deactivate connection
            connection.is_active = False
            connection.updated_by = user
            await sync_to_async(connection.save)()

            logger.info(f"Google Calendar disconnected for user {user.id}")

            return build_mutation_response(
                DisconnectGoogleCalendarResponse,
                success=True,
                message="Google Calendar disconnected successfully.",
                input_obj=input,
            )
        except Exception as e:
            logger.error(f"Error disconnecting Google Calendar: {e}")
            return build_mutation_response(
                DisconnectGoogleCalendarResponse,
                success=False,
                message=f"Failed to disconnect Google Calendar: {str(e)}",
                input_obj=input,
            )
