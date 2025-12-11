"""
GraphQL types for Google Calendar integration.
"""
import strawberry


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


@strawberry.type
class GoogleCalendarConnectionStatus:
    """Status of Google Calendar connection for a user."""
    is_connected: bool
    is_active: bool
    calendar_id: str | None = None
    connected_at: str | None = None  # ISO format datetime string
