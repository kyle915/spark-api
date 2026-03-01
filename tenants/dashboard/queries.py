"""
Dashboard queries for client dashboards.

This module provides GraphQL queries for Event Dashboard and Recap Dashboard data including:
- Event Dashboard filter options
- Event Dashboard with metrics, trends, insights, and recent events
- Recap Dashboard filter options
- Recap Dashboard with metrics, trends, insights, market analysis, and RMM performance

All queries are optimized for performance using database-level aggregations.
"""
import strawberry
import hashlib
import json
from asgiref.sync import sync_to_async
from django.db.models import (
    Case, Count, Q, Sum, When, IntegerField
)
from django.db.models.functions import (
    TruncMonth
)
from django.utils import timezone

from utils.graphql.permissions import StrictIsAuthenticated
from . import types, inputs
from .services import DashboardQueriesService
from events import models as event_models


@strawberry.type
class DashboardQueries:
    """Dashboard queries for client dashboards."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_dashboard_filter_options(
        self,
        info: strawberry.Info,
    ) -> types.EventDashboardFilterOptions:
        """Get available filter options for Event Dashboard."""
        service = DashboardQueriesService()

        async def _execute_query():
            from recaps import models as recap_models
            from tenants import models as tenant_models

            # Get events that have recaps (all tenants - admin dashboard)
            events_with_recaps = event_models.Event.objects.filter(
                recaps__isnull=False
            ).distinct()

            # Get unique distributors from events with recaps
            distributors_data = await sync_to_async(list)(
                events_with_recaps.select_related('request__distributor')
                .filter(request__distributor__isnull=False)
                .values('request__distributor_id', 'request__distributor__name')
                .distinct()
                .order_by('request__distributor__name')
            )

            distributors = [
                types.DistributorOption(
                    id=str(item['request__distributor_id']),
                    name=item['request__distributor__name']
                )
                for item in distributors_data
            ] if distributors_data else None

            # Get unique assigned RMM users from events
            rmms_data = await sync_to_async(list)(
                events_with_recaps.select_related('rmm_asigned')
                .filter(rmm_asigned__isnull=False)
                .values(
                    'rmm_asigned_id',
                    'rmm_asigned__first_name',
                    'rmm_asigned__last_name',
                    'rmm_asigned__email',
                )
                .distinct()
                .order_by('rmm_asigned__first_name', 'rmm_asigned__last_name')
            )

            rmms = [
                types.RetailerOption(
                    id=str(item['rmm_asigned_id']),
                    name=(
                        f"{(item['rmm_asigned__first_name'] or '').strip()} "
                        f"{(item['rmm_asigned__last_name'] or '').strip()}"
                    ).strip()
                    or (item['rmm_asigned__email'] or ''),
                    address=''
                )
                for item in rmms_data
            ] if rmms_data else None

            # Get available quarters (from all events, not tenant-specific)
            quarters_list = service._get_available_quarters(years_back=2)
            quarters = [
                types.QuarterOption(value=q, label=q)
                for q in quarters_list
            ] if quarters_list else None

            # Get tenants user has access to
            user = info.context.request.user
            tenanted_users = await sync_to_async(list)(
                tenant_models.TenantedUser.objects.filter(
                    user=user,
                    is_active=True
                ).select_related('tenant')
                .values('tenant_id', 'tenant__name')
                .distinct()
            )

            tenants = [
                types.TenantOption(
                    id=str(item['tenant_id']),
                    name=item['tenant__name']
                )
                for item in tenanted_users
            ] if tenanted_users else None

            return types.EventDashboardFilterOptions(
                distributors=distributors,
                rmms=rmms,
                quarters=quarters,
                tenants=tenants
            )

        # Use a generic cache key since this is not tenant-specific
        cache_key = service._generate_cache_key(
            'event_dashboard_filter_options',
            0,  # Use 0 as tenant_id for admin/global queries
            None
        )
        ttl = service._get_cache_ttl('event_dashboard_filter_options')

        # Try to get from cache
        from django.core.cache import cache
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        # Cache miss - execute function
        result = await _execute_query()

        # Cache the result
        cache.set(cache_key, result, ttl)

        return result

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_dashboard(
        self,
        info: strawberry.Info,
        filters: inputs.EventDashboardFiltersInput | None = None,
    ) -> types.EventDashboard:
        """Get Event Dashboard data with metrics, trends, insights, and recent events."""
        service = DashboardQueriesService()

        async def _execute_query():
            from recaps import models as recap_models

            # Build base queryset (all tenants - admin dashboard)
            # Only filter by tenant if explicitly provided in filters
            base_queryset = event_models.Event.objects.all()
            base_queryset = service._apply_event_dashboard_filters(
                base_queryset, filters
            )

            # Get date range (defaults to current quarter if not provided)
            start_date, end_date = service._get_event_dashboard_date_range(
                filters)

            # Apply date range filter to queryset
            base_queryset = base_queryset.filter(
                Q(date__date__gte=start_date, date__date__lte=end_date) |
                Q(start_time__date__gte=start_date, start_time__date__lte=end_date) |
                Q(request__date__date__gte=start_date,
                  request__date__date__lte=end_date)
            )

            # Get events with recaps and their consumer engagements
            events_with_recaps = base_queryset.filter(
                recaps__isnull=False).distinct()

            # Key Metrics
            # Total Events
            total_events = await sync_to_async(base_queryset.count)()

            # Aggregate ConsumerEngagements data
            consumer_data = await sync_to_async(
                lambda: recap_models.ConsumerEngagements.objects.filter(
                    recap__event__in=events_with_recaps
                ).aggregate(
                    total_consumers=Sum('total_consumer', default=0),
                    total_brand_aware=Sum('brand_aware_consumers', default=0),
                    total_willing_to_purchase=Sum(
                        'willing_to_purchase_consumers', default=0)
                )
            )()

            consumers_sampled = consumer_data['total_consumers'] or 0
            total_brand_aware = consumer_data['total_brand_aware'] or 0
            total_willing_to_purchase = consumer_data['total_willing_to_purchase'] or 0

            # Calculate percentages
            brand_awareness = (
                (total_brand_aware / consumers_sampled * 100)
                if consumers_sampled > 0 else 0.0
            )
            purchase_intent = (
                (total_willing_to_purchase / consumers_sampled * 100)
                if consumers_sampled > 0 else 0.0
            )

            # Comparison period (same period previous year)
            comparison_period = None
            comparison_values = None
            if filters and filters.quarter:
                try:
                    # Parse current quarter
                    current_start, current_end = service._parse_quarter(
                        filters.quarter)
                    # Get previous year's same quarter
                    prev_year = current_start.year - 1
                    # Calculate quarter number from month
                    quarter_num = (current_start.month - 1) // 3 + 1
                    prev_quarter = f"Q{quarter_num} {prev_year}"
                    prev_start, prev_end = service._parse_quarter(prev_quarter)

                    comparison_period = prev_quarter

                    # Get previous period data (apply same filters but for previous period)
                    prev_events = event_models.Event.objects.all()
                    # Apply same filters but with previous period dates
                    prev_events = service._apply_event_dashboard_filters(
                        prev_events, filters
                    )
                    prev_events = prev_events.filter(
                        Q(date__date__gte=prev_start, date__date__lte=prev_end) |
                        Q(start_time__date__gte=prev_start, start_time__date__lte=prev_end) |
                        Q(request__date__date__gte=prev_start,
                          request__date__date__lte=prev_end)
                    )

                    prev_total_events = await sync_to_async(prev_events.count)()
                    prev_events_with_recaps = prev_events.filter(
                        recaps__isnull=False).distinct()

                    prev_consumer_data = await sync_to_async(
                        lambda: recap_models.ConsumerEngagements.objects.filter(
                            recap__event__in=prev_events_with_recaps
                        ).aggregate(
                            total_consumers=Sum('total_consumer', default=0),
                            total_brand_aware=Sum(
                                'brand_aware_consumers', default=0),
                            total_willing_to_purchase=Sum(
                                'willing_to_purchase_consumers', default=0)
                        )
                    )()

                    prev_consumers = prev_consumer_data['total_consumers'] or 0
                    prev_brand_aware = prev_consumer_data['total_brand_aware'] or 0
                    prev_willing = prev_consumer_data['total_willing_to_purchase'] or 0

                    prev_brand_awareness = (
                        (prev_brand_aware / prev_consumers * 100)
                        if prev_consumers > 0 else 0.0
                    )
                    prev_purchase_intent = (
                        (prev_willing / prev_consumers * 100)
                        if prev_consumers > 0 else 0.0
                    )

                    comparison_values = types.ComparisonValues(
                        total_events=prev_total_events,
                        consumers_sampled=prev_consumers,
                        brand_awareness=prev_brand_awareness,
                        purchase_intent=prev_purchase_intent
                    )
                except ValueError:
                    pass

            metrics = types.EventDashboardMetrics(
                total_events=total_events,
                consumers_sampled=consumers_sampled,
                brand_awareness=round(brand_awareness, 1),
                purchase_intent=round(purchase_intent, 1),
                comparison_period=comparison_period,
                comparison_values=comparison_values
            )

            # Monthly Performance Trends
            # Group by month and aggregate consumer data
            monthly_data = await sync_to_async(list)(
                recap_models.ConsumerEngagements.objects.filter(
                    recap__event__in=events_with_recaps
                ).select_related('recap__event')
                .annotate(
                    month=TruncMonth('recap__event__date')
                )
                .values('month')
                .annotate(
                    consumers_sampled=Sum('total_consumer', default=0),
                    willing_to_purchase=Sum(
                        'willing_to_purchase_consumers', default=0),
                    events_count=Count('recap__event_id', distinct=True)
                )
                .order_by('month')
            )

            monthly_data_points = []
            for item in monthly_data:
                month_str = item['month'].strftime(
                    '%Y-%m') if item['month'] else None
                if not month_str:
                    continue

                consumers = item['consumers_sampled'] or 0
                willing = item['willing_to_purchase'] or 0
                conversion_rate = (
                    (willing / consumers * 100) if consumers > 0 else 0.0
                )

                monthly_data_points.append(
                    types.MonthlyDataPoint(
                        month=month_str,
                        consumers_sampled=consumers,
                        willing_to_purchase=willing,
                        conversion_rate=round(conversion_rate, 1),
                        events_count=item['events_count'] or 0
                    )
                )

            monthly_trends = types.MonthlyPerformanceTrend(
                data_points=monthly_data_points
            )

            # Performance Insights
            knew_about_brand = total_brand_aware
            knew_about_brand_percentage = brand_awareness
            willing_to_purchase_count = total_willing_to_purchase
            willing_to_purchase_percentage = purchase_intent

            # Best Month (month with highest consumers sampled)
            best_month_data = None
            if monthly_data_points:
                best_month_point = max(
                    monthly_data_points,
                    key=lambda x: x.consumers_sampled
                )
                best_month_data = types.BestMonth(
                    month=best_month_point.month,
                    events_count=best_month_point.events_count,
                    consumers_count=best_month_point.consumers_sampled
                )

            # Growth Rate (events vs last year)
            growth_rate = 0.0
            if filters and filters.quarter:
                try:
                    current_start, current_end = service._parse_quarter(
                        filters.quarter)
                    prev_year = current_start.year - 1
                    # Calculate quarter number from month
                    quarter_num = (current_start.month - 1) // 3 + 1
                    prev_quarter = f"Q{quarter_num} {prev_year}"
                    prev_start, prev_end = service._parse_quarter(prev_quarter)

                    # Get previous period events with same filters
                    prev_events = event_models.Event.objects.all()
                    prev_events = service._apply_event_dashboard_filters(
                        prev_events, filters
                    )
                    prev_events_count = await sync_to_async(
                        prev_events.filter(
                            Q(date__date__gte=prev_start, date__date__lte=prev_end) |
                            Q(start_time__date__gte=prev_start, start_time__date__lte=prev_end) |
                            Q(request__date__date__gte=prev_start,
                              request__date__date__lte=prev_end)
                        ).count
                    )()

                    if prev_events_count > 0:
                        growth_rate = (
                            (total_events - prev_events_count) /
                            prev_events_count * 100
                        )
                except ValueError:
                    pass

            performance_insights = types.PerformanceInsights(
                knew_about_brand=knew_about_brand,
                knew_about_brand_percentage=round(
                    knew_about_brand_percentage, 1),
                willing_to_purchase=willing_to_purchase_count,
                willing_to_purchase_percentage=round(
                    willing_to_purchase_percentage, 1),
                best_month=best_month_data,
                growth_rate=round(growth_rate, 1)
            )

            # Recent Events (upcoming events)
            now = timezone.now()
            recent_events_qs = base_queryset.filter(
                Q(start_time__gt=now) | Q(date__gt=now)
            ).select_related(
                'request__retailer'
            ).prefetch_related('recaps__consumer_engagements')[:10]

            recent_events_list = await sync_to_async(list)(recent_events_qs)

            recent_events = []
            for event in recent_events_list:
                # Get retailer name (RMM)
                retailer_name = None
                if event.request and event.request.retailer:
                    retailer_name = event.request.retailer.name

                # Get consumers and intent rate from recaps
                event_consumers = 0
                event_willing = 0
                for recap in event.recaps.all():
                    for ce in recap.consumer_engagements.all():
                        event_consumers += ce.total_consumer
                        event_willing += ce.willing_to_purchase_consumers

                intent_rate = (
                    (event_willing / event_consumers * 100)
                    if event_consumers > 0 else 0.0
                )

                # Event date
                event_date = event.date or event.start_time or event.request.date if event.request else None
                date_str = event_date.strftime(
                    '%Y-%m-%d') if event_date else ''

                recent_events.append(
                    types.RecentEvent(
                        id=str(event.id),
                        name=event.name,
                        date=date_str,
                        location=retailer_name or '',
                        consumers=event_consumers,
                        intent_rate=round(intent_rate, 1),
                        status="Upcoming"
                    )
                )

            # Global KPIs for dashboard view (respecting active filters/date range)
            recap_queryset = recap_models.Recap.objects.filter(
                event__in=events_with_recaps
            )

            global_kpis_data = await sync_to_async(
                lambda: recap_queryset.aggregate(
                    single_cans_sold=Sum('total_cans_sold', default=0),
                    multi_packs_sold=Sum('total_packs_sold', default=0),
                )
            )()

            global_by_rmm_data = await sync_to_async(list)(
                recap_queryset.select_related('event__rmm_asigned')
                .filter(event__rmm_asigned__isnull=False)
                .values(
                    'event__rmm_asigned_id',
                    'event__rmm_asigned__first_name',
                    'event__rmm_asigned__last_name',
                    'event__rmm_asigned__email',
                )
                .annotate(
                    single_cans_sold=Sum('total_cans_sold', default=0),
                    multi_packs_sold=Sum('total_packs_sold', default=0),
                )
                .order_by(
                    'event__rmm_asigned__first_name',
                    'event__rmm_asigned__last_name',
                    'event__rmm_asigned__email',
                )
            )

            global_kpis_by_rmm = [
                types.RecapGlobalKPIByRMM(
                    rmm_id=str(item['event__rmm_asigned_id']),
                    rmm_name=(
                        f"{(item['event__rmm_asigned__first_name'] or '').strip()} "
                        f"{(item['event__rmm_asigned__last_name'] or '').strip()}"
                    ).strip()
                    or (item['event__rmm_asigned__email'] or ''),
                    single_cans_sold=item['single_cans_sold'] or 0,
                    multi_packs_sold=item['multi_packs_sold'] or 0,
                )
                for item in global_by_rmm_data
            ]

            global_kpis = types.RecapGlobalKPIs(
                single_cans_sold=global_kpis_data['single_cans_sold'] or 0,
                multi_packs_sold=global_kpis_data['multi_packs_sold'] or 0,
                by_rmm=global_kpis_by_rmm
            )

            return types.EventDashboard(
                metrics=metrics,
                global_kpis=global_kpis,
                monthly_trends=monthly_trends,
                performance_insights=performance_insights,
                recent_events=recent_events if recent_events else None
            )

        # Generate cache key with Event Dashboard filter extraction
        # Use tenant_id from filters if provided, otherwise use 0 for global cache
        tenant_id_for_cache = service._resolve_filter_tenant_id(filters) or 0
        filter_dict = service._extract_event_dashboard_filter_values(filters)
        filter_str = json.dumps(sorted(filter_dict.items()), sort_keys=True)
        filter_hash = hashlib.md5(filter_str.encode()).hexdigest()
        version = service._get_cache_version(
            'event_dashboard', tenant_id_for_cache)
        cache_key = f"dashboard:event_dashboard:{tenant_id_for_cache}:v{version}:{filter_hash}"
        ttl = service._get_cache_ttl('event_dashboard')

        # Try to get from cache
        from django.core.cache import cache
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        # Cache miss - execute function
        result = await _execute_query()

        # Cache the result
        cache.set(cache_key, result, ttl)

        return result

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_dashboard_filter_options(
        self,
        info: strawberry.Info,
    ) -> types.RecapDashboardFilterOptions:
        """Get available filter options for Recap Dashboard."""
        service = DashboardQueriesService()

        async def _execute_query():
            from recaps import models as recap_models
            from tenants import models as tenant_models

            # Get all recaps (all tenants - admin dashboard)
            recaps = recap_models.Recap.objects.all()

            # Get unique distributors from recaps
            distributors_data = await sync_to_async(list)(
                recaps.select_related('event__request__distributor')
                .filter(event__request__distributor__isnull=False)
                .values('event__request__distributor_id', 'event__request__distributor__name')
                .distinct()
                .order_by('event__request__distributor__name')
            )

            distributors = [
                types.DistributorOption(
                    id=str(item['event__request__distributor_id']),
                    name=item['event__request__distributor__name']
                )
                for item in distributors_data
            ] if distributors_data else None

            # Get unique assigned RMM users from recap events
            rmms_data = await sync_to_async(list)(
                recaps.select_related('event__rmm_asigned')
                .filter(event__rmm_asigned__isnull=False)
                .values(
                    'event__rmm_asigned_id',
                    'event__rmm_asigned__first_name',
                    'event__rmm_asigned__last_name',
                    'event__rmm_asigned__email',
                )
                .distinct()
                .order_by('event__rmm_asigned__first_name', 'event__rmm_asigned__last_name')
            )

            rmms = [
                types.RetailerOption(
                    id=str(item['event__rmm_asigned_id']),
                    name=(
                        f"{(item['event__rmm_asigned__first_name'] or '').strip()} "
                        f"{(item['event__rmm_asigned__last_name'] or '').strip()}"
                    ).strip()
                    or (item['event__rmm_asigned__email'] or ''),
                    address=''
                )
                for item in rmms_data
            ] if rmms_data else None

            # Get available quarters (from all recaps, not tenant-specific)
            quarters_list = service._get_available_quarters(years_back=2)
            quarters = [
                types.QuarterOption(value=q, label=q)
                for q in quarters_list
            ] if quarters_list else None

            # Get tenants user has access to
            user = info.context.request.user
            tenanted_users = await sync_to_async(list)(
                tenant_models.TenantedUser.objects.filter(
                    user=user,
                    is_active=True
                ).select_related('tenant')
                .values('tenant_id', 'tenant__name')
                .distinct()
            )

            tenants = [
                types.TenantOption(
                    id=str(item['tenant_id']),
                    name=item['tenant__name']
                )
                for item in tenanted_users
            ] if tenanted_users else None

            return types.RecapDashboardFilterOptions(
                distributors=distributors,
                rmms=rmms,
                quarters=quarters,
                tenants=tenants
            )

        # Use a generic cache key since this is not tenant-specific
        cache_key = service._generate_cache_key(
            'recap_dashboard_filter_options',
            0,  # Use 0 as tenant_id for admin/global queries
            None
        )
        ttl = service._get_cache_ttl('recap_dashboard_filter_options')

        # Try to get from cache
        from django.core.cache import cache
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        # Cache miss - execute function
        result = await _execute_query()

        # Cache the result
        cache.set(cache_key, result, ttl)

        return result

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_dashboard(
        self,
        info: strawberry.Info,
        filters: inputs.RecapDashboardFiltersInput | None = None,
    ) -> types.RecapDashboard:
        """Get Recap Dashboard data with metrics, trends, insights, market analysis, and RMM performance."""
        service = DashboardQueriesService()

        async def _execute_query():
            from recaps import models as recap_models

            # Build base queryset (all tenants - admin dashboard)
            # Only filter by tenant if explicitly provided in filters
            base_queryset = recap_models.Recap.objects.all()
            base_queryset = service._apply_recap_dashboard_filters(
                base_queryset, filters
            )

            # Get date range (defaults to current quarter if not provided)
            start_date, end_date = service._get_recap_dashboard_date_range(
                filters)

            # Apply date range filter to queryset
            base_queryset = base_queryset.filter(
                Q(event__date__date__gte=start_date, event__date__date__lte=end_date) |
                Q(event__start_time__date__gte=start_date, event__start_time__date__lte=end_date) |
                Q(event__request__date__date__gte=start_date,
                  event__request__date__date__lte=end_date) |
                Q(created_at__date__gte=start_date,
                  created_at__date__lte=end_date)
            )

            # Get recaps with consumer engagements
            recaps_with_engagements = base_queryset.filter(
                consumer_engagements__isnull=False).distinct()

            # Key Metrics
            # Total Consumers Sampled
            consumers_data = await sync_to_async(
                lambda: recap_models.ConsumerEngagements.objects.filter(
                    recap__in=recaps_with_engagements
                ).aggregate(
                    total_consumers=Sum('total_consumer', default=0),
                    total_willing=Sum(
                        'willing_to_purchase_consumers', default=0),
                    total_first_time=Sum('first_time_consumers', default=0),
                    total_brand_aware=Sum('brand_aware_consumers', default=0)
                )
            )()

            total_consumers_sampled = consumers_data['total_consumers'] or 0
            total_willing = consumers_data['total_willing'] or 0

            # Total Purchases (products_sold from Recap)
            total_purchases_data = await sync_to_async(
                lambda: base_queryset.aggregate(
                    total=Sum('products_sold', default=0)
                )
            )()
            total_purchases = total_purchases_data['total'] or 0

            # Conversion Rate
            conversion_rate = (
                (total_willing / total_consumers_sampled * 100)
                if total_consumers_sampled > 0 else 0.0
            )

            # Revenue Generated (total_earnings from Recap)
            revenue_data = await sync_to_async(
                lambda: base_queryset.aggregate(
                    total=Sum('total_earnings', default=0)
                )
            )()
            revenue_generated = revenue_data['total'] or 0.0

            # Comparison values (if quarter filter is provided)
            comparison_period = None
            comparison_values = None
            if filters and filters.quarter:
                try:
                    current_start, current_end = service._parse_quarter(
                        filters.quarter)
                    # Get previous year's same quarter
                    prev_year = current_start.year - 1
                    quarter_num = (current_start.month - 1) // 3 + 1
                    prev_quarter = f"Q{quarter_num} {prev_year}"
                    prev_start, prev_end = service._parse_quarter(prev_quarter)

                    comparison_period = prev_quarter

                    # Get previous period recaps with same filters
                    prev_recaps = recap_models.Recap.objects.all()
                    prev_recaps = service._apply_recap_dashboard_filters(
                        prev_recaps, filters
                    )
                    prev_recaps = prev_recaps.filter(
                        Q(event__date__date__gte=prev_start, event__date__date__lte=prev_end) |
                        Q(event__start_time__date__gte=prev_start, event__start_time__date__lte=prev_end) |
                        Q(event__request__date__date__gte=prev_start,
                          event__request__date__date__lte=prev_end) |
                        Q(created_at__date__gte=prev_start,
                          created_at__date__lte=prev_end)
                    )

                    prev_recaps_with_engagements = prev_recaps.filter(
                        consumer_engagements__isnull=False).distinct()

                    prev_consumers_data = await sync_to_async(
                        lambda: recap_models.ConsumerEngagements.objects.filter(
                            recap__in=prev_recaps_with_engagements
                        ).aggregate(
                            total_consumers=Sum('total_consumer', default=0),
                            total_willing=Sum(
                                'willing_to_purchase_consumers', default=0)
                        )
                    )()

                    prev_total_consumers = prev_consumers_data['total_consumers'] or 0
                    prev_total_willing = prev_consumers_data['total_willing'] or 0

                    prev_purchases_data = await sync_to_async(
                        lambda: prev_recaps.aggregate(
                            total=Sum('products_sold', default=0)
                        )
                    )()
                    prev_total_purchases = prev_purchases_data['total'] or 0

                    prev_revenue_data = await sync_to_async(
                        lambda: prev_recaps.aggregate(
                            total=Sum('total_earnings', default=0)
                        )
                    )()
                    prev_revenue = prev_revenue_data['total'] or 0.0

                    prev_conversion_rate = (
                        (prev_total_willing / prev_total_consumers * 100)
                        if prev_total_consumers > 0 else 0.0
                    )

                    comparison_values = types.RecapComparisonValues(
                        total_consumers_sampled=prev_total_consumers,
                        total_purchases=prev_total_purchases,
                        conversion_rate=prev_conversion_rate,
                        revenue_generated=float(prev_revenue)
                    )
                except ValueError:
                    pass

            metrics = types.RecapDashboardMetrics(
                total_consumers_sampled=total_consumers_sampled,
                total_purchases=total_purchases,
                conversion_rate=round(conversion_rate, 1),
                revenue_generated=float(revenue_generated),
                comparison_period=comparison_period,
                comparison_values=comparison_values
            )

            # Monthly Trends
            monthly_data = await sync_to_async(list)(
                base_queryset.annotate(
                    month=TruncMonth('event__date')
                ).values('month')
                .annotate(
                    recaps_count=Count('id'),
                    consumers=Sum(
                        'consumer_engagements__total_consumer', default=0),
                    purchases=Sum('products_sold', default=0),
                    revenue=Sum('total_earnings', default=0),
                    willing=Sum(
                        'consumer_engagements__willing_to_purchase_consumers', default=0)
                )
                .order_by('month')
            )

            monthly_points = []
            for item in monthly_data:
                month_str = item['month'].strftime(
                    '%Y-%m') if item['month'] else ''
                consumers_month = item['consumers'] or 0
                willing_month = item['willing'] or 0
                conversion_month = (
                    (willing_month / consumers_month * 100)
                    if consumers_month > 0 else 0.0
                )

                monthly_points.append(
                    types.RecapMonthlyDataPoint(
                        month=month_str,
                        consumers_sampled=consumers_month,
                        purchases=item['purchases'] or 0,
                        conversion_rate=round(conversion_month, 1),
                        revenue=float(item['revenue'] or 0),
                        recaps_count=item['recaps_count'] or 0
                    )
                )

            monthly_trends = types.RecapMonthlyTrends(
                data_points=monthly_points
            )

            # Performance Insights
            new_customers = consumers_data['total_first_time'] or 0
            new_customers_percentage = (
                (new_customers / total_consumers_sampled * 100)
                if total_consumers_sampled > 0 else 0.0
            )

            brand_awareness = consumers_data['total_brand_aware'] or 0
            brand_awareness_percentage = (
                (brand_awareness / total_consumers_sampled * 100)
                if total_consumers_sampled > 0 else 0.0
            )

            willing_to_purchase_percentage = (
                (total_willing / total_consumers_sampled * 100)
                if total_consumers_sampled > 0 else 0.0
            )

            # Best Month
            best_month = None
            if monthly_points:
                best_point = max(
                    monthly_points, key=lambda x: x.consumers_sampled)
                best_month = types.BestRecapMonth(
                    month=best_point.month,
                    recaps_count=best_point.recaps_count,
                    consumers_count=best_point.consumers_sampled
                )

            # Growth Rate (recaps vs last year)
            growth_rate = 0.0
            if filters and filters.quarter:
                try:
                    current_start, current_end = service._parse_quarter(
                        filters.quarter)
                    prev_year = current_start.year - 1
                    quarter_num = (current_start.month - 1) // 3 + 1
                    prev_quarter = f"Q{quarter_num} {prev_year}"
                    prev_start, prev_end = service._parse_quarter(prev_quarter)

                    prev_recaps = recap_models.Recap.objects.all()
                    prev_recaps = service._apply_recap_dashboard_filters(
                        prev_recaps, filters
                    )
                    prev_recaps_count = await sync_to_async(
                        prev_recaps.filter(
                            Q(event__date__date__gte=prev_start, event__date__date__lte=prev_end) |
                            Q(event__start_time__date__gte=prev_start, event__start_time__date__lte=prev_end) |
                            Q(event__request__date__date__gte=prev_start,
                              event__request__date__date__lte=prev_end) |
                            Q(created_at__date__gte=prev_start,
                              created_at__date__lte=prev_end)
                        ).count
                    )()

                    current_recaps_count = await sync_to_async(base_queryset.count)()

                    if prev_recaps_count > 0:
                        growth_rate = (
                            (current_recaps_count - prev_recaps_count) /
                            prev_recaps_count * 100
                        )
                except ValueError:
                    pass

            performance_insights = types.RecapPerformanceInsights(
                new_customers_sampled=new_customers,
                new_customers_percentage=round(new_customers_percentage, 1),
                brand_awareness=brand_awareness,
                brand_awareness_percentage=round(
                    brand_awareness_percentage, 1),
                willing_to_purchase=total_willing,
                willing_to_purchase_percentage=round(
                    willing_to_purchase_percentage, 1),
                best_month=best_month,
                growth_rate=round(growth_rate, 1)
            )

            # Market Analysis (grouped by retailer)
            # Get retailers from recap.retailer
            market_data_recap = await sync_to_async(list)(
                base_queryset.select_related('retailer')
                .filter(retailer__isnull=False)
                .values('retailer_id', 'retailer__name')
                .annotate(
                    consumers=Sum(
                        'consumer_engagements__total_consumer', default=0),
                    purchases=Sum('products_sold', default=0),
                    demos=Sum('total_engagements', default=0),
                    willing=Sum(
                        'consumer_engagements__willing_to_purchase_consumers', default=0)
                )
            )

            # Get retailers from event.request.retailer (where recap.retailer is null)
            market_data_event = await sync_to_async(list)(
                base_queryset.select_related('event__request__retailer')
                .filter(
                    retailer__isnull=True,
                    event__request__retailer__isnull=False
                )
                .values('event__request__retailer_id', 'event__request__retailer__name')
                .annotate(
                    consumers=Sum(
                        'consumer_engagements__total_consumer', default=0),
                    purchases=Sum('products_sold', default=0),
                    demos=Sum('total_engagements', default=0),
                    willing=Sum(
                        'consumer_engagements__willing_to_purchase_consumers', default=0)
                )
            )

            # Combine and aggregate by retailer
            market_dict = {}
            for item in market_data_recap:
                r_id = item['retailer_id']
                if r_id not in market_dict:
                    market_dict[r_id] = {
                        'market_id': r_id,
                        'market_name': item['retailer__name'],
                        'consumers': 0,
                        'purchases': 0,
                        'demos': 0,
                        'willing': 0
                    }
                market_dict[r_id]['consumers'] += item['consumers'] or 0
                market_dict[r_id]['purchases'] += item['purchases'] or 0
                market_dict[r_id]['demos'] += item['demos'] or 0
                market_dict[r_id]['willing'] += item['willing'] or 0

            for item in market_data_event:
                r_id = item['event__request__retailer_id']
                if r_id not in market_dict:
                    market_dict[r_id] = {
                        'market_id': r_id,
                        'market_name': item['event__request__retailer__name'],
                        'consumers': 0,
                        'purchases': 0,
                        'demos': 0,
                        'willing': 0
                    }
                market_dict[r_id]['consumers'] += item['consumers'] or 0
                market_dict[r_id]['purchases'] += item['purchases'] or 0
                market_dict[r_id]['demos'] += item['demos'] or 0
                market_dict[r_id]['willing'] += item['willing'] or 0

            market_points = []
            for market in market_dict.values():
                market_consumers = market['consumers']
                market_willing = market['willing']
                market_conversion = (
                    (market_willing / market_consumers * 100)
                    if market_consumers > 0 else 0.0
                )
                # Efficiency: purchases / consumers * 100
                market_efficiency = (
                    (market['purchases'] / market_consumers * 100)
                    if market_consumers > 0 else 0.0
                )

                market_points.append(
                    types.MarketPerformanceData(
                        market_id=str(market['market_id']),
                        market_name=market['market_name'] or '',
                        consumers=market_consumers,
                        purchases=market['purchases'],
                        conversion=round(market_conversion, 1),
                        demos=market['demos'],
                        efficiency=round(market_efficiency, 1)
                    )
                )

            # Sort by consumers descending
            market_points.sort(key=lambda x: x.consumers, reverse=True)

            market_analysis = types.MarketPerformanceAnalysis(
                data_points=market_points
            )

            # RMM Performance (grouped by retailer, similar to market analysis)
            rmm_dict = {}
            for item in market_data_recap:
                r_id = item['retailer_id']
                if r_id not in rmm_dict:
                    rmm_dict[r_id] = {
                        'rmm_id': r_id,
                        'rmm_name': item['retailer__name'],
                        'consumers_sampled': 0,
                        'demos': 0,
                        'willing': 0
                    }
                rmm_dict[r_id]['consumers_sampled'] += item['consumers'] or 0
                rmm_dict[r_id]['demos'] += item['demos'] or 0
                rmm_dict[r_id]['willing'] += item['willing'] or 0

            for item in market_data_event:
                r_id = item['event__request__retailer_id']
                if r_id not in rmm_dict:
                    rmm_dict[r_id] = {
                        'rmm_id': r_id,
                        'rmm_name': item['event__request__retailer__name'],
                        'consumers_sampled': 0,
                        'demos': 0,
                        'willing': 0
                    }
                rmm_dict[r_id]['consumers_sampled'] += item['consumers'] or 0
                rmm_dict[r_id]['demos'] += item['demos'] or 0
                rmm_dict[r_id]['willing'] += item['willing'] or 0

            rmm_points = []
            for rmm in rmm_dict.values():
                rmm_consumers = rmm['consumers_sampled']
                rmm_willing = rmm['willing']
                rmm_conversion = (
                    (rmm_willing / rmm_consumers * 100)
                    if rmm_consumers > 0 else 0.0
                )

                rmm_points.append(
                    types.RMMPerformanceData(
                        rmm_id=str(rmm['rmm_id']),
                        rmm_name=rmm['rmm_name'] or '',
                        consumers_sampled=rmm_consumers,
                        demos=rmm['demos'],
                        conversion_rate=round(rmm_conversion, 1)
                    )
                )

            # Sort by consumers_sampled descending
            rmm_points.sort(key=lambda x: x.consumers_sampled, reverse=True)

            rmm_performance = types.RMMPerformance(
                data_points=rmm_points
            )

            return types.RecapDashboard(
                metrics=metrics,
                monthly_trends=monthly_trends,
                performance_insights=performance_insights,
                market_analysis=market_analysis,
                rmm_performance=rmm_performance
            )

        # Generate cache key with Recap Dashboard filter extraction
        # Use tenant_id from filters if provided, otherwise use 0 for global cache
        tenant_id_for_cache = service._resolve_filter_tenant_id(filters) or 0
        filter_dict = service._extract_recap_dashboard_filter_values(filters)
        filter_str = json.dumps(sorted(filter_dict.items()), sort_keys=True)
        filter_hash = hashlib.md5(filter_str.encode()).hexdigest()
        version = service._get_cache_version(
            'recap_dashboard', tenant_id_for_cache)
        cache_key = f"dashboard:recap_dashboard:{tenant_id_for_cache}:v{version}:{filter_hash}"
        ttl = service._get_cache_ttl('recap_dashboard')

        # Try to get from cache
        from django.core.cache import cache
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        # Cache miss - execute function
        result = await _execute_query()

        # Cache the result
        cache.set(cache_key, result, ttl)

        return result

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def latest_insights(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
    ) -> types.Insights | None:
        """Get the latest Insights ordered by created_at for a tenant."""
        from tenants import models as tenant_models
        from utils.graphql.mixins import resolve_id_to_int

        async def _execute_query():
            user = info.context.request.user

            # Resolve tenant_id if provided
            resolved_tenant_id = None
            if tenant_id:
                try:
                    resolved_tenant_id = resolve_id_to_int(tenant_id)
                except (ValueError, TypeError):
                    return None

            # Get tenant - use user's tenant if not specified
            if not resolved_tenant_id:
                # Get user's tenant
                tenanted_user = await sync_to_async(
                    tenant_models.TenantedUser.objects.filter(
                        user=user, is_active=True
                    ).select_related("tenant").first
                )()
                if not tenanted_user:
                    return None
                resolved_tenant_id = tenanted_user.tenant.id

            # Get latest insights for the tenant
            latest_insights = await sync_to_async(
                tenant_models.Insights.objects.filter(
                    tenant_id=resolved_tenant_id
                )
                .select_related("tenant")
                .prefetch_related("reports")
                .order_by("-created_at")
                .first
            )()

            if not latest_insights:
                return None

            # Build reports list
            # Order by priority level (high=3, medium=2, low=1) then by created_at
            priority_order = Case(
                When(priority="high", then=3),
                When(priority="medium", then=2),
                When(priority="low", then=1),
                default=0,
                output_field=IntegerField(),
            )
            reports_queryset = latest_insights.reports.all().annotate(
                priority_order=priority_order
            ).order_by("-priority_order", "created_at")
            reports_list_data = await sync_to_async(list)(reports_queryset)
            reports_list = []
            for report in reports_list_data:
                reports_list.append(
                    types.InsightReport(
                        id=strawberry.ID(str(report.id)),
                        uuid=str(report.uuid),
                        title=report.title,
                        content=report.content,
                        priority=report.priority,
                        createdAt=report.created_at.isoformat(),
                    )
                )

            return types.Insights(
                id=strawberry.ID(str(latest_insights.id)),
                uuid=str(latest_insights.uuid),
                tenantId=strawberry.ID(str(latest_insights.tenant.id)),
                fromDate=latest_insights.from_date.isoformat(),
                toDate=latest_insights.to_date.isoformat(),
                totalFeedbackCount=latest_insights.total_feedback_count,
                reports=reports_list,
                createdAt=latest_insights.created_at.isoformat(),
            )

        return await _execute_query()
