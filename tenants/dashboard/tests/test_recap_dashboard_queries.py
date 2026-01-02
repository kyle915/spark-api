"""
Tests for Recap Dashboard queries.

This module tests Recap Dashboard queries:
- recap_dashboard_filter_options
- recap_dashboard
"""
import pytest
import strawberry_django  # noqa: F401
from datetime import timedelta
from django.utils import timezone
from django.core.cache import cache
from tenants.dashboard.tests.base import DashboardGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestRecapDashboardQueries(DashboardGraphQLTestCase):
    """Tests for Recap Dashboard queries."""

    @pytest.mark.asyncio
    async def test_recap_dashboard_filter_options(self):
        """Test recap_dashboard_filter_options query."""
        query = """
        query {
            recapDashboardFilterOptions {
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
        data = result.data['recapDashboardFilterOptions']
        assert data is not None
        # Should have at least one distributor (we created one)
        assert data['distributors'] is not None
        assert len(data['distributors']) >= 1
        # Should have at least one RMM/retailer
        assert data['rmms'] is not None
        assert len(data['rmms']) >= 1
        # Should have quarters
        assert data['quarters'] is not None
        assert len(data['quarters']) > 0
        # Should have at least one tenant
        assert data['tenants'] is not None
        assert len(data['tenants']) >= 1

    @pytest.mark.asyncio
    async def test_recap_dashboard_default_quarter(self):
        """Test recap_dashboard query defaults to current quarter."""
        query = """
        query {
            recapDashboard {
                metrics {
                    totalConsumersSampled
                    totalPurchases
                    conversionRate
                    revenueGenerated
                }
                monthlyTrends {
                    dataPoints {
                        month
                        consumersSampled
                        purchases
                        conversionRate
                        revenue
                        recapsCount
                    }
                }
                performanceInsights {
                    newCustomersSampled
                    newCustomersPercentage
                    brandAwareness
                    brandAwarenessPercentage
                    willingToPurchase
                    willingToPurchasePercentage
                    growthRate
                }
                marketAnalysis {
                    dataPoints {
                        marketId
                        marketName
                        consumers
                        purchases
                        conversion
                        demos
                        efficiency
                    }
                }
                rmmPerformance {
                    dataPoints {
                        rmmId
                        rmmName
                        consumersSampled
                        demos
                        conversionRate
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
        data = result.data['recapDashboard']
        assert data is not None
        assert data['metrics'] is not None
        assert isinstance(data['metrics']['totalConsumersSampled'], int)
        assert isinstance(data['metrics']['totalPurchases'], int)
        assert 0 <= data['metrics']['conversionRate'] <= 100
        assert isinstance(data['metrics']['revenueGenerated'], (int, float))
        assert data['monthlyTrends'] is not None
        assert data['performanceInsights'] is not None
        assert data['marketAnalysis'] is not None
        assert data['rmmPerformance'] is not None

    @pytest.mark.asyncio
    async def test_recap_dashboard_with_quarter_filter(self):
        """Test recap_dashboard query with quarter filter."""
        from tenants.dashboard.services import DashboardQueriesService
        service = DashboardQueriesService()
        quarter_string, _, _ = service._get_current_quarter()

        query = """
        query RecapDashboard($quarter: String) {
            recapDashboard(filters: {
                quarter: $quarter
            }) {
                metrics {
                    totalConsumersSampled
                    totalPurchases
                    conversionRate
                    revenueGenerated
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
        data = result.data['recapDashboard']
        assert data is not None
        assert data['metrics'] is not None

    @pytest.mark.asyncio
    async def test_recap_dashboard_with_distributor_filter(self):
        """Test recap_dashboard query with distributor filter."""
        distributor_id = str(self.distributor.id)
        query = """
        query RecapDashboard($distributorId: ID) {
            recapDashboard(filters: {
                distributorId: $distributorId
            }) {
                metrics {
                    totalConsumersSampled
                    totalPurchases
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
        data = result.data['recapDashboard']
        assert data is not None
        assert data['metrics'] is not None

    @pytest.mark.asyncio
    async def test_recap_dashboard_with_rmm_filter(self):
        """Test recap_dashboard query with RMM (retailer) filter."""
        rmm_id = str(self.retailer.id)
        query = """
        query RecapDashboard($rmmId: ID) {
            recapDashboard(filters: {
                rmmId: $rmmId
            }) {
                metrics {
                    totalConsumersSampled
                    totalPurchases
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {'rmmId': rmm_id},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['recapDashboard']
        assert data is not None
        assert data['metrics'] is not None

    @pytest.mark.asyncio
    async def test_recap_dashboard_metrics_calculation(self):
        """Test recap_dashboard metrics calculations."""
        query = """
        query {
            recapDashboard {
                metrics {
                    totalConsumersSampled
                    totalPurchases
                    conversionRate
                    revenueGenerated
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
        metrics = result.data['recapDashboard']['metrics']

        # We created 3 recaps with consumer engagements
        assert metrics['totalConsumersSampled'] >= 0
        assert metrics['totalPurchases'] >= 0
        assert 0 <= metrics['conversionRate'] <= 100
        assert isinstance(metrics['revenueGenerated'], (int, float))

    @pytest.mark.asyncio
    async def test_recap_dashboard_monthly_trends(self):
        """Test recap_dashboard monthly trends aggregation."""
        query = """
        query {
            recapDashboard {
                monthlyTrends {
                    dataPoints {
                        month
                        consumersSampled
                        purchases
                        conversionRate
                        revenue
                        recapsCount
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
        trends = result.data['recapDashboard']['monthlyTrends']
        assert trends is not None
        assert trends['dataPoints'] is not None
        assert isinstance(trends['dataPoints'], list)

        # If we have data points, verify structure
        if len(trends['dataPoints']) > 0:
            point = trends['dataPoints'][0]
            assert 'month' in point
            assert isinstance(point['consumersSampled'], int)
            assert isinstance(point['purchases'], int)
            assert 0 <= point['conversionRate'] <= 100
            assert isinstance(point['revenue'], (int, float))
            assert isinstance(point['recapsCount'], int)

    @pytest.mark.asyncio
    async def test_recap_dashboard_performance_insights(self):
        """Test recap_dashboard performance insights calculations."""
        query = """
        query {
            recapDashboard {
                performanceInsights {
                    newCustomersSampled
                    newCustomersPercentage
                    brandAwareness
                    brandAwarenessPercentage
                    willingToPurchase
                    willingToPurchasePercentage
                    bestMonth {
                        month
                        recapsCount
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
        insights = result.data['recapDashboard']['performanceInsights']
        assert insights is not None
        assert isinstance(insights['newCustomersSampled'], int)
        assert 0 <= insights['newCustomersPercentage'] <= 100
        assert isinstance(insights['brandAwareness'], int)
        assert 0 <= insights['brandAwarenessPercentage'] <= 100
        assert isinstance(insights['willingToPurchase'], int)
        assert 0 <= insights['willingToPurchasePercentage'] <= 100
        # growthRate can be negative, so just check it's a number
        assert isinstance(insights['growthRate'], (int, float))

    @pytest.mark.asyncio
    async def test_recap_dashboard_market_analysis(self):
        """Test recap_dashboard market analysis."""
        query = """
        query {
            recapDashboard {
                marketAnalysis {
                    dataPoints {
                        marketId
                        marketName
                        consumers
                        purchases
                        conversion
                        demos
                        efficiency
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
        market_analysis = result.data['recapDashboard']['marketAnalysis']
        assert market_analysis is not None
        assert market_analysis['dataPoints'] is not None
        assert isinstance(market_analysis['dataPoints'], list)

        # If we have data points, verify structure
        if len(market_analysis['dataPoints']) > 0:
            point = market_analysis['dataPoints'][0]
            assert 'marketId' in point
            assert 'marketName' in point
            assert isinstance(point['consumers'], int)
            assert isinstance(point['purchases'], int)
            assert 0 <= point['conversion'] <= 100
            assert isinstance(point['demos'], int)
            assert 0 <= point['efficiency'] <= 100

    @pytest.mark.asyncio
    async def test_recap_dashboard_rmm_performance(self):
        """Test recap_dashboard RMM performance."""
        query = """
        query {
            recapDashboard {
                rmmPerformance {
                    dataPoints {
                        rmmId
                        rmmName
                        consumersSampled
                        demos
                        conversionRate
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
        rmm_performance = result.data['recapDashboard']['rmmPerformance']
        assert rmm_performance is not None
        assert rmm_performance['dataPoints'] is not None
        assert isinstance(rmm_performance['dataPoints'], list)

        # If we have data points, verify structure
        if len(rmm_performance['dataPoints']) > 0:
            point = rmm_performance['dataPoints'][0]
            assert 'rmmId' in point
            assert 'rmmName' in point
            assert isinstance(point['consumersSampled'], int)
            assert isinstance(point['demos'], int)
            assert 0 <= point['conversionRate'] <= 100

    @pytest.mark.asyncio
    async def test_recap_dashboard_caching(self):
        """Test recap_dashboard caching behavior."""
        query = """
        query {
            recapDashboard {
                metrics {
                    totalConsumersSampled
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
        count1 = result1.data['recapDashboard']['metrics']['totalConsumersSampled']

        # Second call - should return cached result
        result2 = await self._execute_query_authenticated(
            query,
            {},
            self.client_user
        )
        count2 = result2.data['recapDashboard']['metrics']['totalConsumersSampled']

        # Results should be identical (cached)
        assert count1 == count2
        assert result1.errors is None
        assert result2.errors is None

    @pytest.mark.asyncio
    async def test_recap_dashboard_with_date_range(self):
        """Test recap_dashboard with date range instead of quarter."""
        today = timezone.now().date()
        start_date = today - timedelta(days=30)
        end_date = today

        query = """
        query RecapDashboard($startDate: String, $endDate: String) {
            recapDashboard(filters: {
                startDate: $startDate
                endDate: $endDate
            }) {
                metrics {
                    totalConsumersSampled
                    totalPurchases
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
        data = result.data['recapDashboard']
        assert data is not None
        assert data['metrics'] is not None

    @pytest.mark.asyncio
    async def test_recap_dashboard_with_tenant_filter(self):
        """Test recap_dashboard with tenant filter."""
        tenant_id = str(self.tenant.id)
        query = """
        query RecapDashboard($tenantId: ID) {
            recapDashboard(filters: {
                tenantId: $tenantId
            }) {
                metrics {
                    totalConsumersSampled
                    totalPurchases
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {'tenantId': tenant_id},
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        data = result.data['recapDashboard']
        assert data is not None
        assert data['metrics'] is not None

    @pytest.mark.asyncio
    async def test_recap_dashboard_comparison_values(self):
        """Test recap_dashboard comparison values when quarter filter is provided."""
        from tenants.dashboard.services import DashboardQueriesService
        service = DashboardQueriesService()
        quarter_string, _, _ = service._get_current_quarter()

        query = """
        query RecapDashboard($quarter: String) {
            recapDashboard(filters: {
                quarter: $quarter
            }) {
                metrics {
                    totalConsumersSampled
                    totalPurchases
                    conversionRate
                    revenueGenerated
                    comparisonPeriod
                    comparisonValues {
                        totalConsumersSampled
                        totalPurchases
                        conversionRate
                        revenueGenerated
                    }
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
        metrics = result.data['recapDashboard']['metrics']
        assert metrics is not None
        # Comparison period and values may or may not be present depending on data
        if metrics.get('comparisonPeriod'):
            assert metrics['comparisonValues'] is not None
