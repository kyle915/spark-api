"""
GraphQL mutations for Google Calendar OAuth integration.
"""
import logging
import strawberry
from strawberry import relay
from asgiref.sync import sync_to_async
from google.auth.transport.requests import Request

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import build_mutation_response
from tenants.models import User

from .service import GoogleCalendarService
from .types import (
    ConnectGoogleCalendarResponse,
    GoogleCalendarCallbackResponse,
    DisconnectGoogleCalendarResponse,
)
from .inputs import (
    ConnectGoogleCalendarInput,
    GoogleCalendarCallbackInput,
    DisconnectGoogleCalendarInput,
)
from .utils import GoogleCalendarOAuthHelper

ensure_relay_mutation()

logger = logging.getLogger(__name__)


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
            existing_connection = await GoogleCalendarOAuthHelper.get_active_connection(user)
            if existing_connection:
                # Determine if the existing connection is actually working
                service = GoogleCalendarService(user)
                is_working = await sync_to_async(service.test_connection)()

                if is_working:
                    # Active and working connection - require explicit disconnect first
                    return build_mutation_response(
                        ConnectGoogleCalendarResponse,
                        success=False,
                        message="You already have an active Google Calendar connection. Please disconnect first.",
                        input_obj=input,
                    )

                # Connection exists but is not working (tokens expired/revoked)
                # Mark it as inactive so the user can reconnect cleanly
                existing_connection.is_active = False
                existing_connection.updated_by = user
                await sync_to_async(existing_connection.save)()

            # Create OAuth flow and generate state
            flow = GoogleCalendarOAuthHelper.create_oauth_flow()
            state = GoogleCalendarOAuthHelper.generate_state_token()

            # Store state for validation
            GoogleCalendarOAuthHelper.store_state_in_cache(state, user.id)

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
            logger.error(
                f"Error initiating Google Calendar connection: {e}", exc_info=True)
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

            # Validate state parameter
            if not GoogleCalendarOAuthHelper.validate_and_consume_state(input.state, user.id):
                return build_mutation_response(
                    GoogleCalendarCallbackResponse,
                    success=False,
                    message="Invalid state parameter. Please try connecting again.",
                    input_obj=input,
                )

            # Create OAuth flow and exchange code for tokens
            flow = GoogleCalendarOAuthHelper.create_oauth_flow()
            flow.fetch_token(code=input.code)
            credentials = flow.credentials

            # Get or create connection (reuse existing even if inactive)
            connection, is_new = await GoogleCalendarOAuthHelper.get_or_create_connection(user)

            # Update connection with new tokens
            GoogleCalendarOAuthHelper.update_connection_with_tokens(
                connection, credentials, user
            )
            await sync_to_async(connection.save)()

            logger.info(
                f"Google Calendar connected for user {user.id} ({'new' if is_new else 'reconnected'})")

            return build_mutation_response(
                GoogleCalendarCallbackResponse,
                success=True,
                message="Google Calendar connected successfully.",
                input_obj=input,
            )
        except Exception as e:
            logger.error(
                f"Error in Google Calendar callback: {e}", exc_info=True)
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
            connection = await GoogleCalendarOAuthHelper.get_active_connection(user)
            if not connection:
                return build_mutation_response(
                    DisconnectGoogleCalendarResponse,
                    success=False,
                    message="No active Google Calendar connection found.",
                    input_obj=input,
                )

            # Revoke token with Google (non-blocking)
            try:
                credentials = GoogleCalendarOAuthHelper.create_credentials_for_revocation(
                    connection)
                credentials.revoke(Request())
            except Exception as e:
                logger.warning(
                    f"Failed to revoke Google token for user {user.id}: {e}")
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
            logger.error(
                f"Error disconnecting Google Calendar: {e}", exc_info=True)
            return build_mutation_response(
                DisconnectGoogleCalendarResponse,
                success=False,
                message=f"Failed to disconnect Google Calendar: {str(e)}",
                input_obj=input,
            )
