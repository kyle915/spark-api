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
