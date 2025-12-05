"""
GraphQL queries for Google Calendar integration.
"""
import logging
import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.permissions import StrictIsAuthenticated
from utils.google_calendar import GoogleCalendarService
from .models import GoogleCalendarConnection, User

logger = logging.getLogger(__name__)


@strawberry.type
class GoogleCalendarConnectionStatus:
    """Status of Google Calendar connection for a user."""
    is_connected: bool
    is_active: bool
    calendar_id: str | None = None
    connected_at: str | None = None  # ISO format datetime string


@strawberry.type
class GoogleCalendarQueries:
    """Queries for Google Calendar integration."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def google_calendar_connection_status(
        self,
        info: strawberry.Info,
    ) -> GoogleCalendarConnectionStatus:
        """
        Check if the current user has an active Google Calendar connection.
        This performs a real API call to Google Calendar to verify the connection works.

        Returns:
            GoogleCalendarConnectionStatus with connection information
        """
        try:
            user: User = info.context.request.user

            # Check for any connection in the database
            connection = await sync_to_async(
                GoogleCalendarConnection.objects.filter(
                    user=user
                ).first
            )()

            if not connection:
                return GoogleCalendarConnectionStatus(
                    is_connected=False,
                    is_active=False,
                    calendar_id=None,
                    connected_at=None,
                )

            # Test the connection by making a real API call to Google Calendar
            service = GoogleCalendarService(user)
            is_working = await sync_to_async(service.test_connection)()

            return GoogleCalendarConnectionStatus(
                is_connected=True,
                is_active=is_working and connection.is_active,
                calendar_id=connection.calendar_id,
                connected_at=connection.created_at.isoformat() if connection.created_at else None,
            )
        except Exception as e:
            logger.error(
                f"Error checking Google Calendar connection status: {e}")
            return GoogleCalendarConnectionStatus(
                is_connected=False,
                is_active=False,
                calendar_id=None,
                connected_at=None,
            )
