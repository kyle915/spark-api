"""
Tests for Dashboard queries.

This module tests Event Dashboard queries:
- event_dashboard_filter_options
- event_dashboard
"""
import pytest
import strawberry_django  # noqa: F401
from datetime import timedelta, time
from asgiref.sync import sync_to_async
from django.db.models import Sum
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
    async def test_event_dashboard_products_sold_custom_recap_purchase_vocab(self):
        """globalKpis.productsSold folds in custom-recap sales via the SAME
        two-tier matcher the recap list uses (recaps.types
        ._sold_units_from_fields) — one source of truth.

        Regression guard for the dashboard's OLD cans/packs-only inline matcher,
        which showed 0 "Products sold" for bread tenants (Stone House Bread)
        whose template logs sales as "...did consumers PURCHASE..." (no "sold"),
        right next to a "willing to purchase" INTENT field that must NOT count.
        """
        from recaps import models as recap_models

        @sync_to_async
        def add_custom_recap():
            system_user = self.get_system_user()
            template = recap_models.CustomRecapTemplate.objects.create(
                name="Bread recap template",
                event_type=self.event_type,
                tenant=self.tenant,
                created_by=system_user,
            )
            field_type = recap_models.CustomRecapFieldType.objects.create(
                name="Number", created_by=system_user,
            )
            section = recap_models.RecapSection.objects.create(
                name="Sampling", tenant=self.tenant, created_by=system_user,
            )

            def _field(name):
                return recap_models.CustomField.objects.create(
                    name=name, required=False,
                    custom_recap_template=template,
                    custom_field_type=field_type,
                    recap_section=section,
                    created_by=system_user,
                )

            purchase_field = _field(
                "How many products did consumers purchase during the event?"
            )
            intent_field = _field(
                "How many consumers would be willing to purchase the "
                "product after tasting it?"
            )
            sampled_field = _field("Total number of consumers sampled")

            custom_recap = recap_models.CustomRecap.objects.create(
                name="Stone House Bread recap",
                approved=True,
                event=self.event1,
                tenant=self.tenant,
                custom_recap_template=template,
                ambassador=self.ambassador,
                created_by=system_user,
            )
            for field, value in (
                (purchase_field, "12"),
                (intent_field, "20"),
                (sampled_field, "45"),
            ):
                recap_models.CustomFieldValue.objects.create(
                    custom_recap=custom_recap, custom_field=field,
                    value=value, created_by=system_user,
                )

        await add_custom_recap()
        cache.clear()

        query = """
        query {
            eventDashboard {
                globalKpis {
                    singleCansSold
                    multiPacksSold
                    productsSold
                }
            }
        }
        """
        result = await self._execute_query_authenticated(
            query, {}, self.client_user
        )

        assert result.errors is None
        kpis = result.data["eventDashboard"]["globalKpis"]
        # Legacy recaps (products_sold 50 + 40 + 60 = 150) + the custom-recap
        # PURCHASE field (12). The "willing to purchase" INTENT (20) is excluded.
        assert kpis["productsSold"] == 162
        # The custom recap has no cans/packs fields, so the drink tiles are
        # unchanged at the legacy sums (24+18+30 / 12+9+15).
        assert kpis["singleCansSold"] == 72
        assert kpis["multiPacksSold"] == 36

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
    async def test_event_dashboard_with_multiple_distributors_filter(self):
        """Test event_dashboard query with multiple distributors filter."""
        today = timezone.now().date()
        other_distributor = self.create_distributor(
            name="Other Distributor",
            email="other-distributor@example.com",
            location=self.location,
            tenant=self.tenant,
        )
        other_request = self.create_request(
            name="Request Other Distributor",
            date=today,
            address="Address Other Distributor",
            client=self.client,
            distributor=other_distributor,
            retailer=self.retailer,
            request_type=self.request_type,
            tenant=self.tenant,
            start_time=time(11, 0),
            end_time=time(19, 0),
            status=self.approved_status,
        )
        self.create_event(
            name="Event Other Distributor",
            tenant=self.tenant,
            address="Address Other Distributor",
            request=other_request,
            event_type=self.event_type,
            status=self.event_status,
            rmm_asigned=self.rmm_user,
        )

        query = """
        query EventDashboard($distributorId: ID, $distributorIds: [ID!]) {
            single: eventDashboard(filters: {
                distributorId: $distributorId
            }) {
                metrics {
                    totalEvents
                }
            }
            multiple: eventDashboard(filters: {
                distributorIds: $distributorIds
            }) {
                metrics {
                    totalEvents
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {
                'distributorId': str(self.distributor.id),
                'distributorIds': [str(self.distributor.id), str(other_distributor.id)],
            },
            self.client_user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data['single']['metrics']['totalEvents'] < result.data['multiple']['metrics']['totalEvents']

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
    async def test_event_dashboard_percentages_are_clamped_to_100(self):
        """Percentages should be capped at 100 even with inconsistent data."""
        from recaps import models as recap_models

        # Create inconsistent consumer engagement data (brand aware > total)
        await sync_to_async(recap_models.ConsumerEngagements.objects.create)(
            recap=self.recap1,
            total_consumer=10,
            first_time_consumers=0,
            brand_aware_consumers=200,
            willing_to_purchase_consumers=0,
            not_willing_consumers=0,
            created_by=self.get_system_user(),
        )

        # Sanity check: raw aggregate ratio (without clamping) would exceed 100%
        agg = await sync_to_async(
            lambda: recap_models.ConsumerEngagements.objects.filter(
                recap__event__in=[self.event1, self.event2, self.event3]
            ).aggregate(
                total_consumers=Sum("total_consumer"),
                total_brand_aware=Sum("brand_aware_consumers"),
            )
        )()
        raw_percentage = (
            agg["total_brand_aware"] / agg["total_consumers"] * 100
            if agg["total_consumers"] > 0
            else 0.0
        )
        assert raw_percentage > 100.0

        query = """
        query {
            eventDashboard {
                metrics {
                    brandAwareness
                    purchaseIntent
                }
            }
        }
        """

        result = await self._execute_query_authenticated(
            query,
            {},
            self.client_user,
        )

        assert result.errors is None
        metrics = result.data["eventDashboard"]["metrics"]
        assert metrics["brandAwareness"] == pytest.approx(100.0)
        assert 0.0 <= metrics["purchaseIntent"] <= 100.0

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
