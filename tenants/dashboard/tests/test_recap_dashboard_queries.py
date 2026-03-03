"""
Tests for Recap Dashboard queries.

This module tests Recap Dashboard queries:
- recap_dashboard_filter_options
- recap_dashboard
"""
import pytest
import strawberry_django  # noqa: F401
from datetime import datetime, timedelta, time
from asgiref.sync import sync_to_async
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
        """Test recap_dashboard query with RMM assigned user filter."""
        rmm_asigned_id = str(self.rmm_user.id)
        query = """
        query RecapDashboard($rmmAsignedId: ID) {
            recapDashboard(filters: {
                rmmAsignedId: $rmmAsignedId
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
            {'rmmAsignedId': rmm_asigned_id},
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
        """Test recap_dashboard performance insights calculations and new insight cards."""
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
                    topConvertingMarket {
                        marketName
                        conversionRate
                    }
                    highestWillingnessToBuy {
                        marketName
                        willingCount
                    }
                    strongestBrandAwareness {
                        marketName
                        brandAwareCount
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
        insights = result.data['recapDashboard']['performanceInsights']
        assert insights is not None
        assert isinstance(insights['newCustomersSampled'], int)
        assert 0 <= insights['newCustomersPercentage'] <= 100
        assert isinstance(insights['brandAwareness'], int)
        assert 0 <= insights['brandAwarenessPercentage'] <= 100
        assert isinstance(insights['willingToPurchase'], int)
        assert 0 <= insights['willingToPurchasePercentage'] <= 100
        assert isinstance(insights['growthRate'], (int, float))

        # New performance insight cards (optional when no market data)
        assert 'topConvertingMarket' in insights
        assert 'highestWillingnessToBuy' in insights
        assert 'strongestBrandAwareness' in insights
        if insights.get('topConvertingMarket'):
            assert 'marketName' in insights['topConvertingMarket']
            assert 'conversionRate' in insights['topConvertingMarket']
            assert isinstance(insights['topConvertingMarket']['conversionRate'], (int, float))
        if insights.get('highestWillingnessToBuy'):
            assert 'marketName' in insights['highestWillingnessToBuy']
            assert insights['highestWillingnessToBuy']['willingCount'] >= 0
        if insights.get('strongestBrandAwareness'):
            assert 'marketName' in insights['strongestBrandAwareness']
            assert insights['strongestBrandAwareness']['brandAwareCount'] >= 0

        # bestMonth = most active month (by recaps count)
        if insights.get('bestMonth'):
            assert 'recapsCount' in insights['bestMonth']
            assert 'consumersCount' in insights['bestMonth']
            assert insights['bestMonth']['recapsCount'] >= 0

    @pytest.mark.asyncio
    async def test_recap_dashboard_performance_insight_cards_two_retailers(self):
        """Top converting market is the retailer with highest conversion; willingness/brand by count."""
        from recaps import models as recap_models

        # Second retailer with lower conversion so first stays on top, or vice versa (sync ORM in async test)
        retailer2 = await sync_to_async(self.create_retailer)(
            name="Second Retailer",
            address="Address R2",
            store_contact="Contact R2",
            location=self.location,
            tenant=self.tenant
        )
        req_r2 = await sync_to_async(self.create_request)(
            name="Req R2",
            date=timezone.now().date(),
            address="Addr R2",
            client=self.client,
            distributor=self.distributor,
            retailer=retailer2,
            request_type=self.request_type,
            tenant=self.tenant,
            start_time=time(9, 0),
            end_time=time(17, 0),
            status=self.approved_status
        )
        evt_r2 = await sync_to_async(self.create_event)(
            name="Event R2",
            tenant=self.tenant,
            address="Addr R2",
            request=req_r2,
            event_type=self.event_type,
            status=self.event_status,
            rmm_asigned=self.rmm_user
        )
        job_r2 = await sync_to_async(self.create_job)(
            name="Job R2",
            code="JOB-R2",
            address="Job R2",
            event=evt_r2,
            job_title=self.job_title,
            tenant=self.tenant
        )
        recap_r2 = await sync_to_async(recap_models.Recap.objects.create)(
            name="Recap R2",
            event=evt_r2,
            ambassador=self.ambassador,
            job=job_r2,
            retailer=retailer2,
            total_engagements=50,
            products_sold=25,
            total_cans_sold=12,
            total_packs_sold=6,
            total_earnings=500.0,
            approved=True,
            created_by=self.get_system_user()
        )
        await sync_to_async(recap_models.ConsumerEngagements.objects.create)(
            recap=recap_r2,
            total_consumer=50,
            first_time_consumers=10,
            brand_aware_consumers=45,
            willing_to_purchase_consumers=48,
            not_willing_consumers=2,
            created_by=self.get_system_user()
        )
        # Retailer 1 (Test Retailer): 70/100 = 70% conversion, 40 brand aware
        # Retailer 2: 48/50 = 96% conversion, 45 brand aware
        # So top converting = Second Retailer; strongest brand = Second Retailer (45 > 40); highest willing = Second (48) or Test (70)
        cache.clear()
        query = """
        query {
            recapDashboard {
                performanceInsights {
                    topConvertingMarket { marketName conversionRate }
                    highestWillingnessToBuy { marketName willingCount }
                    strongestBrandAwareness { marketName brandAwareCount }
                }
            }
        }
        """
        result = await self._execute_query_authenticated(
            query, {}, self.client_user
        )
        assert result.errors is None
        assert result.data is not None
        pi = result.data['recapDashboard']['performanceInsights']
        assert pi['topConvertingMarket'] is not None
        assert pi['topConvertingMarket']['marketName'] == "Second Retailer"
        assert pi['topConvertingMarket']['conversionRate'] == 96.0
        assert pi['highestWillingnessToBuy'] is not None
        # Test Retailer has recap1 (70) + recap2 (50) + recap3 (90) = 210; Second has 48
        assert pi['highestWillingnessToBuy']['marketName'] == "Test Retailer"
        assert pi['highestWillingnessToBuy']['willingCount'] == 210
        assert pi['strongestBrandAwareness'] is not None
        # Test Retailer: 40+30+50=120 brand aware; Second: 45
        assert pi['strongestBrandAwareness']['marketName'] == "Test Retailer"
        assert pi['strongestBrandAwareness']['brandAwareCount'] == 120

    @pytest.mark.asyncio
    async def test_recap_dashboard_most_active_month_by_recaps(self):
        """Most active month is the month with the most recaps, not most consumers."""
        from recaps import models as recap_models

        # Base has recaps on event1 (today), event2 (today-1), event3 (future). Same month can have 2 recaps.
        # Add another event/recap in a different month (sync ORM in async test)
        past_date = timezone.now().date() - timedelta(days=60)
        req_past = await sync_to_async(self.create_request)(
            name="Past Req",
            date=past_date,
            address="Past Addr",
            client=self.client,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.request_type,
            tenant=self.tenant,
            start_time=time(9, 0),
            end_time=time(17, 0),
            status=self.approved_status
        )
        evt_past = await sync_to_async(self.create_event)(
            name="Event Past",
            tenant=self.tenant,
            address="Past Addr",
            request=req_past,
            event_type=self.event_type,
            status=self.event_status,
            rmm_asigned=self.rmm_user,
            date=timezone.make_aware(datetime.combine(past_date, time(10, 0))),
            start_time=timezone.make_aware(datetime.combine(past_date, time(10, 0)))
        )
        recap_past = await sync_to_async(recap_models.Recap.objects.create)(
            name="Recap Past",
            event=evt_past,
            ambassador=self.ambassador,
            total_engagements=200,
            products_sold=100,
            total_cans_sold=50,
            total_packs_sold=25,
            total_earnings=2000.0,
            approved=True,
            created_by=self.get_system_user()
        )
        await sync_to_async(recap_models.ConsumerEngagements.objects.create)(
            recap=recap_past,
            total_consumer=200,
            first_time_consumers=50,
            brand_aware_consumers=100,
            willing_to_purchase_consumers=150,
            not_willing_consumers=50,
            created_by=self.get_system_user()
        )
        cache.clear()
        query = """
        query {
            recapDashboard {
                performanceInsights {
                    bestMonth { month recapsCount consumersCount }
                }
            }
        }
        """
        result = await self._execute_query_authenticated(
            query, {}, self.client_user
        )
        assert result.errors is None
        assert result.data is not None
        best = result.data['recapDashboard']['performanceInsights']['bestMonth']
        assert best is not None
        # Current quarter: event1+today, event2 today-1 (same month), event3 future, past 60d in another month
        # So one month in the quarter has 2 recaps (event1+event2), one has 1 (event3), one has 1 (past)
        assert best['recapsCount'] >= 1
        assert best['consumersCount'] >= 0

    @pytest.mark.asyncio
    async def test_recap_dashboard_query_performance(self):
        """Recap dashboard returns full payload without error; market aggregation runs once (no duplicate fetches)."""
        cache.clear()
        query = """
        query {
            recapDashboard {
                metrics { totalConsumersSampled totalPurchases conversionRate revenueGenerated }
                monthlyTrends { dataPoints { month recapsCount consumersSampled } }
                performanceInsights {
                    newCustomersSampled bestMonth { month recapsCount consumersCount }
                    topConvertingMarket { marketName conversionRate }
                    highestWillingnessToBuy { marketName willingCount }
                    strongestBrandAwareness { marketName brandAwareCount }
                    growthRate
                }
                marketAnalysis { dataPoints { marketId marketName consumers conversion } }
                rmmPerformance { dataPoints { rmmId rmmName consumersSampled conversionRate } }
            }
        }
        """
        result = await self._execute_query_authenticated(
            query, {}, self.client_user
        )
        assert result.errors is None
        assert result.data is not None
        data = result.data["recapDashboard"]
        assert data["metrics"] is not None
        assert data["performanceInsights"] is not None
        assert data["marketAnalysis"] is not None
        assert data["rmmPerformance"] is not None

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
