import logging
from django.conf import settings
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime
import json

logger = logging.getLogger(__name__)

class GoogleCalendarService:
    SCOPES = ['https://www.googleapis.com/auth/calendar.events']

    def __init__(self):
        self.credentials = self._get_credentials()
        self.calendar_id = getattr(settings, 'GOOGLE_CALENDAR_ID', 'primary')
        self.service = self._build_service()

    def _get_credentials(self):
        """Retrieve credentials from GOOGLE_CALENDAR_CREDENTIALS or fallback to GS_CREDENTIALS."""
        creds_info = getattr(settings, 'GOOGLE_CALENDAR_CREDENTIALS', None) or getattr(settings, 'GS_CREDENTIALS', None)
        if creds_info:
            try:
                return service_account.Credentials.from_service_account_info(
                    creds_info, scopes=self.SCOPES
                )
            except Exception as e:
                logger.error(f"Failed to load service account info for Calendar API: {e}")
                return None
        return None

    def _build_service(self):
        """Build the Google Calendar API service."""
        if not self.credentials:
            return None
        try:
            return build('calendar', 'v3', credentials=self.credentials)
        except Exception as e:
            logger.error(f"Failed to build Google Calendar service: {e}")
            return None

    def create_event(
        self,
        summary: str,
        start_time: datetime,
        end_time: datetime,
        description: str = "",
        location: str = "",
        timezone: str = "UTC",
        attendees: list[str] | None = None,
    ) -> dict | None:
        """
        Creates an event in the configured Google Calendar.
        
        Args:
            summary: Title of the event
            start_time: Start datetime
            end_time: End datetime
            description: Extended description of the event
            location: Address or location of the event
            timezone: Timezone string (e.g., 'America/New_York')
            attendees: List of email addresses to invite
            
        Returns:
            The created event dictionary from the API, or None if failed.
        """
        if not self.service:
            logger.warning("Google Calendar service not initialized. Cannot create event.")
            return None

        event_body = {
            'summary': summary,
            'description': description,
            'location': location,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': timezone,
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': timezone,
            },
        }

        if attendees:
            event_body['attendees'] = [{'email': email} for email in attendees if email]

        try:
            event = self.service.events().insert(
                calendarId=self.calendar_id, 
                body=event_body,
                sendUpdates='all',
            ).execute()
            logger.info(f"Successfully created Google Calendar event: {event.get('htmlLink')}")
            return event
        except HttpError as error:
            logger.error(f"An error occurred creating Google Calendar event: {error}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating Google Calendar event: {e}")
            return None
