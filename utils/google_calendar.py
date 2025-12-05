"""
Google Calendar API service for creating, updating, and deleting calendar events.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from django.conf import settings
from django.utils import timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from asgiref.sync import sync_to_async

from tenants.models import GoogleCalendarConnection, User
from events.models import GoogleCalendarEvent, Event
from events.models import GoogleCalendarEvent

logger = logging.getLogger(__name__)

# Google Calendar API scopes
SCOPES = ['https://www.googleapis.com/auth/calendar.events']


class GoogleCalendarService:
    """Service for interacting with Google Calendar API."""

    def __init__(self, user: User):
        """
        Initialize the service for a specific user.

        Args:
            user: The user whose Google Calendar to interact with
        """
        self.user = user
        self.connection: Optional[GoogleCalendarConnection] = None

    def _get_connection(self) -> Optional[GoogleCalendarConnection]:
        """Get the active Google Calendar connection for the user."""
        if not self.connection:
            try:
                self.connection = GoogleCalendarConnection.objects.get(
                    user=self.user,
                    is_active=True
                )
            except GoogleCalendarConnection.DoesNotExist:
                logger.warning(
                    f"No active Google Calendar connection for user {self.user.id}")
                return None
        return self.connection

    def _get_credentials(self) -> Optional[Credentials]:
        """
        Get valid Google OAuth credentials for the user.

        Returns:
            Credentials object or None if connection doesn't exist or tokens are invalid
        """
        connection = self._get_connection()
        if not connection:
            return None

        # Get decrypted tokens
        access_token = connection.get_access_token()
        refresh_token = connection.get_refresh_token()

        if not access_token:
            logger.error(f"No access token for user {self.user.id}")
            return None

        credentials = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=SCOPES
        )

        # Check if token needs refresh
        if connection.token_expiry and connection.token_expiry <= timezone.now():
            if refresh_token:
                try:
                    credentials.refresh(Request())
                    # Update stored tokens
                    connection.set_access_token(credentials.token)
                    if credentials.refresh_token:
                        connection.set_refresh_token(credentials.refresh_token)
                    if credentials.expiry:
                        connection.token_expiry = credentials.expiry
                    connection.save()
                    logger.info(f"Refreshed token for user {self.user.id}")
                except Exception as e:
                    logger.error(
                        f"Failed to refresh token for user {self.user.id}: {e}")
                    return None
            else:
                logger.error(
                    f"Token expired and no refresh token for user {self.user.id}")
                return None

        return credentials

    def _get_service(self):
        """Get Google Calendar API service instance."""
        credentials = self._get_credentials()
        if not credentials:
            return None

        http = AuthorizedHttp(credentials)
        return build('calendar', 'v3', http=http)

    def test_connection(self) -> bool:
        """
        Test if the Google Calendar connection is working by making a test API call.

        Returns:
            True if connection is valid and working, False otherwise
        """
        service = self._get_service()
        if not service:
            return False

        connection = self._get_connection()
        if not connection:
            return False

        try:
            # Make a lightweight API call to verify the connection works
            # Use events().list() which works with calendar.events scope
            # Limit to 1 result to keep it minimal
            events_result = service.events().list(
                calendarId=connection.calendar_id,
                maxResults=1,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            # If we get here, the API call succeeded
            logger.info(
                f"Google Calendar connection test successful for user {self.user.id}")
            return True
        except HttpError as e:
            logger.warning(
                f"Google Calendar connection test failed for user {self.user.id}: {e}")
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error testing Google Calendar connection for user {self.user.id}: {e}")
            return False

    def _format_event_data(self, event, event_type_name: Optional[str] = None,
                           status_name: Optional[str] = None) -> dict:
        """
        Format Event model data for Google Calendar API.

        IMPORTANT: Event must have a request. We get date, start_time, and end_time from the request.

        Args:
            event: Event model instance (must have event.request)
            event_type_name: Optional event type name
            status_name: Optional status name

        Returns:
            Dictionary formatted for Google Calendar API

        Raises:
            ValueError: If event doesn't have a request
        """
        # Validate that event has a request
        if not event.request:
            raise ValueError(
                f"Event {event.id} must have a request to sync to Google Calendar")

        # Build description with event details
        description_parts = []
        if event.notes:
            description_parts.append(event.notes)
        if event_type_name:
            description_parts.append(f"Type: {event_type_name}")
        if status_name:
            description_parts.append(f"Status: {status_name}")
        description = "\n".join(
            description_parts) if description_parts else None

        # Get date from request (required)
        event_date = event.request.date

        # Get start and end times from request
        start_datetime = None
        end_datetime = None

        # Use request.start_time (required)
        if event.request.start_time:
            start_datetime = datetime.combine(
                event_date, event.request.start_time)
            if timezone.is_aware(timezone.now()):
                start_datetime = timezone.make_aware(start_datetime)
        else:
            raise ValueError(
                f"Request {event.request.id} must have a start_time to sync event to Google Calendar")

        # Use request.end_time if available, otherwise default to 1 hour after start
        if event.request.end_time:
            end_datetime = datetime.combine(event_date, event.request.end_time)
            if timezone.is_aware(timezone.now()):
                end_datetime = timezone.make_aware(end_datetime)
        else:
            # Default to 1 hour duration if no end time
            end_datetime = start_datetime + timedelta(hours=1)

        event_data = {
            'summary': event.name,
            'description': description,
            'location': event.address or None,
            'start': {
                'dateTime': start_datetime.isoformat(),
                'timeZone': str(timezone.get_current_timezone()),
            },
            'end': {
                'dateTime': end_datetime.isoformat(),
                'timeZone': str(timezone.get_current_timezone()),
            },
        }

        return event_data

    def sync_event(self, event: Event, event_type_name: Optional[str] = None,
                   status_name: Optional[str] = None) -> Optional[str]:
        """
        Sync an event to Google Calendar - creates if doesn't exist, updates if it does.
        This prevents duplicates by checking for existing mappings.

        Args:
            event: Event model instance
            event_type_name: Optional event type name
            status_name: Optional status name

        Returns:
            Google Calendar event ID or None if sync failed
        """
        # Check if we already have a Google Calendar event ID for this user/event
        try:
            mapping = GoogleCalendarEvent.objects.get(
                event=event, user=self.user)
            google_event_id = mapping.google_event_id

            # Update existing event
            logger.info(
                f"Found existing Google Calendar event {google_event_id} for event {event.id} and user {self.user.id}, updating...")
            success = self.update_event(
                google_event_id, event, event_type_name, status_name)

            if success:
                return google_event_id
            else:
                # Update failed, might be deleted in Google Calendar
                # Delete the mapping and create a new event
                logger.warning(
                    f"Update failed for Google Calendar event {google_event_id}, deleting mapping and creating new event")
                mapping.delete()
                return self.create_event(event, event_type_name, status_name)
        except GoogleCalendarEvent.DoesNotExist:
            # No existing mapping, create new event
            logger.info(
                f"No existing Google Calendar event for event {event.id} and user {self.user.id}, creating new...")
            return self.create_event(event, event_type_name, status_name)

    def create_event(self, event: Event, event_type_name: Optional[str] = None,
                     status_name: Optional[str] = None) -> Optional[str]:
        """
        Create a calendar event in Google Calendar.

        Args:
            event: Event model instance (must have event.request)
            event_type_name: Optional event type name
            status_name: Optional status name

        Returns:
            Google Calendar event ID or None if creation failed
        """
        service = self._get_service()
        if not service:
            logger.error(
                f"Cannot create event: no service for user {self.user.id}")
            return None

        connection = self._get_connection()
        if not connection:
            return None

        try:
            event_data = self._format_event_data(
                event, event_type_name, status_name)
            created_event = service.events().insert(
                calendarId=connection.calendar_id,
                body=event_data
            ).execute()

            google_event_id = created_event.get('id')
            logger.info(
                f"Created Google Calendar event {google_event_id} for user {self.user.id}")

            # Store the mapping
            GoogleCalendarEvent.objects.create(
                event=event,
                user=self.user,
                google_event_id=google_event_id
            )
            logger.info(
                f"Stored Google Calendar event mapping for event {event.id} and user {self.user.id}")

            return google_event_id
        except HttpError as e:
            logger.error(
                f"Failed to create Google Calendar event for user {self.user.id}: {e}")
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error creating Google Calendar event for user {self.user.id}: {e}")
            return None

    def update_event(self, google_event_id: str, event,
                     event_type_name: Optional[str] = None,
                     status_name: Optional[str] = None) -> bool:
        """
        Update an existing calendar event in Google Calendar.

        Args:
            google_event_id: Google Calendar event ID
            event: Event model instance with updated data
            event_type_name: Optional event type name
            status_name: Optional status name

        Returns:
            True if update successful, False otherwise
        """
        service = self._get_service()
        if not service:
            logger.error(
                f"Cannot update event: no service for user {self.user.id}")
            return False

        connection = self._get_connection()
        if not connection:
            return False

        try:
            # Get existing event first
            existing_event = service.events().get(
                calendarId=connection.calendar_id,
                eventId=google_event_id
            ).execute()

            # Update with new data
            event_data = self._format_event_data(
                event, event_type_name, status_name)
            existing_event.update(event_data)

            updated_event = service.events().update(
                calendarId=connection.calendar_id,
                eventId=google_event_id,
                body=existing_event
            ).execute()

            logger.info(
                f"Updated Google Calendar event {google_event_id} for user {self.user.id}")
            return True
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(
                    f"Google Calendar event {google_event_id} not found for user {self.user.id}")
            else:
                logger.error(
                    f"Failed to update Google Calendar event {google_event_id} for user {self.user.id}: {e}")
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error updating Google Calendar event for user {self.user.id}: {e}")
            return False

    def delete_event(self, google_event_id: str) -> bool:
        """
        Delete a calendar event from Google Calendar.

        Args:
            google_event_id: Google Calendar event ID

        Returns:
            True if deletion successful, False otherwise
        """
        service = self._get_service()
        if not service:
            logger.error(
                f"Cannot delete event: no service for user {self.user.id}")
            return False

        connection = self._get_connection()
        if not connection:
            return False

        try:
            service.events().delete(
                calendarId=connection.calendar_id,
                eventId=google_event_id
            ).execute()

            logger.info(
                f"Deleted Google Calendar event {google_event_id} for user {self.user.id}")
            return True
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(
                    f"Google Calendar event {google_event_id} not found for user {self.user.id}")
                return True  # Consider it successful if already deleted
            else:
                logger.error(
                    f"Failed to delete Google Calendar event {google_event_id} for user {self.user.id}: {e}")
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error deleting Google Calendar event for user {self.user.id}: {e}")
            return False
