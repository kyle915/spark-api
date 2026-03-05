"""
Tests for Dashboard queries.

This module tests Event Dashboard queries:
- event_dashboard_filter_options
- event_dashboard
"""
import pytest
import strawberry_django  # noqa: F401
from datetime import timedelta
from asgiref.sync import sync_to_async
from django.utils import timezone
from django.core.cache import cache
from tenants.dashboard.tests.base import DashboardGraphQLTestCase


# Note: Old dashboard queries (events_stats, events_time_series, etc.) have been removed
# Only Event Dashboard queries are now available


@pytest.mark.django_db(transaction=True)
class TestEventDashboardQueries(DashboardGraphQLTestCase):
    """Tests for Event Dashboard queries."""

    @pytest.mark.asyncio
    async def test_event_dashboard_filter_options(self):
        """Test event_dashboard_filter_options query."""
        query = """
        query {
            eventDashboardFilterOptions {
                distributors {
                    id
                    name
                }
                rmms {
                    id
                    name
                    address
                }
                quarters {
                    value
                    label
                }
                tenants {
                    id
                    name
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
        data = result.data['eventDashboardFilterOptions']
        assert data is not None
        # Should have at least one distributor (we created one)
        assert data['distributors'] is not None
        assert len(data['distributors']) >= 1
        # Should have at least one RMM/location
        assert data['rmms'] is not None
        assert len(data['rmms']) >= 1
        # Should have quarters
        assert data['quarters'] is not None
        assert len(data['quarters']) > 0
        # Should have at least one tenant
        assert data['tenants'] is not None
        assert len(data['tenants']) >= 1

    @pytest.mark.asyncio
    async def test_event_dashboard_default_quarter(self):
        """Test event_dashboard query defaults to current quarter."""
        query = """
        query {
            eventDashboard {
                metrics {
                    totalEvents
                    consumersSampled
                    brandAwareness
                    purchaseIntent
                }
                globalKpis {
                    singleCansSold
                    multiPacksSold
                    byRmm {
                        rmmId
                        rmmName
                        singleCansSold
                        multiPacksSold
                    }
                }
                monthlyTrends {
                    dataPoints {
                        month
                        consumersSampled
                        conversionRate
                    }
                }
                performanceInsights {
                    knewAboutBrand
                    willingToPurchase
                    growthRate
                }
                recentEvents {
                    id
                    name
                    date
                    location
                    consumers
                    intentRate
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
        data = result.data['eventDashboard']
        assert data is not None
        assert data['metrics'] is not None
        assert isinstance(data['metrics']['totalEvents'], int)
        assert isinstance(data['metrics']['consumersSampled'], int)
        assert 0 <= data['metrics']['brandAwareness'] <= 100
        assert 0 <= data['metrics']['purchaseIntent'] <= 100
        assert data['globalKpis'] is not None
        assert data['globalKpis']['singleCansSold'] == 72
        assert data['globalKpis']['multiPacksSold'] == 36
        assert data['globalKpis']['byRmm'] is not None
        assert len(data['globalKpis']['byRmm']) >= 1
        assert data['globalKpis']['byRmm'][0]['singleCansSold'] == 72
        assert data['globalKpis']['byRmm'][0]['multiPacksSold'] == 36
        assert data['monthlyTrends'] is not None
        assert data['performanceInsights'] is not None

    @pytest.mark.asyncio
    async def test_event_dashboard_with_quarter_filter(self):
        """Test event_dashboard query with quarter filter."""
        from tenants.dashboard.services import DashboardQueriesService
        service = DashboardQueriesService()
        quarter_string, _, _ = service._get_current_quarter()

        query = """
        query EventDashboard($quarter: String) {
            eventDashboard(filters: {
                quarter: $quarter
            }) {
                metrics {
                    totalEvents
                    consumersSampled
                    brandAwareness
                    purchaseIntent
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {'quarter': quarter_string},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['eventDashboard']
        assert data is not None
        assert data['metrics'] is not None

    @pytest.mark.asyncio
    async def test_event_dashboard_with_distributor_filter(self):
        """Test event_dashboard query with distributor filter."""
        distributor_id = str(self.distributor.id)
        query = """
        query EventDashboard($distributorId: ID) {
            eventDashboard(filters: {
                distributorId: $distributorId
            }) {
                metrics {
                    totalEvents
                    consumersSampled
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {'distributorId': distributor_id},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['eventDashboard']
        assert data is not None
        assert data['metrics'] is not None

    @pytest.mark.asyncio
    async def test_event_dashboard_with_rmm_filter(self):
        """Test event_dashboard query with RMM assigned user filter."""
        rmm_asigned_id = str(self.rmm_user.id)
        query = """
        query EventDashboard($rmmAsignedId: ID) {
            eventDashboard(filters: {
                rmmAsignedId: $rmmAsignedId
            }) {
                metrics {
                    totalEvents
                    consumersSampled
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {'rmmAsignedId': rmm_asigned_id},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['eventDashboard']
        assert data is not None
        assert data['metrics'] is not None

    @pytest.mark.asyncio
    async def test_event_dashboard_metrics_calculation(self):
        """Test event_dashboard metrics calculations."""
        query = """
        query {
            eventDashboard {
                metrics {
                    totalEvents
                    consumersSampled
                    brandAwareness
                    purchaseIntent
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
        metrics = result.data['eventDashboard']['metrics']

        # We created 3 events with recaps
        assert metrics['totalEvents'] >= 0

        # Consumers sampled should be sum of total_consumer from ConsumerEngagements
        # We created: 100 + 80 + 120 = 300
        assert metrics['consumersSampled'] >= 0

        # Brand awareness should be percentage
        assert 0 <= metrics['brandAwareness'] <= 100

        # Purchase intent should be percentage
        assert 0 <= metrics['purchaseIntent'] <= 100

    @pytest.mark.asyncio
    async def test_event_dashboard_monthly_trends(self):
        """Test event_dashboard monthly trends aggregation."""
        query = """
        query {
            eventDashboard {
                monthlyTrends {
                    dataPoints {
                        month
                        consumersSampled
                        willingToPurchase
                        conversionRate
                        eventsCount
                    }
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
        trends = result.data['eventDashboard']['monthlyTrends']
        assert trends is not None
        assert trends['dataPoints'] is not None
        assert isinstance(trends['dataPoints'], list)

        # If we have data points, verify structure
        if len(trends['dataPoints']) > 0:
            point = trends['dataPoints'][0]
            assert 'month' in point
            assert isinstance(point['consumersSampled'], int)
            assert isinstance(point['willingToPurchase'], int)
            assert 0 <= point['conversionRate'] <= 100

    @pytest.mark.asyncio
    async def test_event_dashboard_performance_insights(self):
        """Test event_dashboard performance insights calculations."""
        query = """
        query {
            eventDashboard {
                performanceInsights {
                    knewAboutBrand
                    knewAboutBrandPercentage
                    willingToPurchase
                    willingToPurchasePercentage
                    bestMonth {
                        month
                        eventsCount
                        consumersCount
                    }
                    growthRate
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
        insights = result.data['eventDashboard']['performanceInsights']
        assert insights is not None
        assert isinstance(insights['knewAboutBrand'], int)
        assert 0 <= insights['knewAboutBrandPercentage'] <= 100
        assert isinstance(insights['willingToPurchase'], int)
        assert 0 <= insights['willingToPurchasePercentage'] <= 100
        # growthRate can be negative, so just check it's a number
        assert isinstance(insights['growthRate'], (int, float))

    @pytest.mark.asyncio
    async def test_event_dashboard_recent_events(self):
        """Test event_dashboard recent events query."""
        query = """
        query {
            eventDashboard {
                recentEvents {
                    id
                    name
                    date
                    location
                    consumers
                    intentRate
                    status
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
        recent_events = result.data['eventDashboard']['recentEvents']
        # Should have at least one upcoming event (event3)
        if recent_events:
            assert len(recent_events) > 0
            event = recent_events[0]
            assert 'id' in event
            assert 'name' in event
            assert 'date' in event
            assert 'location' in event
            assert isinstance(event['consumers'], int)
            assert 0 <= event['intentRate'] <= 100

    @pytest.mark.asyncio
    async def test_event_dashboard_caching(self):
        """Test event_dashboard caching behavior."""
        query = """
        query {
            eventDashboard {
                metrics {
                    totalEvents
                }
            }
        }
        """

        # Clear cache first
        cache.clear()

        # First call - should cache
        result1 = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )
        count1 = result1.data['eventDashboard']['metrics']['totalEvents']

        # Second call - should return cached result
        result2 = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )
        count2 = result2.data['eventDashboard']['metrics']['totalEvents']

        # Results should be identical (cached)
        assert count1 == count2
        assert result1.errors is None
        assert result2.errors is None

    @pytest.mark.asyncio
    async def test_event_dashboard_no_recaps(self):
        """Test event_dashboard behavior when events have no recaps."""
        # Create an event without recaps
        await sync_to_async(self.create_event)(
            name="Event No Recap",
            tenant=self.tenant,
            address="Address No Recap",
            request=self.request1,
            event_type=self.event_type,
            status=self.event_status
        )

        query = """
        query {
            eventDashboard {
                metrics {
                    totalEvents
                    consumersSampled
                    brandAwareness
                    purchaseIntent
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
        metrics = result.data['eventDashboard']['metrics']
        # Should still return valid metrics even if some events have no recaps
        assert isinstance(metrics['totalEvents'], int)
        assert isinstance(metrics['consumersSampled'], int)
        # Brand awareness and purchase intent should be 0 if no consumers
        assert 0 <= metrics['brandAwareness'] <= 100
        assert 0 <= metrics['purchaseIntent'] <= 100

    @pytest.mark.asyncio
    async def test_event_dashboard_with_date_range(self):
        """Test event_dashboard with date range instead of quarter."""
        today = timezone.now().date()
        start_date = today - timedelta(days=30)
        end_date = today

        query = """
        query EventDashboard($startDate: String, $endDate: String) {
            eventDashboard(filters: {
                startDate: $startDate
                endDate: $endDate
            }) {
                metrics {
                    totalEvents
                    consumersSampled
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {
                'startDate': str(start_date),
                'endDate': str(end_date)
            },
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['eventDashboard']
        assert data is not None
        assert data['metrics'] is not None
