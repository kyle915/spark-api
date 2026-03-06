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

    # Optional year for goals progress (e.g. 2025); when not set, year is derived from dashboard date range
    year: int | None = None


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


@strawberry.input
class SetGoalsInput(BaseTenantInput):
    """Input to create or update goals for a user for a given tenant and year."""

    year: int
    event_target_goal: int | None = None
    consumer_sampling_goal: int | None = None
    brand_awareness_goal: float | None = None
    purchase_intent_goal: float | None = None
    female_participation_goal: float | None = None
    first_time_buyers_goal: int | None = None
