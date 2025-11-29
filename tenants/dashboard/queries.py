"""
Dashboard queries for client dashboards.

This module provides GraphQL queries for dashboard data including:
- Event statistics and time series
- Ambassador statistics
- Request statistics and approval/rejection rates
- Event detail views

All queries are optimized for performance using database-level aggregations.
"""
import strawberry
from datetime import datetime, timedelta, date
from typing import List
from asgiref.sync import sync_to_async
from django.db.models import (
    Count, Q,
)
from django.db.models.functions import (
    TruncHour, TruncDay, TruncWeek, TruncMonth
)
from django.utils import timezone

from utils.graphql.permissions import StrictIsAuthenticated
from . import types, inputs
from .services import DashboardQueriesService
from events import models as event_models
from jobs import models as job_models
from ambassadors import models as ambassador_models


@strawberry.type
class DashboardQueries:
    """Dashboard queries for client dashboards."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def events_stats(
        self,
        info: strawberry.Info,
        filters: inputs.DashboardFiltersInput | None = None,
    ) -> types.EventStats:
        """Get aggregated event statistics."""
        service = DashboardQueriesService()
        tenant = await service.get_user_tenant(
            info,
            tenant_id=filters.tenant_id if filters else None,
        )

        # Try to get from cache first
        async def _execute_query():
            # Build base queryset with filters
            base_queryset = event_models.Event.objects.filter(
                tenant_id=tenant.id)
            base_queryset = service._apply_filters(
                base_queryset, filters, tenant.id)

            # Get today, this week, this month dates
            today = timezone.now().date()
            week_start = today - timedelta(days=today.weekday())
            month_start = today.replace(day=1)

            # Single query with multiple aggregations for performance
            stats = await sync_to_async(
                lambda: base_queryset.aggregate(
                    total_events=Count('id'),
                    events_today=Count('id', filter=Q(created_at__date=today)),
                    events_this_week=Count('id', filter=Q(
                        created_at__date__gte=week_start)),
                    events_this_month=Count('id', filter=Q(
                        created_at__date__gte=month_start)),
                )
            )()

            # Get events by status (single query with values + annotate)
            events_by_status = await sync_to_async(list)(
                base_queryset.values('status_id', 'status__name')
                .annotate(count=Count('id'))
                .order_by('-count')
            )

            status_counts = [
                types.EventStatusCount(
                    status_id=str(item['status_id']
                                  ) if item['status_id'] else '',
                    status_name=item['status__name'] or 'No Status',
                    count=item['count']
                )
                for item in events_by_status
            ]

            # Get events by location (single query)
            # Location is accessed via request->distributor->location or request->retailer->location
            # We'll use distributor's location as primary
            events_by_location = await sync_to_async(list)(
                base_queryset.select_related('request__distributor__location')
                .filter(request__distributor__location__isnull=False)
                .values('request__distributor__location_id', 'request__distributor__location__name', 'request__distributor__location__code')
                .annotate(count=Count('id'))
                .order_by('-count')
            )

            location_counts = [
                types.LocationEventCount(
                    location_id=str(item['request__distributor__location_id']),
                    location_name=item['request__distributor__location__name'],
                    location_code=item['request__distributor__location__code'],
                    count=item['count']
                )
                for item in events_by_location
            ]

            return types.EventStats(
                total_events=stats['total_events'] or 0,
                events_by_status=status_counts if status_counts else None,
                events_by_location=location_counts if location_counts else None,
                events_today=stats['events_today'] or 0,
                events_this_week=stats['events_this_week'] or 0,
                events_this_month=stats['events_this_month'] or 0,
            )

        # Use cache with filter-based key
        return await service.get_cached_or_execute(
            'events_stats',
            tenant.id,
            _execute_query,
            filters=filters
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def events_time_series(
        self,
        info: strawberry.Info,
        filters: inputs.DashboardFiltersInput | None = None,
        group_by: inputs.TimeGroupBy = inputs.TimeGroupBy.DAY,
    ) -> types.EventTimeSeries:
        """Get time series data for events throughout the day (historic)."""
        service = DashboardQueriesService()
        tenant = await service.get_user_tenant(
            info,
            tenant_id=filters.tenant_id if filters else None,
        )

        # Try to get from cache first
        async def _execute_query():
            # Build base queryset with filters
            base_queryset = event_models.Event.objects.filter(
                tenant_id=tenant.id)
            base_queryset = service._apply_filters(
                base_queryset, filters, tenant.id)

            # Get date range
            start_date, end_date = service._get_date_range(filters)

            # Apply date range filter
            base_queryset = base_queryset.filter(
                created_at__date__gte=start_date,
                created_at__date__lte=end_date
            )

            # Choose truncation function based on group_by
            trunc_func = {
                inputs.TimeGroupBy.HOUR: TruncHour('created_at'),
                inputs.TimeGroupBy.DAY: TruncDay('created_at'),
                inputs.TimeGroupBy.WEEK: TruncWeek('created_at'),
                inputs.TimeGroupBy.MONTH: TruncMonth('created_at'),
            }.get(group_by, TruncDay('created_at'))

            # Single query for time series with database-level grouping
            time_series_data = await sync_to_async(list)(
                base_queryset.annotate(truncated_date=trunc_func)
                .values('truncated_date')
                .annotate(count=Count('id'))
                .order_by('truncated_date')
            )

            # Get total count efficiently
            total_count = await sync_to_async(base_queryset.count)()

            data_points = [
                types.TimeSeriesDataPoint(
                    timestamp=item['truncated_date'].isoformat(
                    ) if item['truncated_date'] else '',
                    count=item['count'],
                    value=None
                )
                for item in time_series_data
            ]

            return types.EventTimeSeries(
                data_points=data_points,
                group_by=group_by.value,
                total_count=total_count,
            )

        # Use cache with filter-based key
        return await service.get_cached_or_execute(
            'events_time_series',
            tenant.id,
            _execute_query,
            filters=filters,
            group_by=group_by.value
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassadors_stats(
        self,
        info: strawberry.Info,
        filters: inputs.DashboardFiltersInput | None = None,
    ) -> types.AmbassadorStats:
        """Get ambassador working statistics."""
        service = DashboardQueriesService()
        tenant = await service.get_user_tenant(
            info,
            tenant_id=filters.tenant_id if filters else None,
        )

        # Try to get from cache first
        async def _execute_query():
            # Build base event queryset with filters
            event_queryset = event_models.Event.objects.filter(
                tenant_id=tenant.id)
            event_queryset = service._apply_filters(
                event_queryset, filters, tenant.id)

            # Get ambassadors through AmbassadorEvent (more direct relationship)
            # Use distinct to count unique ambassadors
            ambassador_events_qs = ambassador_models.AmbassadorEvent.objects.filter(
                event__in=event_queryset,
                tenant_id=tenant.id
            )

            # Also get ambassadors through jobs
            ambassador_jobs_qs = job_models.AmbassadorJob.objects.filter(
                job__event__in=event_queryset,
                tenant_id=tenant.id
            )

            # Count unique ambassadors (database-level) - union of both sources
            # Get unique ambassador IDs from both sources
            ambassador_ids_from_events = await sync_to_async(
                lambda: list(ambassador_events_qs.values_list(
                    'ambassador_id', flat=True).distinct())
            )()
            ambassador_ids_from_jobs = await sync_to_async(
                lambda: list(ambassador_jobs_qs.values_list(
                    'ambassador_id', flat=True).distinct())
            )()

            # Total unique ambassadors (union of both sets)
            total_unique = len(set(ambassador_ids_from_events)
                               | set(ambassador_ids_from_jobs))

            # Ambassadors by event (single query with prefetch)
            ambassadors_by_event_data = await sync_to_async(list)(
                ambassador_events_qs.values('event_id', 'event__name')
                .annotate(ambassador_count=Count('ambassador_id', distinct=True))
                .order_by('-ambassador_count')
            )

            event_ambassador_counts = [
                types.EventAmbassadorCount(
                    event_id=str(item['event_id']),
                    event_name=item['event__name'] or 'Unknown',
                    ambassador_count=item['ambassador_count']
                )
                for item in ambassadors_by_event_data
            ]

            # Ambassadors by location (through events)
            ambassadors_by_location_data = await sync_to_async(list)(
                ambassador_events_qs.select_related(
                    'event__request__distributor__location')
                .filter(event__request__distributor__location__isnull=False)
                .values('event__request__distributor__location_id', 'event__request__distributor__location__name', 'event__request__distributor__location__code')
                .annotate(ambassador_count=Count('ambassador_id', distinct=True))
                .order_by('-ambassador_count')
            )

            location_ambassador_counts = [
                types.LocationAmbassadorCount(
                    location_id=str(
                        item['event__request__distributor__location_id']),
                    location_name=item['event__request__distributor__location__name'],
                    location_code=item['event__request__distributor__location__code'],
                    ambassador_count=item['ambassador_count']
                )
                for item in ambassadors_by_location_data
            ]

            return types.AmbassadorStats(
                total_ambassadors_working=total_unique,
                ambassadors_by_event=event_ambassador_counts if event_ambassador_counts else None,
                ambassadors_by_location=location_ambassador_counts if location_ambassador_counts else None,
                unique_ambassadors_count=total_unique,
            )

        # Use cache with filter-based key
        return await service.get_cached_or_execute(
            'ambassadors_stats',
            tenant.id,
            _execute_query,
            filters=filters
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request_stats(
        self,
        info: strawberry.Info,
        filters: inputs.DashboardFiltersInput | None = None,
    ) -> types.RequestStats:
        """Get request statistics including approval/rejection rates."""
        service = DashboardQueriesService()
        tenant = await service.get_user_tenant(
            info,
            tenant_id=filters.tenant_id if filters else None,
        )

        # Try to get from cache first
        async def _execute_query():
            # Build base queryset with filters
            base_queryset = event_models.Request.objects.filter(
                tenant_id=tenant.id)
            base_queryset = service._apply_filters(
                base_queryset, filters, tenant.id)

            # Get approval status (status with create_event=True)
            approval_status = await sync_to_async(
                event_models.RequestStatus.objects.filter(
                    tenant_id=tenant.id,
                    create_event=True
                ).first
            )()

            # Single query with conditional aggregations for performance
            if approval_status:
                stats = await sync_to_async(
                    lambda: base_queryset.aggregate(
                        total_requests=Count('id'),
                        approved_count=Count('id', filter=Q(
                            status__create_event=True)),
                        rejected_count=Count('id', filter=Q(
                            status__create_event=False, status__isnull=False)),
                        pending_count=Count(
                            'id', filter=Q(status__isnull=True)),
                    )
                )()
            else:
                # If no approval status exists, count all non-null statuses as rejected
                stats = await sync_to_async(
                    lambda: base_queryset.aggregate(
                        total_requests=Count('id'),
                        # No approval status means 0 approved
                        approved_count=Count(
                            'id', filter=Q(status__isnull=True)),
                        rejected_count=Count(
                            'id', filter=Q(status__isnull=False)),
                        pending_count=Count(
                            'id', filter=Q(status__isnull=True)),
                    )
                )()

            total = stats['total_requests'] or 0
            approved = stats['approved_count'] or 0
            rejected = stats['rejected_count'] or 0

            # Calculate rates
            approval_rate = (approved / total * 100) if total > 0 else 0.0
            rejection_rate = (rejected / total * 100) if total > 0 else 0.0

            # Count requests with jobs assigned (requests that have events with jobs)
            requests_with_jobs = await sync_to_async(
                lambda: base_queryset.filter(
                    event__jobs__isnull=False
                ).distinct().count()
            )()

            requests_with_jobs_percentage = (
                requests_with_jobs / total * 100) if total > 0 else 0.0

            # Requests by status
            requests_by_status_data = await sync_to_async(list)(
                base_queryset.values('status_id', 'status__name')
                .annotate(count=Count('id'))
                .order_by('-count')
            )

            request_status_counts = [
                types.RequestStatusCount(
                    status_id=str(item['status_id']
                                  ) if item['status_id'] else '',
                    status_name=item['status__name'] or 'No Status',
                    count=item['count']
                )
                for item in requests_by_status_data
            ]

            return types.RequestStats(
                total_requests=total,
                approved_count=approved,
                rejected_count=rejected,
                pending_count=stats['pending_count'] or 0,
                approval_rate=round(approval_rate, 2),
                rejection_rate=round(rejection_rate, 2),
                requests_with_jobs_count=requests_with_jobs,
                requests_with_jobs_percentage=round(
                    requests_with_jobs_percentage, 2),
                requests_by_status=request_status_counts if request_status_counts else None,
            )

        # Use cache with filter-based key
        return await service.get_cached_or_execute(
            'request_stats',
            tenant.id,
            _execute_query,
            filters=filters
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def request_time_series(
        self,
        info: strawberry.Info,
        filters: inputs.DashboardFiltersInput | None = None,
        group_by: inputs.TimeGroupBy = inputs.TimeGroupBy.DAY,
    ) -> types.RequestTimeSeries:
        """Get time series data for requests (for graphs)."""
        service = DashboardQueriesService()
        tenant = await service.get_user_tenant(
            info,
            tenant_id=filters.tenant_id if filters else None,
        )

        # Try to get from cache first
        async def _execute_query():
            # Build base queryset with filters
            base_queryset = event_models.Request.objects.filter(
                tenant_id=tenant.id)
            base_queryset = service._apply_filters(
                base_queryset, filters, tenant.id)

            # Get date range
            start_date, end_date = service._get_date_range(filters)

            # Apply date range filter
            base_queryset = base_queryset.filter(
                created_at__date__gte=start_date,
                created_at__date__lte=end_date
            )

            # Choose truncation function
            trunc_func = {
                inputs.TimeGroupBy.HOUR: TruncHour('created_at'),
                inputs.TimeGroupBy.DAY: TruncDay('created_at'),
                inputs.TimeGroupBy.WEEK: TruncWeek('created_at'),
                inputs.TimeGroupBy.MONTH: TruncMonth('created_at'),
            }.get(group_by, TruncDay('created_at'))

            # Get approval status for filtering
            approval_status = await sync_to_async(
                event_models.RequestStatus.objects.filter(
                    tenant_id=tenant.id,
                    create_event=True
                ).first
            )()

            # Time series for all requests
            time_series_data = await sync_to_async(list)(
                base_queryset.annotate(truncated_date=trunc_func)
                .values('truncated_date')
                .annotate(count=Count('id'))
                .order_by('truncated_date')
            )

            # Approval trend (if approval status exists)
            approval_trend_data = []
            if approval_status:
                approval_trend_data = await sync_to_async(list)(
                    base_queryset.filter(status__create_event=True)
                    .annotate(truncated_date=trunc_func)
                    .values('truncated_date')
                    .annotate(count=Count('id'))
                    .order_by('truncated_date')
                )

            # Rejection trend
            rejection_trend_data = await sync_to_async(list)(
                base_queryset.filter(
                    status__create_event=False, status__isnull=False)
                .annotate(truncated_date=trunc_func)
                .values('truncated_date')
                .annotate(count=Count('id'))
                .order_by('truncated_date')
            )

            # Jobs assigned trend
            jobs_assigned_trend_data = await sync_to_async(list)(
                base_queryset.filter(event__jobs__isnull=False)
                .annotate(truncated_date=trunc_func)
                .values('truncated_date')
                .annotate(count=Count('id', distinct=True))
                .order_by('truncated_date')
            )

            total_count = await sync_to_async(base_queryset.count)()

            data_points = [
                types.TimeSeriesDataPoint(
                    timestamp=item['truncated_date'].isoformat(
                    ) if item['truncated_date'] else '',
                    count=item['count'],
                    value=None
                )
                for item in time_series_data
            ]

            approval_trend = [
                types.TimeSeriesDataPoint(
                    timestamp=item['truncated_date'].isoformat(
                    ) if item['truncated_date'] else '',
                    count=item['count'],
                    value=None
                )
                for item in approval_trend_data
            ] if approval_trend_data else None

            rejection_trend = [
                types.TimeSeriesDataPoint(
                    timestamp=item['truncated_date'].isoformat(
                    ) if item['truncated_date'] else '',
                    count=item['count'],
                    value=None
                )
                for item in rejection_trend_data
            ] if rejection_trend_data else None

            jobs_assigned_trend = [
                types.TimeSeriesDataPoint(
                    timestamp=item['truncated_date'].isoformat(
                    ) if item['truncated_date'] else '',
                    count=item['count'],
                    value=None
                )
                for item in jobs_assigned_trend_data
            ] if jobs_assigned_trend_data else None

            return types.RequestTimeSeries(
                data_points=data_points,
                group_by=group_by.value,
                total_count=total_count,
                approval_trend=approval_trend,
                rejection_trend=rejection_trend,
                jobs_assigned_trend=jobs_assigned_trend,
            )

        # Use cache with filter-based key
        return await service.get_cached_or_execute(
            'request_time_series',
            tenant.id,
            _execute_query,
            filters=filters,
            group_by=group_by.value
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_detail(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
        filters: inputs.DashboardFiltersInput | None = None,
    ) -> types.EventDetail | None:
        """Get detailed information about a specific event."""
        service = DashboardQueriesService()
        tenant = await service.get_user_tenant(
            info,
            tenant_id=filters.tenant_id if filters else None,
        )

        # Try to get from cache first
        async def _execute_query():
            try:
                from django.db.models import Prefetch

                # Prefetch ambassador events with related data
                ambassador_events_prefetch = Prefetch(
                    'ambassadors_events',
                    queryset=ambassador_models.AmbassadorEvent.objects.select_related(
                        'ambassador', 'ambassador__user'
                    )
                )

                # Prefetch jobs with ambassador job counts
                jobs_prefetch = Prefetch(
                    'jobs',
                    queryset=job_models.Job.objects.prefetch_related(
                        Prefetch(
                            'ambassador_jobs',
                            queryset=job_models.AmbassadorJob.objects.select_related(
                                'ambassador', 'ambassador__user'
                            )
                        )
                    )
                )

                # Single query with all related data prefetched and annotated for performance
                event = await sync_to_async(
                    event_models.Event.objects.select_related(
                        'tenant', 'request', 'event_type', 'status',
                        'request__distributor__location', 'request__client',
                        'request__distributor', 'request__retailer'
                    ).prefetch_related(
                        ambassador_events_prefetch,
                        jobs_prefetch,
                    ).annotate(
                        # Annotate counts directly on the event
                        total_jobs_count=Count('jobs', distinct=False),
                        active_jobs_count=Count('jobs', filter=Q(
                            jobs__closed=False), distinct=False),
                    ).get
                )(id=id, tenant_id=tenant.id)

                # Get total unique ambassadors (union of both sources) - single query approach
                # Use a single query that gets all ambassador IDs from both sources
                ambassador_ids_from_events = await sync_to_async(
                    lambda: list(ambassador_models.AmbassadorEvent.objects.filter(
                        event_id=event.id
                    ).values_list('ambassador_id', flat=True).distinct())
                )()
                ambassador_ids_from_jobs = await sync_to_async(
                    lambda: list(job_models.AmbassadorJob.objects.filter(
                        job__event_id=event.id
                    ).values_list('ambassador_id', flat=True).distinct())
                )()
                total_ambassadors = len(
                    set(ambassador_ids_from_events) | set(ambassador_ids_from_jobs))

                # Get jobs count from annotation (already computed in main query)
                jobs_count = event.total_jobs_count
                active_jobs_count = event.active_jobs_count

                # Get all job counts per ambassador in a single query (no N+1)
                ambassador_job_counts = await sync_to_async(
                    lambda: dict(
                        job_models.AmbassadorJob.objects.filter(
                            job__event_id=event.id
                        ).values('ambassador_id')
                        .annotate(jobs_count=Count('id'))
                        .values_list('ambassador_id', 'jobs_count')
                    )
                )()

                # Get ambassador info efficiently (from prefetched data, no N+1 queries)
                ambassador_info_dict = {}

                # From AmbassadorEvent (prefetched)
                for ambassador_event in event.ambassadors_events.all():
                    ambassador = ambassador_event.ambassador
                    ambassador_id_str = str(ambassador.id)
                    jobs_count_for_amb = ambassador_job_counts.get(
                        ambassador.id, 0)

                    ambassador_info_dict[ambassador_id_str] = types.EventAmbassadorInfo(
                        ambassador_id=ambassador_id_str,
                        ambassador_name=f"{ambassador.user.first_name} {ambassador.user.last_name}".strip(
                        )
                        or ambassador.user.email,
                        is_approved=ambassador_event.is_approved,
                        jobs_count=jobs_count_for_amb,
                    )

                # From AmbassadorJob (if not already included, from prefetched data)
                for job in event.jobs.all():
                    for ambassador_job in job.ambassador_jobs.all():
                        ambassador_id_str = str(ambassador_job.ambassador.id)
                        if ambassador_id_str not in ambassador_info_dict:
                            ambassador = ambassador_job.ambassador
                            jobs_count_for_amb = ambassador_job_counts.get(
                                ambassador.id, 0)

                            ambassador_info_dict[ambassador_id_str] = types.EventAmbassadorInfo(
                                ambassador_id=ambassador_id_str,
                                ambassador_name=f"{ambassador.user.first_name} {ambassador.user.last_name}".strip(
                                )
                                or ambassador.user.email,
                                is_approved=True,  # If they have a job, consider approved
                                jobs_count=jobs_count_for_amb,
                            )

                ambassador_info_list = list(ambassador_info_dict.values())

                # Get location (strawberry_django handles conversion)
                location = None
                # Location is accessed via distributor or retailer
                location = None
                if event.request and event.request.distributor and event.request.distributor.location:
                    # Will be converted by strawberry_django
                    location = event.request.distributor.location

                # Statistics
                approved_ambassadors = sum(
                    1 for info in ambassador_info_list if info.is_approved)
                total_requests = 1 if event.request else 0

                statistics = types.EventDetailStatistics(
                    total_ambassadors=total_ambassadors,
                    approved_ambassadors=approved_ambassadors,
                    total_jobs=jobs_count,
                    active_jobs=active_jobs_count,
                    total_requests=total_requests,
                )

                # strawberry_django automatically converts Django instances to GraphQL types
                # when returned from a field that expects the GraphQL type
                from events.types import Event as EventGraphQLType, Location as LocationGraphQLType

                return types.EventDetail(
                    event=event,  # strawberry_django will convert automatically
                    related_request_id=str(
                        event.request.id) if event.request else None,
                    ambassadors_count=total_ambassadors,
                    jobs_count=jobs_count,
                    ambassadors=ambassador_info_list if ambassador_info_list else None,
                    # strawberry_django will convert automatically
                    location=location if location else None,
                    statistics=statistics,
                )

            except event_models.Event.DoesNotExist:
                return None

        # Use cache with filter-based key (include event id in cache key)
        # For event_detail, we need to include the event id in the cache key
        cache_key = service._generate_cache_key(
            'event_detail',
            tenant.id,
            filters,
            group_by=str(id)  # Use id as additional cache key component
        )
        ttl = service._get_cache_ttl('event_detail')

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
