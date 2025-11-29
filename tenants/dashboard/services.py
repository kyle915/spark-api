"""
Dashboard query services.

This module contains service classes for dashboard queries with performance optimizations.
"""
from datetime import datetime, timedelta
from django.utils import timezone

from utils.graphql.mixins import SparkGraphQLMixin
from . import inputs


class DashboardQueriesService(SparkGraphQLMixin):
    """Service for dashboard queries with performance optimizations."""

    def _apply_filters(
        self,
        queryset,
        filters: inputs.DashboardFiltersInput | None,
        tenant_id: int
    ):
        """Apply filters to queryset efficiently."""
        if not filters:
            return queryset.filter(tenant_id=tenant_id)

        # Start with tenant filter
        queryset = queryset.filter(tenant_id=tenant_id)

        # Date range filters
        if filters.start_date:
            try:
                start = datetime.fromisoformat(filters.start_date.replace('Z', '+00:00'))
                queryset = queryset.filter(created_at__gte=start)
            except (ValueError, AttributeError):
                pass

        if filters.end_date:
            try:
                end = datetime.fromisoformat(filters.end_date.replace('Z', '+00:00'))
                # Add one day to include the entire end date
                end = end + timedelta(days=1)
                queryset = queryset.filter(created_at__lt=end)
            except (ValueError, AttributeError):
                pass

        # Location filters
        if filters.location_id:
            queryset = queryset.filter(location_id=filters.location_id)
        elif filters.location_code:
            queryset = queryset.filter(location__code=filters.location_code)

        # Event filters
        if filters.event_type_id:
            queryset = queryset.filter(event_type_id=filters.event_type_id)
        if filters.event_status_id:
            queryset = queryset.filter(status_id=filters.event_status_id)

        # Request filters
        if filters.request_status_id:
            queryset = queryset.filter(status_id=filters.request_status_id)
        if filters.request_type_id:
            queryset = queryset.filter(request_type_id=filters.request_type_id)
        if filters.client_id:
            queryset = queryset.filter(client_id=filters.client_id)
        if filters.distributor_id:
            queryset = queryset.filter(distributor_id=filters.distributor_id)
        if filters.retailer_id:
            queryset = queryset.filter(retailer_id=filters.retailer_id)

        return queryset

    def _get_date_range(self, filters: inputs.DashboardFiltersInput | None):
        """Get date range from filters with defaults."""
        today = timezone.now().date()
        
        if filters and filters.start_date:
            try:
                start = datetime.fromisoformat(filters.start_date.replace('Z', '+00:00')).date()
            except (ValueError, AttributeError):
                start = today - timedelta(days=30)  # Default to last 30 days
        else:
            start = today - timedelta(days=30)

        if filters and filters.end_date:
            try:
                end = datetime.fromisoformat(filters.end_date.replace('Z', '+00:00')).date()
            except (ValueError, AttributeError):
                end = today
        else:
            end = today

        return start, end

