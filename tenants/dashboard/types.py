"""
GraphQL types for dashboard queries.

This module defines all response types for dashboard data queries.
"""
from __future__ import annotations

import strawberry
from typing import List

from events.types import Event, Location


@strawberry.type
class TimeSeriesDataPoint:
    """A single data point in a time series."""
    timestamp: str  # ISO datetime string
    count: int
    value: float | None = None  # Optional additional metric value


@strawberry.type
class EventStats:
    """Aggregated event statistics."""
    total_events: int
    events_by_status: List[EventStatusCount] | None = None
    events_by_location: List[LocationEventCount] | None = None
    events_today: int
    events_this_week: int
    events_this_month: int


@strawberry.type
class EventStatusCount:
    """Event count grouped by status."""
    status_id: strawberry.ID
    status_name: str
    count: int


@strawberry.type
class LocationEventCount:
    """Event count grouped by location."""
    location_id: strawberry.ID
    location_name: str
    location_code: str
    count: int


@strawberry.type
class EventTimeSeries:
    """Time series data for events."""
    data_points: List[TimeSeriesDataPoint]
    group_by: str  # HOUR, DAY, WEEK, MONTH
    total_count: int


@strawberry.type
class AmbassadorStats:
    """Ambassador working statistics."""
    total_ambassadors_working: int
    ambassadors_by_event: List[EventAmbassadorCount] | None = None
    ambassadors_by_location: List[LocationAmbassadorCount] | None = None
    unique_ambassadors_count: int  # Distinct ambassadors across all events


@strawberry.type
class EventAmbassadorCount:
    """Ambassador count per event."""
    event_id: strawberry.ID
    event_name: str
    ambassador_count: int


@strawberry.type
class LocationAmbassadorCount:
    """Ambassador count grouped by location."""
    location_id: strawberry.ID
    location_name: str
    location_code: str
    ambassador_count: int


@strawberry.type
class RequestStats:
    """Request statistics including approval/rejection rates."""
    total_requests: int
    approved_count: int
    rejected_count: int
    pending_count: int
    approval_rate: float  # Percentage (0-100)
    rejection_rate: float  # Percentage (0-100)
    requests_with_jobs_count: int
    requests_with_jobs_percentage: float  # Percentage (0-100)
    requests_by_status: List[RequestStatusCount] | None = None


@strawberry.type
class RequestStatusCount:
    """Request count grouped by status."""
    status_id: strawberry.ID
    status_name: str
    count: int


@strawberry.type
class RequestTimeSeries:
    """Time series data for requests."""
    data_points: List[TimeSeriesDataPoint]
    group_by: str  # HOUR, DAY, WEEK, MONTH
    total_count: int
    approval_trend: List[TimeSeriesDataPoint] | None = None
    rejection_trend: List[TimeSeriesDataPoint] | None = None
    jobs_assigned_trend: List[TimeSeriesDataPoint] | None = None


@strawberry.type
class EventDetail:
    """Detailed information about a specific event."""
    event: Event
    related_request_id: strawberry.ID | None = None
    ambassadors_count: int
    jobs_count: int
    ambassadors: List[EventAmbassadorInfo] | None = None
    location: Location | None = None
    statistics: EventDetailStatistics | None = None


@strawberry.type
class EventAmbassadorInfo:
    """Information about an ambassador working in an event."""
    ambassador_id: strawberry.ID
    ambassador_name: str
    is_approved: bool
    jobs_count: int  # Number of jobs this ambassador has in this event


@strawberry.type
class EventDetailStatistics:
    """Statistics for a specific event."""
    total_ambassadors: int
    approved_ambassadors: int
    total_jobs: int
    active_jobs: int  # Jobs that are not closed
    total_requests: int  # Related requests count

