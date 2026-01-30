"""
Google Calendar API service for creating, updating, and deleting calendar events.
"""
import logging
from datetime import datetime, timedelta, date as date_type
from typing import Optional, Tuple
from zoneinfo import ZoneInfo
from django.conf import settings
from django.utils import timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tenants.models import GoogleCalendarConnection, User
from events.models import Event, GoogleCalendarEvent, TimeZone
from .constants import GOOGLE_CALENDAR_SCOPES

logger = logging.getLogger(__name__)


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
            scopes=GOOGLE_CALENDAR_SCOPES
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

    def _ensure_service_and_connection(self) -> Tuple[Optional[object], Optional[GoogleCalendarConnection]]:
        """
        Ensure both service and connection are available.

        Returns:
            tuple: (service, connection) if both are available, (None, None) otherwise

        Note:
            This method logs errors if service or connection is unavailable
        """
        service = self._get_service()
        if not service:
            logger.error(
                f"Cannot perform operation: no service for user {self.user.id}")
            return None, None

        connection = self._get_connection()
        if not connection:
            logger.error(
                f"Cannot perform operation: no active connection for user {self.user.id}")
            return None, None

        return service, connection

    @staticmethod
    def _build_datetime(date, time_value, _timezone: TimeZone | None = None) -> Optional[datetime]:
        """
        Build a timezone-aware datetime from date and time values.

        Args:
            date: Date object or datetime object (used only if time_value is a time object)
            time_value: Time object, datetime object, or None
            _timezone: TimeZone model instance (uses timezone.name for IANA name) or None (defaults to UTC)

        Returns:
            Timezone-aware datetime or None if time_value is None
        """
        if not time_value:
            return None

        # Determine target timezone (IANA name from TimeZone.name, default UTC)

        # logger.info(f"Datetime: {dt}")

        # Build datetime from date and time_value
        if isinstance(time_value, datetime):
            dt = time_value
        else:
            # Combine date and time
            if isinstance(date, datetime):
                date_obj = date.date()
            elif isinstance(date, date_type):
                date_obj = date
            else:
                raise ValueError(
                    f"date parameter must be date or datetime, got {type(date)}")
            dt = datetime.combine(date_obj, time_value)

        logger.info(f"Datetime: {dt}")
        return dt

    def _handle_http_error(self, error: HttpError, operation: str, google_event_id: Optional[str] = None,
                           treat_404_as_success: bool = False) -> bool:
        """
        Handle HttpError exceptions consistently.

        Args:
            error: The HttpError exception
            operation: Description of the operation being performed
            google_event_id: Optional Google Calendar event ID for context
            treat_404_as_success: Whether to treat 404 errors as success (e.g., for delete operations)

        Returns:
            True if operation should be considered successful, False otherwise
        """
        event_id_context = f" {google_event_id}" if google_event_id else ""

        if error.resp.status == 404:
            logger.warning(
                f"Google Calendar event{event_id_context} not found for user {self.user.id} during {operation}")
            return treat_404_as_success
        else:
            logger.error(
                f"Failed to {operation} Google Calendar event{event_id_context} for user {self.user.id}: {error}")
            return False

    def test_connection(self) -> bool:
        """
        Test if the Google Calendar connection is working by making a test API call.

        Returns:
            True if connection is valid and working, False otherwise
        """
        service, connection = self._ensure_service_and_connection()
        if not service or not connection:
            return False

        try:
            # Make a lightweight API call to verify the connection works
            # Use events().list() which works with calendar.events scope
            # Limit to 1 result to keep it minimal
            service.events().list(
                calendarId=connection.calendar_id,
                maxResults=1,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

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

    def _format_event_data(self, event: Event, event_type_name: Optional[str] = None,
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

        # Get timezone: event.timezone or event.request.timezone, default None (UTC)
        event_timezone = event.timezone
        if not event_timezone and event.request:
            event_timezone = event.request.timezone

        # Get date from request (required)
        event_date = event.date

        # Get start and end times from request
        if not event.start_time:
            raise ValueError(
                f"Request {event.request.id} must have a start_time to sync event to Google Calendar")

        start_datetime = self._build_datetime(
            event_date, event.start_time, event_timezone)

        end_datetime = self._build_datetime(
            event_date, event.end_time, event_timezone)
        if not end_datetime:
            end_datetime = start_datetime + timedelta(hours=1)

        # Determine timezone name for Google Calendar API (IANA name or UTC)
        tz_name = event_timezone.name if event_timezone else "UTC"

        event_data = {
            'summary': event.name,
            'description': description,
            'location': event.address or None,
            'start': {
                'dateTime': start_datetime.strftime('%Y-%m-%dT%H:%M:%S'),
                'timeZone': tz_name,
            },
            'end': {
                'dateTime': end_datetime.strftime('%Y-%m-%dT%H:%M:%S'),
                'timeZone': tz_name,
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
        connection = self._get_connection()
        if connection:
            logger.info(
                f"Syncing event {event.id} to Google Calendar for user {self.user.id} using calendar '{connection.calendar_id}'")
        else:
            logger.warning(
                f"No active Google Calendar connection for user {self.user.id}")

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
        service, connection = self._ensure_service_and_connection()
        if not service or not connection:
            return None

        try:
            calendar_id = connection.calendar_id
            logger.info(
                f"Creating Google Calendar event for user {self.user.id} in calendar '{calendar_id}'")

            event_data = self._format_event_data(
                event, event_type_name, status_name)
            logger.debug(
                f"Event data to create: summary={event_data.get('summary')}, "
                f"start={event_data.get('start')}, end={event_data.get('end')}, "
                f"location={event_data.get('location')}")

            created_event = service.events().insert(
                calendarId=calendar_id,
                body=event_data
            ).execute()

            google_event_id = created_event.get('id')
            logger.info(
                f"Created Google Calendar event {google_event_id} for user {self.user.id} in calendar '{calendar_id}'. "
                f"Event summary: {created_event.get('summary')}, "
                f"Start: {created_event.get('start')}, "
                f"End: {created_event.get('end')}, "
                f"Status: {created_event.get('status')}, "
                f"Visibility: {created_event.get('visibility', 'default')}")

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
                f"Failed to create Google Calendar event for user {self.user.id} in calendar '{connection.calendar_id}': {e}")
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error creating Google Calendar event for user {self.user.id}: {e}")
            return None

    def update_event(self, google_event_id: str, event: Event,
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
        service, connection = self._ensure_service_and_connection()
        if not service or not connection:
            return False

        try:
            calendar_id = connection.calendar_id
            logger.info(
                f"Updating Google Calendar event {google_event_id} for user {self.user.id} in calendar '{calendar_id}'")

            # Get existing event first
            existing_event = service.events().get(
                calendarId=calendar_id,
                eventId=google_event_id
            ).execute()

            # Update with new data
            event_data = self._format_event_data(
                event, event_type_name, status_name)
            logger.info(f"Event data to update: {event_data}")
            existing_event.update(event_data)
            updated_event = service.events().update(
                calendarId=calendar_id,
                eventId=google_event_id,
                body=existing_event
            ).execute()

            logger.info(
                f"Updated Google Calendar event {google_event_id} for user {self.user.id} in calendar '{calendar_id}'. "
                f"Event summary: {updated_event.get('summary')}, "
                f"Start: {updated_event.get('start')}, "
                f"End: {updated_event.get('end')}, "
                f"Status: {updated_event.get('status')}, "
                f"Visibility: {updated_event.get('visibility', 'default')}")

            # Verify the event exists after update
            try:
                service.events().get(
                    calendarId=calendar_id,
                    eventId=google_event_id
                ).execute()
                logger.info(
                    f"Verified event {google_event_id} exists in calendar '{calendar_id}' after update")
                return True
            except HttpError as verify_error:
                logger.error(
                    f"Failed to verify event {google_event_id} exists after update: {verify_error}")
                return False

        except HttpError as e:
            return self._handle_http_error(e, "update", google_event_id, treat_404_as_success=False)
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
        service, connection = self._ensure_service_and_connection()
        if not service or not connection:
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
            return self._handle_http_error(e, "delete", google_event_id, treat_404_as_success=True)
        except Exception as e:
            logger.error(
                f"Unexpected error deleting Google Calendar event for user {self.user.id}: {e}")
            return False
