"""
Dashboard query services.

This module contains service classes for dashboard queries with performance optimizations.
"""
import hashlib
import json
from datetime import datetime, date
from typing import Any, Callable, List, Tuple
import strawberry
from django.core.cache import cache
from django.utils import timezone
from django.db.models import Q
from graphql import GraphQLError

from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from . import inputs


class DashboardQueriesService(SparkGraphQLMixin):
    """Service for dashboard queries with performance optimizations."""

    # Quarter definitions: (quarter_num, start_month, end_month, end_day)
    QUARTER_DEFINITIONS = [
        (1, 1, 3, 31),   # Q1: Jan-Mar
        (2, 4, 6, 30),   # Q2: Apr-Jun
        (3, 7, 9, 30),   # Q3: Jul-Sep
        (4, 10, 12, 31),  # Q4: Oct-Dec
    ]

    @staticmethod
    def _get_quarter_date_range(quarter_num: int, year: int) -> Tuple[date, date]:
        """
        Get start and end dates for a given quarter and year.

        Args:
            quarter_num: Quarter number (1-4)
            year: Year (e.g., 2025)

        Returns:
            Tuple of (start_date, end_date)

        Raises:
            ValueError: If quarter_num is not between 1 and 4
        """
        if not 1 <= quarter_num <= 4:
            raise ValueError(
                f"Invalid quarter number: {quarter_num}. Must be between 1 and 4.")

        # Find quarter definition
        quarter_def = next(
            (sm, em, ed) for q, sm, em, ed in DashboardQueriesService.QUARTER_DEFINITIONS
            if q == quarter_num
        )

        start_month, end_month, end_day = quarter_def
        start_date = date(year, start_month, 1)
        end_date = date(year, end_month, end_day)

        return start_date, end_date

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
        filters: inputs.EventDashboardFiltersInput | inputs.RecapDashboardFiltersInput | None = None,
    ) -> str:
        """
        Generate a cache key based on query name, tenant ID, version, and all filter parameters.

        Args:
            query_name: Name of the query (e.g., 'event_dashboard', 'recap_dashboard', etc.)
            tenant_id: The tenant ID
            filters: Optional Event Dashboard or Recap Dashboard filter input

        Returns:
            Cache key string in format: dashboard:{query_name}:{tenant_id}:v{version}:{filter_hash}
        """
        # Get current version for cache invalidation
        version = self._get_cache_version(query_name, tenant_id)

        # Build filter dict with all parameters (only non-None values)
        # Extract filter values based on query type
        if query_name.startswith('recap_dashboard'):
            filter_dict = self._extract_recap_dashboard_filter_values(filters)
        else:
            filter_dict = self._extract_event_dashboard_filter_values(filters)

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
        # Filter Options: 1 hour TTL (rarely changes)
        filter_options_queries = [
            'event_dashboard_filter_options',
            'recap_dashboard_filter_options'
        ]
        # Dashboards: 10 minutes TTL (frequently accessed, needs freshness)
        dashboard_queries = [
            'event_dashboard',
            'recap_dashboard'
        ]

        if query_name in filter_options_queries:
            return 3600  # 1 hour
        elif query_name in dashboard_queries:
            return 600  # 10 minutes
        else:
            return 600  # Default: 10 minutes

    async def get_cached_or_execute(
        self,
        query_name: str,
        tenant_id: int,
        execute_func: Callable,
        filters: inputs.EventDashboardFiltersInput | None = None,
        *args,
        **kwargs
    ) -> Any:
        """
        Get result from cache or execute function and cache the result.

        Args:
            query_name: Name of the query (for cache key generation)
            tenant_id: The tenant ID
            execute_func: Async function to execute if cache miss
            filters: Optional Event Dashboard filter input
            *args, **kwargs: Additional arguments to pass to execute_func

        Returns:
            Cached result or result from execute_func
        """
        cache_key = self._generate_cache_key(
            query_name, tenant_id, filters)
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
            # Invalidate all Dashboard queries for this tenant
            query_names = [
                'event_dashboard_filter_options',
                'event_dashboard',
                'recap_dashboard_filter_options',
                'recap_dashboard'
            ]

        for query_name in query_names:
            version_key = f"dashboard:version:{query_name}:{tenant_id}"
            current_version = cache.get(version_key, 0)
            cache.set(version_key, current_version + 1, timeout=None)

    def _get_current_quarter(self) -> Tuple[str, date, date]:
        """
        Get current quarter string and date range.

        Returns:
            Tuple of (quarter_string, start_date, end_date)
            e.g., ("Q1 2025", date(2025, 1, 1), date(2025, 3, 31))
        """
        now = timezone.now().date()
        year = now.year
        month = now.month

        # Determine current quarter based on month
        quarter_num, _, _, _ = next(
            (q, sm, em, ed) for q, sm, em, ed in self.QUARTER_DEFINITIONS
            if sm <= month <= em
        )

        start_date, end_date = self._get_quarter_date_range(quarter_num, year)
        quarter_string = f"Q{quarter_num} {year}"

        return quarter_string, start_date, end_date

    def _parse_quarter(self, quarter: str) -> Tuple[date, date]:
        """
        Parse quarter string to date range.

        Args:
            quarter: Quarter string like "Q1 2025"

        Returns:
            Tuple of (start_date, end_date)

        Raises:
            ValueError: If quarter string is invalid
        """
        try:
            # Parse format: "Q1 2025" or "Q2 2024"
            parts = quarter.strip().split()
            if len(parts) != 2:
                raise ValueError(f"Invalid quarter format: {quarter}")

            quarter_part = parts[0].upper()
            if not quarter_part.startswith('Q') or len(quarter_part) != 2:
                raise ValueError(f"Invalid quarter format: {quarter}")

            quarter_num = int(quarter_part[1])
            if quarter_num < 1 or quarter_num > 4:
                raise ValueError(f"Invalid quarter number: {quarter_num}")

            year = int(parts[1])

            # Calculate date range using utility function
            start_date, end_date = self._get_quarter_date_range(
                quarter_num, year)

            return start_date, end_date
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid quarter format: {quarter}") from e

    def _get_available_quarters(
        self, years_back: int = 2
    ) -> List[str]:
        """
        Get available quarters from event data.

        Args:
            years_back: Number of years back to include (unused)

        Returns:
            List of quarter strings like ["Q1 2026", "Q2 2026", ...]
        """
        year = 2026
        return [f"Q{quarter_num} {year}" for quarter_num in range(1, 5)]

    def _apply_event_dashboard_filters(
        self,
        queryset,
        filters: inputs.EventDashboardFiltersInput | None
    ):
        """
        Apply Event Dashboard specific filters including quarter and tenant.

        Args:
            queryset: Base queryset (Event model)
            filters: EventDashboardFiltersInput filters

        Returns:
            Filtered queryset
        """
        if not filters:
            return queryset

        # Tenant filter (only if provided - admin dashboard shows all by default)
        tenant_id = self._resolve_filter_tenant_id(filters)
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)

        # Quarter filter (takes precedence over start_date/end_date)
        if filters.quarter:
            try:
                start_date, end_date = self._parse_quarter(filters.quarter)
                # Use event date or start_time for filtering
                queryset = queryset.filter(
                    Q(date__date__gte=start_date, date__date__lte=end_date) |
                    Q(start_time__date__gte=start_date, start_time__date__lte=end_date) |
                    Q(request__date__date__gte=start_date,
                      request__date__date__lte=end_date)
                )
            except ValueError:
                # Invalid quarter format, ignore
                pass
        elif filters.start_date or filters.end_date:
            # Use date range if quarter not provided
            if filters.start_date:
                try:
                    start = datetime.fromisoformat(
                        filters.start_date.replace('Z', '+00:00')).date()
                    queryset = queryset.filter(
                        Q(date__date__gte=start) |
                        Q(start_time__date__gte=start) |
                        Q(request__date__date__gte=start)
                    )
                except (ValueError, AttributeError):
                    pass

            if filters.end_date:
                try:
                    end = datetime.fromisoformat(
                        filters.end_date.replace('Z', '+00:00')).date()
                    queryset = queryset.filter(
                        Q(date__date__lte=end) |
                        Q(start_time__date__lte=end) |
                        Q(request__date__date__lte=end)
                    )
                except (ValueError, AttributeError):
                    pass

        # RMM assigned user filter
        if filters.rmm_asigned_id:
            rmm_asigned_id = self._resolve_filter_id(
                filters.rmm_asigned_id, "rmm_asigned"
            )
            queryset = queryset.filter(
                Q(rmm_asigned_id=rmm_asigned_id)
                | Q(request__rmm_asigned_id=rmm_asigned_id)
            )

        # Distributor filter (supports single distributor_id and multiple distributor_ids)
        distributor_ids = self._resolve_distributor_filter_ids(filters)
        if distributor_ids:
            queryset = queryset.filter(request__distributor_id__in=distributor_ids)

        # State filter — events can hit `state_id` directly or via the
        # request → location → state chain. Accept either single id or
        # list, then merge into a single IN-clause so callers don't
        # have to pick.
        state_ids = self._resolve_id_list_filter(
            getattr(filters, "state_id", None),
            getattr(filters, "state_ids", None),
            "state",
        )
        if state_ids:
            queryset = queryset.filter(
                Q(state_id__in=state_ids)
                | Q(request__state_id__in=state_ids)
                | Q(location__state_id__in=state_ids)
            )

        # Retailer filter — retailer is stored on event + request. We
        # use OR'd lookups for robustness so legacy rows without an
        # event-level retailer still match the request-level one.
        retailer_ids = self._resolve_id_list_filter(
            getattr(filters, "retailer_id", None),
            getattr(filters, "retailer_ids", None),
            "retailer",
        )
        if retailer_ids:
            queryset = queryset.filter(
                Q(retailer_id__in=retailer_ids)
                | Q(request__retailer_id__in=retailer_ids)
            )

        return queryset

    def _get_event_dashboard_date_range(
        self, filters: inputs.EventDashboardFiltersInput | None
    ) -> Tuple[date, date]:
        """
        Get date range for Event Dashboard with defaults.

        If quarter is provided, use it. Otherwise use start_date/end_date.
        If neither is provided, default to current quarter.

        Args:
            filters: EventDashboardFiltersInput filters

        Returns:
            Tuple of (start_date, end_date)
        """
        if filters and filters.quarter:
            try:
                return self._parse_quarter(filters.quarter)
            except ValueError:
                pass

        if filters and (filters.start_date or filters.end_date):
            today = timezone.now().date()
            start = today
            end = today

            if filters.start_date:
                try:
                    start = datetime.fromisoformat(
                        filters.start_date.replace('Z', '+00:00')).date()
                except (ValueError, AttributeError):
                    pass

            if filters.end_date:
                try:
                    end = datetime.fromisoformat(
                        filters.end_date.replace('Z', '+00:00')).date()
                except (ValueError, AttributeError):
                    pass

            return start, end

        # Default to current quarter
        _, start_date, end_date = self._get_current_quarter()
        return start_date, end_date

    def _extract_event_dashboard_filter_values(
        self, filters: inputs.EventDashboardFiltersInput | None
    ) -> dict[str, str]:
        """
        Extract Event Dashboard filter values for cache key generation.

        Args:
            filters: EventDashboardFiltersInput filters

        Returns:
            Dictionary of filter key-value pairs
        """
        if not filters:
            return {}

        filter_dict = {}

        # Quarter (takes precedence)
        if filters.quarter:
            filter_dict['quarter'] = filters.quarter
        else:
            # Date range
            if filters.start_date:
                filter_dict['start_date'] = filters.start_date
            if filters.end_date:
                filter_dict['end_date'] = filters.end_date

        # Other filters
        if filters.rmm_asigned_id:
            rmm_asigned_id = self._resolve_filter_id(
                filters.rmm_asigned_id, "rmm_asigned"
            )
            filter_dict['rmm_asigned_id'] = str(rmm_asigned_id)
        distributor_ids = self._resolve_distributor_filter_ids(filters)
        if distributor_ids:
            filter_dict['distributor_ids'] = ",".join(
                str(distributor_id) for distributor_id in distributor_ids
            )
        # State / retailer filters fold into the cache key so the same
        # tenant viewed under different filter combos doesn't collide.
        state_ids = self._resolve_id_list_filter(
            getattr(filters, "state_id", None),
            getattr(filters, "state_ids", None),
            "state",
        )
        if state_ids:
            filter_dict['state_ids'] = ",".join(str(s) for s in state_ids)
        retailer_ids = self._resolve_id_list_filter(
            getattr(filters, "retailer_id", None),
            getattr(filters, "retailer_ids", None),
            "retailer",
        )
        if retailer_ids:
            filter_dict['retailer_ids'] = ",".join(str(r) for r in retailer_ids)
        tenant_id = self._resolve_filter_tenant_id(filters)
        if tenant_id:
            filter_dict['tenant_id'] = str(tenant_id)
        if getattr(filters, 'year', None) is not None:
            filter_dict['year'] = str(filters.year)

        return filter_dict

    def _apply_recap_dashboard_filters(
        self,
        queryset,
        filters: inputs.RecapDashboardFiltersInput | None
    ):
        """
        Apply Recap Dashboard specific filters including quarter and tenant.

        Args:
            queryset: Base queryset (Recap model)
            filters: RecapDashboardFiltersInput filters

        Returns:
            Filtered queryset
        """
        if not filters:
            return queryset

        # Tenant filter (only if provided - admin dashboard shows all by default)
        tenant_id = self._resolve_filter_tenant_id(filters)
        if tenant_id:
            # Filter by tenant via event
            queryset = queryset.filter(event__tenant_id=tenant_id)

        # Quarter filter (takes precedence over start_date/end_date)
        if filters.quarter:
            try:
                start_date, end_date = self._parse_quarter(filters.quarter)
                # Use event date or recap created_at for filtering
                queryset = queryset.filter(
                    Q(event__date__date__gte=start_date, event__date__date__lte=end_date) |
                    Q(event__start_time__date__gte=start_date, event__start_time__date__lte=end_date) |
                    Q(event__request__date__date__gte=start_date,
                      event__request__date__date__lte=end_date) |
                    Q(created_at__date__gte=start_date,
                      created_at__date__lte=end_date)
                )
            except ValueError:
                # Invalid quarter format, ignore
                pass
        elif filters.start_date or filters.end_date:
            # Use date range if quarter not provided
            if filters.start_date:
                try:
                    start = datetime.fromisoformat(
                        filters.start_date.replace('Z', '+00:00')).date()
                    queryset = queryset.filter(
                        Q(event__date__date__gte=start) |
                        Q(event__start_time__date__gte=start) |
                        Q(event__request__date__date__gte=start) |
                        Q(created_at__date__gte=start)
                    )
                except (ValueError, AttributeError):
                    pass

            if filters.end_date:
                try:
                    end = datetime.fromisoformat(
                        filters.end_date.replace('Z', '+00:00')).date()
                    queryset = queryset.filter(
                        Q(event__date__date__lte=end) |
                        Q(event__start_time__date__lte=end) |
                        Q(event__request__date__date__lte=end) |
                        Q(created_at__date__lte=end)
                    )
                except (ValueError, AttributeError):
                    pass

        # RMM assigned user filter
        if filters.rmm_asigned_id:
            rmm_asigned_id = self._resolve_filter_id(
                filters.rmm_asigned_id, "rmm_asigned"
            )
            queryset = queryset.filter(
                Q(event__rmm_asigned_id=rmm_asigned_id)
                | Q(event__request__rmm_asigned_id=rmm_asigned_id)
            )

        # Distributor filter (supports single distributor_id and multiple distributor_ids)
        distributor_ids = self._resolve_distributor_filter_ids(filters)
        if distributor_ids:
            queryset = queryset.filter(
                event__request__distributor_id__in=distributor_ids
            )

        return queryset

    def _get_recap_dashboard_date_range(
        self, filters: inputs.RecapDashboardFiltersInput | None
    ) -> Tuple[date, date]:
        """
        Get date range for Recap Dashboard with defaults.

        If quarter is provided, use it. Otherwise use start_date/end_date.
        If neither is provided, default to current quarter.

        Args:
            filters: RecapDashboardFiltersInput filters

        Returns:
            Tuple of (start_date, end_date)
        """
        if filters and filters.quarter:
            try:
                return self._parse_quarter(filters.quarter)
            except ValueError:
                pass

        if filters and (filters.start_date or filters.end_date):
            today = timezone.now().date()
            start = today
            end = today

            if filters.start_date:
                try:
                    start = datetime.fromisoformat(
                        filters.start_date.replace('Z', '+00:00')).date()
                except (ValueError, AttributeError):
                    pass

            if filters.end_date:
                try:
                    end = datetime.fromisoformat(
                        filters.end_date.replace('Z', '+00:00')).date()
                except (ValueError, AttributeError):
                    pass

            return start, end

        # Default to current quarter
        _, start_date, end_date = self._get_current_quarter()
        return start_date, end_date

    def _extract_recap_dashboard_filter_values(
        self, filters: inputs.RecapDashboardFiltersInput | None
    ) -> dict[str, str]:
        """
        Extract Recap Dashboard filter values for cache key generation.

        Args:
            filters: RecapDashboardFiltersInput filters

        Returns:
            Dictionary of filter key-value pairs
        """
        if not filters:
            return {}

        filter_dict = {}

        # Quarter (takes precedence)
        if filters.quarter:
            filter_dict['quarter'] = filters.quarter
        else:
            # Date range
            if filters.start_date:
                filter_dict['start_date'] = filters.start_date
            if filters.end_date:
                filter_dict['end_date'] = filters.end_date

        # Other filters
        if filters.rmm_asigned_id:
            rmm_asigned_id = self._resolve_filter_id(
                filters.rmm_asigned_id, "rmm_asigned"
            )
            filter_dict['rmm_asigned_id'] = str(rmm_asigned_id)
        distributor_ids = self._resolve_distributor_filter_ids(filters)
        if distributor_ids:
            filter_dict['distributor_ids'] = ",".join(
                str(distributor_id) for distributor_id in distributor_ids
            )
        tenant_id = self._resolve_filter_tenant_id(filters)
        if tenant_id:
            filter_dict['tenant_id'] = str(tenant_id)

        return filter_dict

    @staticmethod
    def _resolve_filter_tenant_id(
        filters: inputs.EventDashboardFiltersInput | inputs.RecapDashboardFiltersInput | None,
    ) -> int | None:
        """Resolve tenant id from filters, supporting relay/global IDs."""
        tenant_id_value = getattr(filters, "tenant_id", None) if filters else None
        if not tenant_id_value:
            return None
        try:
            return resolve_id_to_int(tenant_id_value)
        except (TypeError, ValueError, GraphQLError) as exc:
            raise GraphQLError("Invalid tenant ID.") from exc

    @staticmethod
    def _resolve_filter_id(value: strawberry.ID | None, label: str) -> int | None:
        """Resolve relay/global IDs used in filters to database IDs."""
        if value in (None, ""):
            return None
        try:
            return resolve_id_to_int(value)
        except (TypeError, ValueError, GraphQLError) as exc:
            raise GraphQLError(f"Invalid {label} ID.") from exc

    def _resolve_distributor_filter_ids(
        self,
        filters: inputs.EventDashboardFiltersInput | inputs.RecapDashboardFiltersInput | None,
    ) -> list[int]:
        """Resolve distributor filter(s), accepting distributor_id and distributor_ids."""
        if not filters:
            return []

        resolved_ids: list[int] = []
        distributor_id = getattr(filters, "distributor_id", None)
        distributor_ids = getattr(filters, "distributor_ids", None) or []

        if distributor_id:
            resolved_single_id = self._resolve_filter_id(distributor_id, "distributor")
            if resolved_single_id is not None:
                resolved_ids.append(resolved_single_id)

        for distributor_value in distributor_ids:
            resolved_id = self._resolve_filter_id(distributor_value, "distributor")
            if resolved_id is not None:
                resolved_ids.append(resolved_id)

        # Deduplicate and normalize order for deterministic cache keys.
        return sorted(set(resolved_ids))

    def _resolve_id_list_filter(
        self,
        single_value: strawberry.ID | None,
        list_value: list[strawberry.ID] | None,
        label: str,
    ) -> list[int]:
        """Generic helper: collapse `<label>_id` + `<label>_ids` into a
        single deduped, sorted list of DB ids. Used by state / retailer
        filters (and now distributor follows the same pattern).
        """
        resolved: list[int] = []
        if single_value:
            r = self._resolve_filter_id(single_value, label)
            if r is not None:
                resolved.append(r)
        for v in (list_value or []):
            r = self._resolve_filter_id(v, label)
            if r is not None:
                resolved.append(r)
        return sorted(set(resolved))
