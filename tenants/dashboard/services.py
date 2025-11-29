"""
Dashboard query services.

This module contains service classes for dashboard queries with performance optimizations.
"""
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Callable
from functools import wraps
from django.core.cache import cache
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
                start = datetime.fromisoformat(
                    filters.start_date.replace('Z', '+00:00'))
                queryset = queryset.filter(created_at__gte=start)
            except (ValueError, AttributeError):
                pass

        if filters.end_date:
            try:
                end = datetime.fromisoformat(
                    filters.end_date.replace('Z', '+00:00'))
                # Add one day to include the entire end date
                end = end + timedelta(days=1)
                queryset = queryset.filter(created_at__lt=end)
            except (ValueError, AttributeError):
                pass

        # Location filters - apply only if model has direct location field
        # For Event querysets, location is accessed via request__location (handled in queries)
        # For Request querysets, location is accessed via distributor__location or retailer__location
        model = queryset.model
        if hasattr(model, '_meta') and 'location' in [f.name for f in model._meta.get_fields()]:
            if filters.location_id:
                queryset = queryset.filter(location_id=filters.location_id)
            elif filters.location_code:
                queryset = queryset.filter(
                    location__code=filters.location_code)

        # Event filters (only apply to Event querysets)
        model = queryset.model
        model_name = model.__name__

        if model_name == 'Event':
            if filters.event_type_id:
                queryset = queryset.filter(event_type_id=filters.event_type_id)
            if filters.event_status_id:
                queryset = queryset.filter(status_id=filters.event_status_id)

        # Request filters (only apply to Request querysets)
        if model_name == 'Request':
            if filters.request_status_id:
                queryset = queryset.filter(status_id=filters.request_status_id)
            if filters.request_type_id:
                queryset = queryset.filter(
                    request_type_id=filters.request_type_id)
            if filters.client_id:
                queryset = queryset.filter(client_id=filters.client_id)
            if filters.distributor_id:
                queryset = queryset.filter(
                    distributor_id=filters.distributor_id)
            if filters.retailer_id:
                queryset = queryset.filter(retailer_id=filters.retailer_id)

        return queryset

    def _get_date_range(self, filters: inputs.DashboardFiltersInput | None):
        """Get date range from filters with defaults."""
        today = timezone.now().date()

        if filters and filters.start_date:
            try:
                start = datetime.fromisoformat(
                    filters.start_date.replace('Z', '+00:00')).date()
            except (ValueError, AttributeError):
                start = today - timedelta(days=30)  # Default to last 30 days
        else:
            start = today - timedelta(days=30)

        if filters and filters.end_date:
            try:
                end = datetime.fromisoformat(
                    filters.end_date.replace('Z', '+00:00')).date()
            except (ValueError, AttributeError):
                end = today
        else:
            end = today

        return start, end

    def _get_cache_version(self, query_name: str, tenant_id: int) -> int:
        """
        Get current cache version for a query and tenant.

        Args:
            query_name: Name of the query
            tenant_id: The tenant ID

        Returns:
            Current version number
        """
        version_key = f"dashboard:version:{query_name}:{tenant_id}"
        return cache.get(version_key, 0)

    def _generate_cache_key(
        self,
        query_name: str,
        tenant_id: int,
        filters: inputs.DashboardFiltersInput | None = None,
        group_by: str | None = None,
    ) -> str:
        """
        Generate a cache key based on query name, tenant ID, version, and all filter parameters.

        Args:
            query_name: Name of the query (e.g., 'events_stats', 'request_time_series')
            tenant_id: The tenant ID
            filters: Optional filter input
            group_by: Optional group_by parameter for time series queries

        Returns:
            Cache key string in format: dashboard:{query_name}:{tenant_id}:v{version}:{filter_hash}
        """
        # Get current version for cache invalidation
        version = self._get_cache_version(query_name, tenant_id)

        # Build filter dict with all parameters (only non-None values)
        filter_dict = {}
        if filters:
            if filters.start_date:
                filter_dict['start_date'] = filters.start_date
            if filters.end_date:
                filter_dict['end_date'] = filters.end_date
            if filters.location_id:
                filter_dict['location_id'] = str(filters.location_id)
            if filters.location_code:
                filter_dict['location_code'] = filters.location_code
            if filters.event_type_id:
                filter_dict['event_type_id'] = str(filters.event_type_id)
            if filters.event_status_id:
                filter_dict['event_status_id'] = str(filters.event_status_id)
            if filters.request_status_id:
                filter_dict['request_status_id'] = str(
                    filters.request_status_id)
            if filters.request_type_id:
                filter_dict['request_type_id'] = str(filters.request_type_id)
            if filters.client_id:
                filter_dict['client_id'] = str(filters.client_id)
            if filters.distributor_id:
                filter_dict['distributor_id'] = str(filters.distributor_id)
            if filters.retailer_id:
                filter_dict['retailer_id'] = str(filters.retailer_id)
            if filters.tenant_id:
                filter_dict['tenant_id'] = str(filters.tenant_id)

        if group_by:
            filter_dict['group_by'] = group_by

        # Sort keys to ensure consistent hash
        sorted_items = sorted(filter_dict.items())
        filter_str = json.dumps(sorted_items, sort_keys=True)

        # Generate hash of filter parameters
        filter_hash = hashlib.md5(filter_str.encode()).hexdigest()

        return f"dashboard:{query_name}:{tenant_id}:v{version}:{filter_hash}"

    def _get_cache_ttl(self, query_name: str) -> int:
        """
        Get cache TTL in seconds for a given query.

        Args:
            query_name: Name of the query

        Returns:
            TTL in seconds
        """
        # Stats queries: 5-15 minutes (using 10 minutes = 600 seconds)
        stats_queries = ['events_stats', 'ambassadors_stats', 'request_stats']
        # Time series queries: 1 hour (3600 seconds)
        time_series_queries = ['events_time_series', 'request_time_series']
        # Detail queries: 5 minutes (300 seconds)
        detail_queries = ['event_detail']

        if query_name in stats_queries:
            return 600  # 10 minutes
        elif query_name in time_series_queries:
            return 3600  # 1 hour
        elif query_name in detail_queries:
            return 300  # 5 minutes
        else:
            return 600  # Default: 10 minutes

    async def get_cached_or_execute(
        self,
        query_name: str,
        tenant_id: int,
        execute_func: Callable,
        filters: inputs.DashboardFiltersInput | None = None,
        group_by: str | None = None,
        *args,
        **kwargs
    ) -> Any:
        """
        Get result from cache or execute function and cache the result.

        Args:
            query_name: Name of the query (for cache key generation)
            tenant_id: The tenant ID
            execute_func: Async function to execute if cache miss
            filters: Optional filter input
            group_by: Optional group_by parameter
            *args, **kwargs: Additional arguments to pass to execute_func

        Returns:
            Cached result or result from execute_func
        """
        cache_key = self._generate_cache_key(
            query_name, tenant_id, filters, group_by)
        ttl = self._get_cache_ttl(query_name)

        # Try to get from cache
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        # Cache miss - execute function
        result = await execute_func(*args, **kwargs)

        # Cache the result
        cache.set(cache_key, result, ttl)

        return result

    @staticmethod
    def invalidate_cache_for_tenant(tenant_id: int, query_names: list[str] | None = None):
        """
        Invalidate all cache entries for a tenant, optionally filtered by query names.

        This uses a pattern-based approach. Since Django's cache doesn't support
        wildcard deletion natively, we'll need to track keys or use a different approach.
        For now, we'll invalidate specific patterns if query_names are provided.

        Args:
            tenant_id: The tenant ID
            query_names: Optional list of query names to invalidate.
                        If None, invalidates all queries for the tenant.
        """
        # Note: Django's default cache backends don't support pattern-based deletion.
        # For production, consider using Redis with pattern deletion or maintaining
        # a key registry. For now, we'll invalidate known patterns.
        # This is a limitation - in production with Redis, use cache.delete_pattern()

        if query_names is None:
            # Invalidate all dashboard queries for this tenant
            query_names = [
                'events_stats', 'events_time_series',
                'ambassadors_stats',
                'request_stats', 'request_time_series',
                'event_detail'
            ]

        # Since we can't do wildcard deletion with default cache,
        # we'll need to track keys or use Redis in production.
        # For now, this is a placeholder that would need Redis or key tracking.
        pass
