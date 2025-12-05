"""
Constants for Google Calendar integration.
"""

# Google Calendar API scopes
GOOGLE_CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar.events']

# OAuth state cache configuration
STATE_CACHE_TIMEOUT = 600  # 10 minutes
STATE_CACHE_PREFIX = "google_calendar_oauth_state_"

# Default calendar ID
DEFAULT_CALENDAR_ID = "primary"
