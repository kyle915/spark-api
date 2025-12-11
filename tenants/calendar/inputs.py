"""
GraphQL input types for Google Calendar integration.
"""
import strawberry

from utils.graphql.inputs import SparkGraphQLInput


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
