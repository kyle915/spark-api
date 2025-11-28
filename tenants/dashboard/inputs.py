"""
Input types for dashboard queries.

This module defines filter inputs for dashboard data queries.
"""
import strawberry
from enum import Enum

from utils.graphql.inputs import BaseTenantInput


@strawberry.enum
class TimeGroupBy(str, Enum):
    """Time grouping options for time series queries."""
    HOUR = "HOUR"
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


@strawberry.input
class DashboardFiltersInput(BaseTenantInput):
    """Filters for dashboard queries.
    
    All filters are optional. When not provided, queries will return
    data for the authenticated user's tenant within reasonable defaults.
    """
    # Date range filters
    start_date: str | None = None  # ISO date string (YYYY-MM-DD)
    end_date: str | None = None    # ISO date string (YYYY-MM-DD)
    
    # Location/zone filters
    location_id: strawberry.ID | None = None
    location_code: str | None = None  # Filter by location code (zone)
    
    # Event filters
    event_type_id: strawberry.ID | None = None
    event_status_id: strawberry.ID | None = None
    
    # Request filters
    request_status_id: strawberry.ID | None = None
    request_type_id: strawberry.ID | None = None
    
    # Additional useful filters
    client_id: strawberry.ID | None = None
    distributor_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None

