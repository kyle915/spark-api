"""
GraphQL types for dashboard queries.

This module defines all response types for Event Dashboard and Recap Dashboard data queries.
"""
from __future__ import annotations

import strawberry
from typing import List

# Shared Filter Option Types (used by both Event and Recap Dashboards)


@strawberry.type
class DistributorOption:
    """Distributor option for filters."""
    id: strawberry.ID
    name: str


@strawberry.type
class RetailerOption:
    """Retailer/RMM option for filters."""
    id: strawberry.ID
    name: str
    address: str


@strawberry.type
class QuarterOption:
    """Quarter option for filters."""
    value: str  # e.g., "Q1 2025"
    label: str  # e.g., "Q1 2025"


@strawberry.type
class TenantOption:
    """Tenant option for filters."""
    id: strawberry.ID
    name: str


@strawberry.type
class EventDashboardFilterOptions:
    """Available filter options for Event Dashboard."""
    distributors: List[DistributorOption] | None = None
    rmms: List[RetailerOption] | None = None  # RMM = Retailer
    quarters: List[QuarterOption] | None = None
    tenants: List[TenantOption] | None = None


@strawberry.type
class ComparisonValues:
    """Period-over-period comparison values."""
    total_events: int
    consumers_sampled: int
    brand_awareness: float
    purchase_intent: float


@strawberry.type
class EventDashboardMetrics:
    """Key metrics for Event Dashboard."""
    total_events: int
    consumers_sampled: int
    brand_awareness: float  # Percentage
    purchase_intent: float  # Percentage
    comparison_period: str | None = None  # e.g., "Q4 2024"
    comparison_values: ComparisonValues | None = None


@strawberry.type
class MonthlyDataPoint:
    """Monthly performance data point."""
    month: str  # e.g., "2025-01"
    consumers_sampled: int
    willing_to_purchase: int
    conversion_rate: float  # Percentage
    events_count: int


@strawberry.type
class MonthlyPerformanceTrend:
    """Monthly performance trends chart data."""
    data_points: List[MonthlyDataPoint]


@strawberry.type
class BestMonth:
    """Best performing month."""
    month: str  # e.g., "2025-06"
    events_count: int
    consumers_count: int


@strawberry.type
class PerformanceInsights:
    """Performance insights section."""
    knew_about_brand: int
    knew_about_brand_percentage: float
    willing_to_purchase: int
    willing_to_purchase_percentage: float
    best_month: BestMonth | None = None
    growth_rate: float  # Percentage (events vs last year)


@strawberry.type
class RecentEvent:
    """Recent/upcoming event."""
    id: strawberry.ID
    name: str
    date: str  # ISO date string
    location: str  # RMM/Location name
    consumers: int
    intent_rate: float  # Percentage
    status: str  # "Upcoming", "Completed", etc.


@strawberry.type
class EventDashboard:
    """Main Event Dashboard response."""
    metrics: EventDashboardMetrics
    monthly_trends: MonthlyPerformanceTrend
    performance_insights: PerformanceInsights
    recent_events: List[RecentEvent] | None = None


# Recap Dashboard Types

@strawberry.type
class RecapDashboardFilterOptions:
    """Available filter options for Recap Dashboard."""
    distributors: List[DistributorOption] | None = None
    rmms: List[RetailerOption] | None = None  # RMM = Retailer
    quarters: List[QuarterOption] | None = None
    tenants: List[TenantOption] | None = None


@strawberry.type
class RecapComparisonValues:
    """Period-over-period comparison values for Recap Dashboard."""
    total_consumers_sampled: int
    total_purchases: int
    conversion_rate: float
    revenue_generated: float


@strawberry.type
class RecapDashboardMetrics:
    """Key metrics for Recap Dashboard."""
    total_consumers_sampled: int
    total_purchases: int
    conversion_rate: float  # Percentage
    revenue_generated: float
    comparison_period: str | None = None  # e.g., "Q4 2024"
    comparison_values: RecapComparisonValues | None = None


@strawberry.type
class RecapMonthlyDataPoint:
    """Monthly performance data point for Recap Dashboard."""
    month: str  # e.g., "2025-01"
    consumers_sampled: int
    purchases: int
    conversion_rate: float  # Percentage
    revenue: float
    recaps_count: int


@strawberry.type
class RecapMonthlyTrends:
    """Monthly performance trends chart data for Recap Dashboard."""
    data_points: List[RecapMonthlyDataPoint]


@strawberry.type
class BestRecapMonth:
    """Best performing month for Recap Dashboard."""
    month: str  # e.g., "2025-06"
    recaps_count: int
    consumers_count: int


@strawberry.type
class RecapPerformanceInsights:
    """Performance insights section for Recap Dashboard."""
    new_customers_sampled: int
    new_customers_percentage: float  # Percentage
    brand_awareness: int
    brand_awareness_percentage: float  # Percentage
    willing_to_purchase: int
    willing_to_purchase_percentage: float  # Percentage
    best_month: BestRecapMonth | None = None
    growth_rate: float  # Percentage (recaps growth vs last year)


@strawberry.type
class MarketPerformanceData:
    """Market performance data point."""
    market_id: strawberry.ID  # Retailer ID
    market_name: str  # Retailer name
    consumers: int
    purchases: int
    conversion: float  # Percentage
    demos: int  # total_engagements
    efficiency: float  # Calculated metric (percentage)


@strawberry.type
class MarketPerformanceAnalysis:
    """Market performance analysis section."""
    data_points: List[MarketPerformanceData]


@strawberry.type
class RMMPerformanceData:
    """RMM performance data point."""
    rmm_id: strawberry.ID  # Retailer ID
    rmm_name: str  # Retailer name
    consumers_sampled: int
    demos: int
    conversion_rate: float  # Percentage


@strawberry.type
class RMMPerformance:
    """RMM performance section."""
    data_points: List[RMMPerformanceData]


@strawberry.type
class RecapDashboard:
    """Main Recap Dashboard response."""
    metrics: RecapDashboardMetrics
    monthly_trends: RecapMonthlyTrends
    performance_insights: RecapPerformanceInsights
    market_analysis: MarketPerformanceAnalysis
    rmm_performance: RMMPerformance
