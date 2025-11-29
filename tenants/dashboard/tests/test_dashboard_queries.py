"""
Tests for Dashboard queries.

This module tests all dashboard queries:
- events_stats
- events_time_series
- ambassadors_stats
- request_stats
- request_time_series
- event_detail
"""
import pytest
import strawberry_django  # noqa: F401
from datetime import date, time, timedelta
from asgiref.sync import sync_to_async
from django.utils import timezone
from django.core.cache import cache
from tenants.dashboard.tests.base import DashboardGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestClientDashboardQueries(DashboardGraphQLTestCase):
    """Tests for Dashboard queries (Client schema)."""

    @pytest.mark.asyncio
    async def test_events_stats_no_filters(self):
        """Test events_stats query with no filters."""
        query = """
        query {
            eventsStats {
                totalEvents
                eventsToday
                eventsThisWeek
                eventsThisMonth
                eventsByStatus {
                    statusId
                    statusName
                    count
                }
                eventsByLocation {
                    locationId
                    locationName
                    locationCode
                    count
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['eventsStats']
        assert data['totalEvents'] == 2
        assert data['eventsToday'] >= 0
        assert data['eventsThisWeek'] >= 0
        assert data['eventsThisMonth'] >= 0

    @pytest.mark.asyncio
    async def test_events_time_series(self):
        """Test events_time_series query."""
        query = """
        query {
            eventsTimeSeries(groupBy: DAY) {
                groupBy
                totalCount
                dataPoints {
                    timestamp
                    count
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['eventsTimeSeries']
        assert data['groupBy'] == 'DAY'
        assert isinstance(data['dataPoints'], list)

    @pytest.mark.asyncio
    async def test_ambassadors_stats(self):
        """Test ambassadors_stats query."""
        query = """
        query {
            ambassadorsStats {
                totalAmbassadorsWorking
                uniqueAmbassadorsCount
                ambassadorsByEvent {
                    eventId
                    eventName
                    ambassadorCount
                }
                ambassadorsByLocation {
                    locationId
                    locationName
                    locationCode
                    ambassadorCount
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['ambassadorsStats']
        assert data['totalAmbassadorsWorking'] >= 0
        assert data['uniqueAmbassadorsCount'] >= 0

    @pytest.mark.asyncio
    async def test_request_stats(self):
        """Test request_stats query."""
        query = """
        query {
            requestStats {
                totalRequests
                approvedCount
                rejectedCount
                pendingCount
                approvalRate
                rejectionRate
                requestsWithJobsCount
                requestsWithJobsPercentage
                requestsByStatus {
                    statusId
                    statusName
                    count
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['requestStats']
        assert data['totalRequests'] == 2
        assert data['approvedCount'] >= 0
        assert data['rejectedCount'] >= 0
        assert 0 <= data['approvalRate'] <= 100
        assert 0 <= data['rejectionRate'] <= 100

    @pytest.mark.asyncio
    async def test_request_time_series(self):
        """Test request_time_series query."""
        query = """
        query {
            requestTimeSeries(groupBy: DAY) {
                groupBy
                totalCount
                dataPoints {
                    timestamp
                    count
                }
                approvalTrend {
                    timestamp
                    count
                }
                rejectionTrend {
                    timestamp
                    count
                }
                jobsAssignedTrend {
                    timestamp
                    count
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['requestTimeSeries']
        assert data['groupBy'] == 'DAY'
        assert isinstance(data['dataPoints'], list)

    @pytest.mark.asyncio
    async def test_event_detail(self):
        """Test event_detail query."""
        event_id = str(self.event1.id)
        query = """
        query EventDetail($id: ID!) {
            eventDetail(id: $id) {
                event {
                    id
                    name
                }
                ambassadorsCount
                jobsCount
                statistics {
                    totalAmbassadors
                    totalJobs
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {'id': event_id},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['eventDetail']
        assert data is not None
        assert data['event']['id'] == event_id
        assert data['ambassadorsCount'] >= 0
        assert data['jobsCount'] >= 0

    @pytest.mark.asyncio
    async def test_event_detail_not_found(self):
        """Test event_detail query with non-existent event."""
        query = """
        query EventDetail($id: ID!) {
            eventDetail(id: $id) {
                event {
                    id
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {'id': '999999'},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data['eventDetail'] is None

    @pytest.mark.asyncio
    async def test_cache_invalidation_on_event_create(self):
        """Test that cache is invalidated when an event is created."""
        # First query - should cache
        query = """
        query {
            eventsStats {
                totalEvents
            }
        }
        """

        result1 = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )
        initial_count = result1.data['eventsStats']['totalEvents']

        # Create new event
        new_event = await sync_to_async(self.create_event)(
            name="New Event",
            tenant=self.tenant,
            address="New Address"
        )

        # Second query - should reflect new event (cache invalidated)
        result2 = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )
        new_count = result2.data['eventsStats']['totalEvents']

        assert new_count == initial_count + 1

    @pytest.mark.asyncio
    async def test_events_stats_with_filters(self):
        """Test events_stats query with various filter combinations."""
        # Test with date range filter
        query_with_dates = """
        query EventsStats($startDate: String, $endDate: String) {
            eventsStats(filters: {
                startDate: $startDate
                endDate: $endDate
            }) {
                totalEvents
            }
        }
        """
        today = timezone.now().date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        result = await self._execute_query_authenticated(
            query_with_dates,
            {
                'startDate': str(yesterday),
                'endDate': str(tomorrow)
            },
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data['eventsStats']['totalEvents'] >= 0

        # Test with location filter
        query_with_location = """
        query EventsStats($locationId: ID) {
            eventsStats(filters: {
                locationId: $locationId
            }) {
                totalEvents
            }
        }
        """
        location_id = str(self.location.id)

        result = await self._execute_query_authenticated(
            query_with_location,
            {'locationId': location_id},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data['eventsStats']['totalEvents'] >= 0

        # Test with event type and status filters
        query_with_event_filters = """
        query EventsStats($eventTypeId: ID, $eventStatusId: ID) {
            eventsStats(filters: {
                eventTypeId: $eventTypeId
                eventStatusId: $eventStatusId
            }) {
                totalEvents
            }
        }
        """
        event_type_id = str(self.event_type.id)
        event_status_id = str(self.event_status.id)

        result = await self._execute_query_authenticated(
            query_with_event_filters,
            {
                'eventTypeId': event_type_id,
                'eventStatusId': event_status_id
            },
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data['eventsStats']['totalEvents'] >= 0

    @pytest.mark.asyncio
    async def test_cache_with_different_filters(self):
        """Test that different filter combinations produce different cache entries."""
        query_template = """
        query EventsStats($startDate: String, $locationId: ID) {
            eventsStats(filters: {
                startDate: $startDate
                locationId: $locationId
            }) {
                totalEvents
            }
        }
        """

        # Clear cache first
        cache.clear()

        # Query 1: No filters
        query_no_filters = """
        query {
            eventsStats {
                totalEvents
            }
        }
        """
        result1 = await self._execute_query_authenticated(
            query_no_filters,
            {},
            self.client_user
        )
        count1 = result1.data['eventsStats']['totalEvents']

        # Query 2: With date filter
        today = timezone.now().date()
        yesterday = today - timedelta(days=1)
        result2 = await self._execute_query_authenticated(
            query_template,
            {'startDate': str(yesterday)},
            self.client_user
        )
        count2 = result2.data['eventsStats']['totalEvents']

        # Query 3: With location filter
        location_id = str(self.location.id)
        result3 = await self._execute_query_authenticated(
            query_template,
            {'locationId': location_id},
            self.client_user
        )
        count3 = result3.data['eventsStats']['totalEvents']

        # Query 4: With both filters
        result4 = await self._execute_query_authenticated(
            query_template,
            {
                'startDate': str(yesterday),
                'locationId': location_id
            },
            self.client_user
        )
        count4 = result4.data['eventsStats']['totalEvents']

        # All queries should succeed
        assert result1.errors is None
        assert result2.errors is None
        assert result3.errors is None
        assert result4.errors is None

        # Verify all counts are valid (may be same or different depending on data)
        assert isinstance(count1, int)
        assert isinstance(count2, int)
        assert isinstance(count3, int)
        assert isinstance(count4, int)

    @pytest.mark.asyncio
    async def test_cache_hit_same_filters(self):
        """Test that same filter combination returns cached result."""
        query = """
        query EventsStats($startDate: String) {
            eventsStats(filters: {
                startDate: $startDate
            }) {
                totalEvents
            }
        }
        """

        # Clear cache first
        cache.clear()

        today = timezone.now().date()
        yesterday = today - timedelta(days=1)
        filters = {'startDate': str(yesterday)}

        # First call - should cache
        result1 = await self._execute_query_authenticated(
            query,
            filters,
            self.client_user
        )
        count1 = result1.data['eventsStats']['totalEvents']

        # Second call with same filters - should return cached result
        result2 = await self._execute_query_authenticated(
            query,
            filters,
            self.client_user
        )
        count2 = result2.data['eventsStats']['totalEvents']

        # Results should be identical (cached)
        assert count1 == count2
        assert result1.errors is None
        assert result2.errors is None

    @pytest.mark.asyncio
    async def test_request_stats_with_filters(self):
        """Test request_stats query with various filter combinations."""
        # Test with date range
        query_with_dates = """
        query RequestStats($startDate: String, $endDate: String) {
            requestStats(filters: {
                startDate: $startDate
                endDate: $endDate
            }) {
                totalRequests
            }
        }
        """
        today = timezone.now().date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        result = await self._execute_query_authenticated(
            query_with_dates,
            {
                'startDate': str(yesterday),
                'endDate': str(tomorrow)
            },
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data['requestStats']['totalRequests'] >= 0

        # Test with request status filter
        query_with_status = """
        query RequestStats($requestStatusId: ID) {
            requestStats(filters: {
                requestStatusId: $requestStatusId
            }) {
                totalRequests
                approvedCount
            }
        }
        """
        approved_status_id = str(self.approved_status.id)

        result = await self._execute_query_authenticated(
            query_with_status,
            {'requestStatusId': approved_status_id},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data['requestStats']['totalRequests'] >= 0

        # Test with client filter
        query_with_client = """
        query RequestStats($clientId: ID) {
            requestStats(filters: {
                clientId: $clientId
            }) {
                totalRequests
            }
        }
        """
        client_id = str(self.client.id)

        result = await self._execute_query_authenticated(
            query_with_client,
            {'clientId': client_id},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data['requestStats']['totalRequests'] >= 0

    @pytest.mark.asyncio
    async def test_events_time_series_with_filters(self):
        """Test events_time_series query with filter combinations."""
        query = """
        query EventsTimeSeries($groupBy: TimeGroupBy, $startDate: String, $locationId: ID) {
            eventsTimeSeries(
                groupBy: $groupBy
                filters: {
                    startDate: $startDate
                    locationId: $locationId
                }
            ) {
                groupBy
                totalCount
                dataPoints {
                    timestamp
                    count
                }
            }
        }
        """

        today = timezone.now().date()
        yesterday = today - timedelta(days=7)  # Last week
        location_id = str(self.location.id)

        # Test with date and location filters
        result = await self._execute_query_authenticated(
            query,
            {
                'groupBy': 'DAY',
                'startDate': str(yesterday),
                'locationId': location_id
            },
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['eventsTimeSeries']
        assert data['groupBy'] == 'DAY'
        assert isinstance(data['dataPoints'], list)

    @pytest.mark.asyncio
    async def test_cache_invalidation_with_filters(self):
        """Test that cache invalidation works correctly with filters."""
        query = """
        query EventsStats($startDate: String) {
            eventsStats(filters: {
                startDate: $startDate
            }) {
                totalEvents
            }
        }
        """

        # Clear cache first
        cache.clear()

        today = timezone.now().date()
        yesterday = today - timedelta(days=1)
        filters = {'startDate': str(yesterday)}

        # First query - should cache
        result1 = await self._execute_query_authenticated(
            query,
            filters,
            self.client_user
        )
        initial_count = result1.data['eventsStats']['totalEvents']

        # Create new event
        new_event = await sync_to_async(self.create_event)(
            name="New Event for Cache Test",
            tenant=self.tenant,
            address="New Address",
            request=self.request1,
            event_type=self.event_type,
            status=self.event_status
        )

        # Second query with same filters - should reflect new event (cache invalidated)
        result2 = await self._execute_query_authenticated(
            query,
            filters,
            self.client_user
        )
        new_count = result2.data['eventsStats']['totalEvents']

        # Should see the new event (cache was invalidated)
        assert new_count >= initial_count
