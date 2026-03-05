"""
Base test class for dashboard tests.

This module provides a base class with common setup for dashboard query tests.
"""
import pytest
from datetime import date, time, timedelta, datetime
from django.utils import timezone
from django.core.cache import cache
from events.tests.base import EventsGraphQLTestCase
from events import models as event_models
from jobs import models as job_models
from ambassadors import models as ambassador_models


class DashboardGraphQLTestCase(EventsGraphQLTestCase):
    """
    Base test class for dashboard queries.
    
    Extends EventsGraphQLTestCase and provides common setup for dashboard tests,
    including creating all necessary test data (requests, events, jobs, ambassadors, etc.).
    """

    @pytest.fixture(autouse=True)
    def setup_dashboard_data(self, db):
        """
        Set up common dashboard test data.
        
        This fixture creates:
        - Tenant and roles
        - Client user for authentication
        - Location, Client, Distributor, Retailer
        - Request types and statuses
        - Event types and statuses
        - Sample requests and events
        - Jobs and ambassadors
        """
        from config.schema_client import schema_clients

        # Clear cache before each test
        cache.clear()

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")

        # Create a client user for authentication
        self.client_user = self.create_user(
            username="client@test.com",
            email="client@test.com",
            role=self.roles['client'],
            password="testpass123"
        )
        self.create_tenanted_user(user=self.client_user, tenant=self.tenant)

        self.rmm_user = self.create_user(
            username="rmm@test.com",
            email="rmm@test.com",
            role=self.roles['client'],
            password="testpass123"
        )
        self.create_tenanted_user(user=self.rmm_user, tenant=self.tenant)

        # Create prerequisite data
        self.location = self.create_location(
            name="Test Location",
            code="TEST",
            zip_code="12345",
            tenant=self.tenant
        )

        self.client = self.create_client(
            name="Test Client",
            email="client@example.com",
            tenant=self.tenant
        )
        self.distributor = self.create_distributor(
            name="Test Distributor",
            email="distributor@example.com",
            location=self.location,
            tenant=self.tenant
        )
        self.retailer = self.create_retailer(
            name="Test Retailer",
            address="Retailer Address",
            store_contact="Contact",
            location=self.location,
            tenant=self.tenant
        )
        self.request_type = self.create_request_type(
            name="Test Request Type",
            tenant=self.tenant
        )

        # Create request statuses
        self.approved_status = self.create_request_status(
            name="Approved",
            tenant=self.tenant,
            create_event=True
        )
        self.rejected_status = self.create_request_status(
            name="Rejected",
            tenant=self.tenant,
            create_event=False
        )

        # Create event status
        self.event_status = self.create_event_status(
            name="Active",
            tenant=self.tenant
        )

        # Create event type
        self.event_type = self.create_event_type(
            name="Promotion",
            tenant=self.tenant
        )

        # Create requests
        today = timezone.now().date()
        self.request1 = self.create_request(
            name="Request 1",
            date=today,
            address="Address 1",
            client=self.client,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.request_type,
            tenant=self.tenant,
            start_time=time(9, 0),
            end_time=time(17, 0),
            status=self.approved_status
        )

        self.request2 = self.create_request(
            name="Request 2",
            date=today - timedelta(days=1),
            address="Address 2",
            client=self.client,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.request_type,
            tenant=self.tenant,
            start_time=time(10, 0),
            end_time=time(18, 0),
            status=self.rejected_status
        )

        # Create events from requests
        self.event1 = self.create_event(
            name="Event 1",
            tenant=self.tenant,
            address="Address 1",
            request=self.request1,
            event_type=self.event_type,
            status=self.event_status,
            rmm_asigned=self.rmm_user
        )

        self.event2 = self.create_event(
            name="Event 2",
            tenant=self.tenant,
            address="Address 2",
            request=self.request2,
            event_type=self.event_type,
            status=self.event_status,
            rmm_asigned=self.rmm_user
        )

        # Create company and jobs
        self.company = self.create_company(
            name="Test Company",
            email="company@test.com",
            phone="123-456-7890",
            tenant=self.tenant
        )
        self.job_title = self.create_job_title(
            name="Promoter",
            tenant=self.tenant
        )

        self.job1 = self.create_job(
            name="Job 1",
            code="JOB-001",
            address="Job Address 1",
            event=self.event1,
            job_title=self.job_title,
            tenant=self.tenant
        )

        # Create ambassador
        ambassador_user = self.create_user(
            username="ambassador@test.com",
            email="ambassador@test.com",
            role=self.roles['ambassador'],
            password="testpass123"
        )
        self.ambassador = self.create_ambassador(ambassador_user)

        # Create ambassador event
        self.ambassador_event = ambassador_models.AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=self.event1,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.get_system_user()
        )

        # Create ambassador job
        status = self.create_status(name="Assigned", tenant=self.tenant)
        rate_type = self.create_rate_type(name="Hourly", tenant=self.tenant)
        rate = self.create_rate(amount=20.0, rate_type=rate_type, tenant=self.tenant)
        self.ambassador_job = self.create_ambassador_job(
            ambassador=self.ambassador,
            job=self.job1,
            status=status,
            rate=rate,
            tenant=self.tenant
        )

        # Create recaps and consumer engagements for Event Dashboard tests
        from recaps import models as recap_models
        
        # Create recap for event1
        self.recap1 = recap_models.Recap.objects.create(
            name="Recap 1",
            event=self.event1,
            ambassador=self.ambassador,
            job=self.job1,
            retailer=self.retailer,
            total_engagements=100,
            products_sold=50,
            total_cans_sold=24,
            total_packs_sold=12,
            total_earnings=1000.0,
            approved=True,
            created_by=self.get_system_user()
        )
        
        # Create consumer engagements for recap1
        self.consumer_engagements1 = recap_models.ConsumerEngagements.objects.create(
            recap=self.recap1,
            total_consumer=100,
            first_time_consumers=30,
            brand_aware_consumers=40,
            willing_to_purchase_consumers=70,
            not_willing_consumers=30,
            created_by=self.get_system_user()
        )
        
        # Create recap for event2
        self.recap2 = recap_models.Recap.objects.create(
            name="Recap 2",
            event=self.event2,
            ambassador=self.ambassador,
            total_engagements=80,
            products_sold=40,
            total_cans_sold=18,
            total_packs_sold=9,
            total_earnings=800.0,
            approved=False,
            created_by=self.get_system_user()
        )
        
        # Create consumer engagements for recap2
        self.consumer_engagements2 = recap_models.ConsumerEngagements.objects.create(
            recap=self.recap2,
            total_consumer=80,
            first_time_consumers=20,
            brand_aware_consumers=30,
            willing_to_purchase_consumers=50,
            not_willing_consumers=30,
            created_by=self.get_system_user()
        )
        
        # Create an upcoming event for recent events test
        future_date = today + timedelta(days=7)
        self.request3 = self.create_request(
            name="Request 3",
            date=future_date,
            address="Address 3",
            client=self.client,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.request_type,
            tenant=self.tenant,
            start_time=time(9, 0),
            end_time=time(17, 0),
            status=self.approved_status
        )
        
        self.event3 = self.create_event(
            name="Event 3",
            tenant=self.tenant,
            address="Address 3",
            request=self.request3,
            event_type=self.event_type,
            status=self.event_status,
            rmm_asigned=self.rmm_user,
            date=timezone.make_aware(datetime.combine(future_date, time(10, 0))),
            start_time=timezone.make_aware(datetime.combine(future_date, time(10, 0)))
        )
        
        # Create recap for event3 (upcoming event)
        self.recap3 = recap_models.Recap.objects.create(
            name="Recap 3",
            event=self.event3,
            ambassador=self.ambassador,
            total_engagements=120,
            products_sold=60,
            total_cans_sold=30,
            total_packs_sold=15,
            total_earnings=1200.0,
            approved=True,
            created_by=self.get_system_user()
        )
        
        self.consumer_engagements3 = recap_models.ConsumerEngagements.objects.create(
            recap=self.recap3,
            total_consumer=120,
            first_time_consumers=40,
            brand_aware_consumers=50,
            willing_to_purchase_consumers=90,
            not_willing_consumers=30,
            created_by=self.get_system_user()
        )

        # Set schema and endpoint for tests
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
