"""
Utility classes and helper functions for Google Calendar OAuth operations.
"""
import secrets
from typing import Optional, Tuple
from django.conf import settings
from django.core.cache import cache
from asgiref.sync import sync_to_async
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from tenants.models import GoogleCalendarConnection, User
from .constants import (
    GOOGLE_CALENDAR_SCOPES,
    STATE_CACHE_TIMEOUT,
    STATE_CACHE_PREFIX,
    DEFAULT_CALENDAR_ID,
)


class GoogleCalendarOAuthHelper:
    """Helper class for Google Calendar OAuth operations."""

    @staticmethod
    def create_oauth_flow(client_origin: str) -> Flow:
        """
        Create a Google OAuth flow instance.

        Returns:
            Configured OAuth Flow instance
        """
        redirect_uri = f"{client_origin}/auth/google-calendar/callback"
        print(f"Redirect URI: {redirect_uri}")
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            scopes=GOOGLE_CALENDAR_SCOPES,
        )
        flow.redirect_uri = redirect_uri
        return flow

    @staticmethod
    def generate_state_token() -> str:
        """
        Generate a cryptographically secure state token for CSRF protection.

        Returns:
            URL-safe random token string
        """
        return secrets.token_urlsafe(32)

    @staticmethod
    def get_state_cache_key(state: str) -> str:
        """Get cache key for OAuth state token."""
        return f"{STATE_CACHE_PREFIX}{state}"

    @staticmethod
    def store_state_in_cache(state: str, user_id: int) -> None:
        """
        Store OAuth state token in cache for validation.

        Args:
            state: State token to store
            user_id: User ID to associate with state
        """
        cache_key = GoogleCalendarOAuthHelper.get_state_cache_key(state)
        cache.set(cache_key, user_id, timeout=STATE_CACHE_TIMEOUT)

    @staticmethod
    def validate_and_consume_state(state: str, user_id: int) -> bool:
        """
        Validate OAuth state token and remove it from cache.

        Args:
            state: State token to validate
            user_id: Expected user ID

        Returns:
            True if state is valid, False otherwise
        """
        cache_key = GoogleCalendarOAuthHelper.get_state_cache_key(state)
        cached_user_id = cache.get(cache_key)

        if not cached_user_id or cached_user_id != user_id:
            return False

        # Consume state by deleting from cache
        cache.delete(cache_key)
        return True

    @staticmethod
    async def get_active_connection(user: User) -> Optional[GoogleCalendarConnection]:
        """
        Get active Google Calendar connection for a user.

        Args:
            user: User to get connection for

        Returns:
            Active connection or None
        """
        return await sync_to_async(
            GoogleCalendarConnection.objects.filter(
                user=user,
                is_active=True
            ).first
        )()

    @staticmethod
    async def get_or_create_connection(user: User) -> Tuple[GoogleCalendarConnection, bool]:
        """
        Get existing connection or create new one for a user.
        Reuses existing connection even if inactive to avoid unique constraint violations.

        Args:
            user: User to get/create connection for

        Returns:
            Tuple of (connection, is_new)
        """
        try:
            connection = await sync_to_async(
                GoogleCalendarConnection.objects.get
            )(user=user)
            return connection, False
        except GoogleCalendarConnection.DoesNotExist:
            connection = GoogleCalendarConnection(
                user=user,
                created_by=user,
                updated_by=user,
            )
            return connection, True

    @staticmethod
    def create_credentials_for_revocation(connection: GoogleCalendarConnection) -> Credentials:
        """
        Create credentials object for token revocation.

        Args:
            connection: GoogleCalendarConnection instance

        Returns:
            Credentials object for revocation
        """
        return Credentials(
            token=connection.get_access_token(),
            refresh_token=connection.get_refresh_token(),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        )

    @staticmethod
    def update_connection_with_tokens(
        connection: GoogleCalendarConnection,
        credentials: Credentials,
        user: User
    ) -> None:
        """
        Update connection with new OAuth tokens and activate it.

        Args:
            connection: Connection to update
            credentials: OAuth credentials from Google
            user: User who owns the connection
        """
        connection.is_active = True
        connection.updated_by = user
        connection.calendar_id = DEFAULT_CALENDAR_ID
        connection.token_expiry = credentials.expiry if credentials.expiry else None
        connection.set_access_token(credentials.token)
        if credentials.refresh_token:
            connection.set_refresh_token(credentials.refresh_token)
        # If no new refresh token provided, keep existing one
