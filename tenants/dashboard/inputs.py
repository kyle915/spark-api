"""
Input types for dashboard queries.

This module defines filter inputs for Event Dashboard and Recap Dashboard data queries.
"""
import strawberry

from utils.graphql.inputs import BaseTenantInput


@strawberry.input
class EventDashboardFiltersInput(BaseTenantInput):
    """Filters for Event Dashboard queries.

    All filters are optional. When not provided, queries will return
    data for the authenticated user's tenant within the current quarter.
    """
    # Date range filters
    start_date: str | None = None  # ISO date string (YYYY-MM-DD)
    end_date: str | None = None    # ISO date string (YYYY-MM-DD)

    # Quarter filter (takes precedence over start_date/end_date if provided)
    quarter: str | None = None  # Quarter string like "Q1 2025"

    # RMM assigned user filter
    rmm_asigned_id: strawberry.ID | None = None

    # Distributor filter
    distributor_id: strawberry.ID | None = None


@strawberry.input
class RecapDashboardFiltersInput(BaseTenantInput):
    """Filters for Recap Dashboard queries.

    All filters are optional. When not provided, queries will return
    data for all tenants within the current quarter (admin dashboard).
    """
    # Date range filters
    start_date: str | None = None  # ISO date string (YYYY-MM-DD)
    end_date: str | None = None    # ISO date string (YYYY-MM-DD)

    # Quarter filter (takes precedence over start_date/end_date if provided)
    quarter: str | None = None  # Quarter string like "Q1 2025"

    # RMM assigned user filter
    rmm_asigned_id: strawberry.ID | None = None

    # Distributor filter
    distributor_id: strawberry.ID | None = None
