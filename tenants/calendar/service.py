"""
Google Calendar API service for creating, updating, and deleting calendar events.
"""

import logging
from datetime import datetime, timedelta, date as date_type, timezone as dt_timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo
from django.conf import settings
from django.utils import timezone
import httplib2
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from google_auth_httplib2 import AuthorizedHttp, Request as Httplib2Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tenants.models import GoogleCalendarConnection, User
from events.models import Event, GoogleCalendarEvent, TimeZone
from .constants import GOOGLE_CALENDAR_SCOPES

# Hard ceiling on every Google Calendar HTTP call (OAuth token refresh + API
# requests). Without it, google-auth's requests transport defaults to 120s and
# httplib2 to NO timeout — a stalled call hangs the caller. That inline hang in
# the AmbassadorEvent post_save froze the invite mutation for ~2 min. Calendar
# sync is best-effort, so keep this short.
_CALENDAR_HTTP_TIMEOUT_SECONDS = 10

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
                    user=self.user, is_active=True
                )
            except GoogleCalendarConnection.DoesNotExist:
                logger.warning(
                    f"No active Google Calendar connection for user {self.user.id}"
                )
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
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=GOOGLE_CALENDAR_SCOPES,
        )

        # Check if token needs refresh
        if connection.token_expiry and connection.token_expiry <= timezone.now():
            if refresh_token:
                try:
                    credentials.refresh(
                        Httplib2Request(
                            httplib2.Http(timeout=_CALENDAR_HTTP_TIMEOUT_SECONDS)
                        )
                    )
                    # Update stored tokens
                    connection.set_access_token(credentials.token)
                    if credentials.refresh_token:
                        connection.set_refresh_token(credentials.refresh_token)
                    if credentials.expiry:
                        connection.token_expiry = credentials.expiry
                    connection.save()
                    logger.info(f"Refreshed token for user {self.user.id}")
                except RefreshError as e:
                    logger.error(
                        f"Failed to refresh token for user {self.user.id}: {e}"
                    )
                    if self._is_invalid_grant_error(e):
                        self._deactivate_connection(connection)
                    return None
                except Exception as e:
                    logger.error(
                        f"Failed to refresh token for user {self.user.id}: {e}"
                    )
                    return None
            else:
                logger.error(
                    f"Token expired and no refresh token for user {self.user.id}"
                )
                return None

        return credentials

    @staticmethod
    def _is_invalid_grant_error(error: Exception) -> bool:
        """Return True when Google refresh fails because token grant is invalid."""
        if "invalid_grant" in str(error).lower():
            return True

        for arg in getattr(error, "args", []):
            if isinstance(arg, dict) and str(arg.get("error", "")).lower() == "invalid_grant":
                return True
        return False

    def _deactivate_connection(self, connection: GoogleCalendarConnection) -> None:
        """Deactivate broken OAuth connection so the user can reconnect cleanly."""
        connection.is_active = False
        connection.updated_by = self.user
        connection.save(update_fields=["is_active", "updated_by", "updated_at"])
        logger.warning(
            "Deactivated Google Calendar connection for user %s due to invalid_grant; user must reconnect.",
            self.user.id,
        )

    def _get_service(self):
        """Get Google Calendar API service instance."""
        credentials = self._get_credentials()
        if not credentials:
            return None

        http = AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=_CALENDAR_HTTP_TIMEOUT_SECONDS)
        )
        return build("calendar", "v3", http=http)

    def _ensure_service_and_connection(
        self,
    ) -> Tuple[Optional[object], Optional[GoogleCalendarConnection]]:
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
                f"Cannot perform operation: no service for user {self.user.id}"
            )
            return None, None

        connection = self._get_connection()
        if not connection:
            logger.error(
                f"Cannot perform operation: no active connection for user {self.user.id}"
            )
            return None, None

        return service, connection

    @staticmethod
    def _format_google_datetime(dt: datetime) -> str:
        """
        Format a datetime for Google Calendar as RFC3339 preserving its offset.
        """
        return dt.replace(microsecond=0).isoformat()

    @staticmethod
    def _resolve_offset_timezone(_timezone: TimeZone | None) -> dt_timezone:
        """
        Resolve a fixed-offset timezone from event.timezone.offset.
        """
        if _timezone and _timezone.offset is not None:
            offset_minutes = int(_timezone.offset)
            return dt_timezone(timedelta(minutes=offset_minutes))
        return dt_timezone.utc

    @staticmethod
    def _build_datetime_with_offset(
        date_value, time_value, _timezone: TimeZone | None = None
    ) -> Optional[datetime]:
        """
        Build a datetime that preserves the event's wall time and applies event.timezone.offset.
        """
        if not time_value:
            return None

        offset_tzinfo = GoogleCalendarService._resolve_offset_timezone(_timezone)

        if isinstance(date_value, datetime):
            date_obj = date_value.date()
        elif isinstance(date_value, date_type):
            date_obj = date_value
        else:
            date_obj = None

        if isinstance(time_value, datetime):
            time_part = time_value.time().replace(tzinfo=None)
            if not date_obj:
                date_obj = time_value.date()
        else:
            time_part = time_value

        if not date_obj:
            raise ValueError(
                f"date parameter must be date or datetime, got {type(date_value)}"
            )

        return datetime.combine(date_obj, time_part).replace(tzinfo=offset_tzinfo)

    @staticmethod
    def _format_event_timezone_label(_timezone: TimeZone | None, fallback_tz_name: Optional[str] = None) -> str:
        """
        Format timezone label like `Pacific-PDT (UTC-07:00)`.
        """
        if not _timezone:
            return fallback_tz_name or "-"

        name = (_timezone.name or "").strip()
        code = (_timezone.code or "").strip()
        offset_minutes = int(_timezone.offset) if _timezone.offset is not None else 0
        sign = "+" if offset_minutes >= 0 else "-"
        abs_minutes = abs(offset_minutes)
        hours, minutes = divmod(abs_minutes, 60)
        utc_label = f"UTC{sign}{hours:02d}:{minutes:02d}"

        label = "-".join(part for part in [name, code] if part)
        if not label:
            label = fallback_tz_name or "-"
        return f"{label} ({utc_label})"

    @staticmethod
    def _resolve_timezone(
        _timezone: TimeZone | None,
    ) -> tuple[dt_timezone, Optional[str]]:
        """
        Resolve a timezone to tzinfo + optional IANA name.
        Prefer a stable IANA mapping (DST-aware), otherwise fall back to fixed offset.
        """
        if _timezone:
            tz_code = (_timezone.code or "").strip().upper()
            tz_name_raw = (_timezone.name or "").strip()
            # Map app-specific names/codes to IANA zones to preserve DST.
            mapped = {
                "EASTERN": "America/New_York",
                "CENTRAL": "America/Chicago",
                "MOUNTAIN": "America/Denver",
                "PACIFIC": "America/Los_Angeles",
                "ALASKA": "America/Anchorage",
                "HAWAII-ALEUTIAN": "Pacific/Honolulu",
                "HAWAII–ALEUTIAN": "Pacific/Honolulu",
                "EST": "America/New_York",
                "EDT": "America/New_York",
                "CST": "America/Chicago",
                "CDT": "America/Chicago",
                "MST": "America/Denver",
                "MDT": "America/Denver",
                "PST": "America/Los_Angeles",
                "PDT": "America/Los_Angeles",
                "AKST": "America/Anchorage",
                "AKDT": "America/Anchorage",
                "HST": "Pacific/Honolulu",
                "HDT": "Pacific/Honolulu",
            }
            candidate = mapped.get(tz_code) or mapped.get(tz_name_raw.upper())
            if candidate:
                try:
                    return ZoneInfo(candidate), candidate
                except Exception:
                    pass

            if tz_name_raw:
                candidates = [tz_name_raw]
                if tz_name_raw.startswith("Americas/"):
                    candidates.append("America/" + tz_name_raw[len("Americas/"):])
                for candidate in candidates:
                    try:
                        return ZoneInfo(candidate), candidate
                    except Exception:
                        continue

            if _timezone.offset is not None:
                offset_minutes = int(_timezone.offset)
                tzinfo = dt_timezone(timedelta(minutes=offset_minutes))
                tz_name = None
                if offset_minutes % 60 == 0:
                    # Etc/GMT has inverted sign (Etc/GMT+6 == UTC-6)
                    offset_hours = int(offset_minutes / 60)
                    sign = "+" if offset_hours < 0 else "-"
                    tz_name = f"Etc/GMT{sign}{abs(offset_hours)}"
                return tzinfo, tz_name

        return dt_timezone.utc, "UTC"

    @staticmethod
    def _build_datetime(
        date, time_value, _timezone: TimeZone | None = None
    ) -> Optional[datetime]:
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

        tzinfo, _ = GoogleCalendarService._resolve_timezone(_timezone)

        # Build datetime from date and time_value.
        # If values are timezone-aware datetimes, first convert to event timezone
        # so we preserve the intended local wall time.
        if isinstance(date, datetime):
            date_local = date.astimezone(tzinfo) if timezone.is_aware(date) else date
            date_obj = date_local.date()
        elif isinstance(date, date_type):
            date_obj = date
        else:
            date_obj = None

        if isinstance(time_value, datetime):
            time_local = (
                time_value.astimezone(tzinfo)
                if timezone.is_aware(time_value)
                else time_value
            )
            time_part = time_local.time().replace(tzinfo=None)
            if not date_obj:
                date_obj = time_local.date()
            dt = datetime.combine(date_obj, time_part)
        else:
            if not date_obj:
                raise ValueError(
                    f"date parameter must be date or datetime, got {type(date)}"
                )
            dt = datetime.combine(date_obj, time_value)

        if timezone.is_aware(dt):
            dt = dt.astimezone(tzinfo)
        else:
            dt = dt.replace(tzinfo=tzinfo)

        return dt

    def _handle_http_error(
        self,
        error: HttpError,
        operation: str,
        google_event_id: Optional[str] = None,
        treat_404_as_success: bool = False,
    ) -> bool:
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
                f"Google Calendar event{event_id_context} not found for user {self.user.id} during {operation}"
            )
            return treat_404_as_success
        else:
            logger.error(
                f"Failed to {operation} Google Calendar event{event_id_context} for user {self.user.id}: {error}"
            )
            return False

    @staticmethod
    def _is_not_found_error(error: HttpError) -> bool:
        return getattr(getattr(error, "resp", None), "status", None) == 404

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
                orderBy="startTime",
            ).execute()

            logger.info(
                f"Google Calendar connection test successful for user {self.user.id}"
            )
            return True
        except HttpError as e:
            logger.warning(
                f"Google Calendar connection test failed for user {self.user.id}: {e}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error testing Google Calendar connection for user {self.user.id}: {e}"
            )
            return False

    def _format_event_data(
        self,
        event: Event,
        event_type_name: Optional[str] = None,
        status_name: Optional[str] = None,
    ) -> dict:
        """
        Format Event model data for Google Calendar API.

        Args:
            event: Event model instance
            event_type_name: Optional event type name
            status_name: Optional status name

        Returns:
            Dictionary formatted for Google Calendar API

        Raises:
            ValueError: If event is missing required time data
        """
        # Get timezone: event.timezone or event.request.timezone, default None (UTC)
        event_timezone = event.timezone
        if not event_timezone and event.request:
            event_timezone = event.request.timezone

        # Prefer Event fields; fallback to Request fields for legacy records.
        event_date = event.date or (event.request.date if event.request else None)
        event_start_time = event.start_time or (
            event.request.start_time if event.request else None
        )
        event_end_time = event.end_time or (
            event.request.end_time if event.request else None
        )

        if not event_start_time:
            raise ValueError(
                f"Event {event.id} must have start_time (or request.start_time) to sync to Google Calendar"
            )

        start_datetime = self._build_datetime(event_date, event_start_time, event_timezone)
        end_datetime = self._build_datetime(event_date, event_end_time, event_timezone)
        if end_datetime and end_datetime <= start_datetime:
            logger.warning(
                "Event %s has invalid time range (start=%s, end=%s). Adjusting end time to +1 hour.",
                event.id,
                start_datetime,
                end_datetime,
            )
            end_datetime = start_datetime + timedelta(hours=1)

        tzinfo, tz_name = self._resolve_timezone(event_timezone)

        offset_tzinfo = self._resolve_offset_timezone(event_timezone)
        start_local_with_offset = start_datetime.astimezone(offset_tzinfo)

        effective_end_datetime = end_datetime
        if not effective_end_datetime:
            effective_end_datetime = start_datetime + timedelta(hours=1)
        elif end_datetime <= start_datetime:
            effective_end_datetime = start_datetime + timedelta(hours=1)

        end_local_with_offset = effective_end_datetime.astimezone(offset_tzinfo)
        start_iso = self._format_google_datetime(start_local_with_offset)
        end_iso = self._format_google_datetime(end_local_with_offset)

        request_product_names: list[str] = []
        if event.request_id:
            request_product_names = list(
                event.request.request_product.select_related("product")
                .values_list("product__name", flat=True)
            )

        description_parts = []
        description_parts.extend(
            [
                f"Date: {start_local_with_offset.strftime('%m/%d/%Y')}",
                f"Retail: {getattr(getattr(event, 'retailer', None), 'name', None) or '-'}",
                f"Address: {event.address or '-'}",
                f"Start Time: {start_local_with_offset.strftime('%I:%M %p')}",
                f"End Time: {end_local_with_offset.strftime('%I:%M %p')}",
                f"Timezone: {self._format_event_timezone_label(event_timezone, tz_name)}",
                f"Products: {', '.join(filter(None, request_product_names)) or '-'}",
            ]
        )
        if event_type_name:
            description_parts.append(f"Type: {event_type_name}")
        if event.notes:
            description_parts.append(f"Note: {event.notes}")
        description = "\n".join(description_parts)

        summary_suffix = (
            f" | {start_local_with_offset.strftime('%I:%M %p')}"
            f" | {end_local_with_offset.strftime('%I:%M %p')}"
            f" | {getattr(event_timezone, 'code', None) or '-'}"
        )

        event_data = {
            "summary": f"{event.name}{summary_suffix}",
            "description": description,
            "location": event.address or None,
            "start": {
                "dateTime": start_iso,
            },
            "end": {
                "dateTime": end_iso,
            },
        }
        if tz_name:
            event_data["start"]["timeZone"] = tz_name
            event_data["end"]["timeZone"] = tz_name

        return event_data

    def sync_event(
        self,
        event: Event,
        event_type_name: Optional[str] = None,
        status_name: Optional[str] = None,
    ) -> Optional[str]:
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
                f"Syncing event {event.id} to Google Calendar for user {self.user.id} using calendar '{connection.calendar_id}'"
            )
        else:
            logger.warning(
                f"No active Google Calendar connection for user {self.user.id}"
            )

        # Check if we already have a Google Calendar event ID for this user/event
        try:
            mapping = GoogleCalendarEvent.objects.get(event=event, user=self.user)
            google_event_id = mapping.google_event_id

            # Update existing event
            logger.info(
                f"Found existing Google Calendar event {google_event_id} for event {event.id} and user {self.user.id}, updating..."
            )
            update_result = self.update_event(
                google_event_id, event, event_type_name, status_name
            )

            if update_result is True:
                return google_event_id
            elif update_result is None:
                # Update failed because event no longer exists in Google Calendar.
                # Delete the mapping and create a new event
                logger.warning(
                    f"Google Calendar event {google_event_id} was not found for event {event.id} and user {self.user.id}, deleting mapping and creating new event"
                )
                mapping.delete()
                return self.create_event(event, event_type_name, status_name)
            else:
                logger.warning(
                    "Update failed for Google Calendar event %s for event %s and user %s; preserving mapping and not creating a duplicate event",
                    google_event_id,
                    event.id,
                    self.user.id,
                )
                return None
        except GoogleCalendarEvent.DoesNotExist:
            # No existing mapping, create new event
            logger.info(
                f"No existing Google Calendar event for event {event.id} and user {self.user.id}, creating new..."
            )
            return self.create_event(event, event_type_name, status_name)

    def create_event(
        self,
        event: Event,
        event_type_name: Optional[str] = None,
        status_name: Optional[str] = None,
    ) -> Optional[str]:
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
                f"Creating Google Calendar event for user {self.user.id} in calendar '{calendar_id}'"
            )

            event_data = self._format_event_data(event, event_type_name, status_name)
            logger.info(
                "Google Calendar create payload for user %s event %s: start=%s end=%s timezone_start=%s timezone_end=%s",
                self.user.id,
                event.id,
                event_data.get("start", {}).get("dateTime"),
                event_data.get("end", {}).get("dateTime"),
                event_data.get("start", {}).get("timeZone"),
                event_data.get("end", {}).get("timeZone"),
            )

            created_event = (
                service.events()
                .insert(calendarId=calendar_id, body=event_data)
                .execute()
            )

            google_event_id = created_event.get("id")
            logger.info(
                f"Created Google Calendar event {google_event_id} for user {self.user.id} in calendar '{calendar_id}'. "
                f"Event summary: {created_event.get('summary')}, "
                f"Start: {created_event.get('start')}, "
                f"End: {created_event.get('end')}, "
                f"Status: {created_event.get('status')}, "
                f"Visibility: {created_event.get('visibility', 'default')}"
            )

            # Store the mapping
            GoogleCalendarEvent.objects.create(
                event=event, user=self.user, google_event_id=google_event_id
            )
            logger.info(
                f"Stored Google Calendar event mapping for event {event.id} and user {self.user.id}"
            )

            return google_event_id
        except HttpError as e:
            logger.error(
                f"Failed to create Google Calendar event for user {self.user.id} in calendar '{connection.calendar_id}': {e}"
            )
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error creating Google Calendar event for user {self.user.id}: {e}"
            )
            return None

    def update_event(
        self,
        google_event_id: str,
        event: Event,
        event_type_name: Optional[str] = None,
        status_name: Optional[str] = None,
    ) -> bool | None:
        """
        Update an existing calendar event in Google Calendar.

        Args:
            google_event_id: Google Calendar event ID
            event: Event model instance with updated data
            event_type_name: Optional event type name
            status_name: Optional status name

        Returns:
            True if update successful, None if Google returned 404, False otherwise
        """
        service, connection = self._ensure_service_and_connection()
        if not service or not connection:
            return False

        try:
            calendar_id = connection.calendar_id
            logger.info(
                f"Updating Google Calendar event {google_event_id} for user {self.user.id} in calendar '{calendar_id}'"
            )

            # Update with new data only. Avoid sending full existing payload because
            # it can carry stale fields from previous versions.
            event_data = self._format_event_data(event, event_type_name, status_name)
            logger.info(
                "Google Calendar update payload for user %s event %s google_event_id=%s: start=%s end=%s timezone_start=%s timezone_end=%s",
                self.user.id,
                event.id,
                google_event_id,
                event_data.get("start", {}).get("dateTime"),
                event_data.get("end", {}).get("dateTime"),
                event_data.get("start", {}).get("timeZone"),
                event_data.get("end", {}).get("timeZone"),
            )
            updated_event = (
                service.events()
                .patch(
                    calendarId=calendar_id,
                    eventId=google_event_id,
                    body=event_data,
                )
                .execute()
            )

            logger.info(
                f"Updated Google Calendar event {google_event_id} for user {self.user.id} in calendar '{calendar_id}'. "
                f"Event summary: {updated_event.get('summary')}, "
                f"Start: {updated_event.get('start')}, "
                f"End: {updated_event.get('end')}, "
                f"Status: {updated_event.get('status')}, "
                f"Visibility: {updated_event.get('visibility', 'default')}"
            )

            # Verify the event exists after update
            try:
                service.events().get(
                    calendarId=calendar_id, eventId=google_event_id
                ).execute()
                logger.info(
                    f"Verified event {google_event_id} exists in calendar '{calendar_id}' after update"
                )
                return True
            except HttpError as verify_error:
                if self._is_not_found_error(verify_error):
                    logger.warning(
                        "Google Calendar event %s was not found during post-update verification for user %s",
                        google_event_id,
                        self.user.id,
                    )
                    return None
                logger.error(
                    f"Failed to verify event {google_event_id} exists after update: {verify_error}"
                )
                return False

        except HttpError as e:
            if self._is_not_found_error(e):
                logger.warning(
                    "Google Calendar event %s was not found during update for user %s",
                    google_event_id,
                    self.user.id,
                )
                return None
            return self._handle_http_error(
                e, "update", google_event_id, treat_404_as_success=False
            )
        except Exception as e:
            logger.error(
                f"Unexpected error updating Google Calendar event for user {self.user.id}: {e}"
            )
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
                calendarId=connection.calendar_id, eventId=google_event_id
            ).execute()

            logger.info(
                f"Deleted Google Calendar event {google_event_id} for user {self.user.id}"
            )
            return True
        except HttpError as e:
            return self._handle_http_error(
                e, "delete", google_event_id, treat_404_as_success=True
            )
        except Exception as e:
            logger.error(
                f"Unexpected error deleting Google Calendar event for user {self.user.id}: {e}"
            )
            return False
