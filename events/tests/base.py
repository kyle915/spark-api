"""
Base test class for events app tests.

This module provides helper methods for creating event-related models
(Client, Distributor, Retailer, Request, Event, etc.) for testing.
"""
from datetime import date, time
from jobs.tests.base import JobsGraphQLTestCase
from events import models as event_models
from tenants.models import Tenant


class EventsGraphQLTestCase(JobsGraphQLTestCase):
    """
    Base test class for events-related queries and mutations.

    Extends JobsGraphQLTestCase to reuse all job-related helper methods
    and adds methods for creating event-related models.
    """

    def create_client(self, name: str, email: str, tenant: Tenant, **kwargs):
        """Create a Client instance."""
        system_user = self.get_system_user()
        return event_models.Client.objects.create(
            name=name,
            email=email,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

    def create_distributor(self, name: str, email: str, location: event_models.Location, tenant: Tenant, **kwargs):
        """Create a Distributor instance."""
        system_user = self.get_system_user()
        return event_models.Distributor.objects.create(
            name=name,
            email=email,
            location=location,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

    def create_retailer(self, name: str, address: str, store_contact: str, location: event_models.Location, tenant: Tenant, **kwargs):
        """Create a Retailer instance."""
        system_user = self.get_system_user()
        return event_models.Retailer.objects.create(
            name=name,
            address=address,
            store_contact=store_contact,
            location=location,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

    def create_request_type(self, name: str, tenant: Tenant, **kwargs):
        """Create a RequestType instance."""
        system_user = self.get_system_user()
        return event_models.RequestType.objects.create(
            name=name,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

    def create_request_status(self, name: str, tenant: Tenant, create_event: bool = False, **kwargs):
        """Create a RequestStatus instance."""
        system_user = self.get_system_user()
        return event_models.RequestStatus.objects.create(
            name=name,
            tenant=tenant,
            create_event=create_event,
            created_by=system_user,
            **kwargs
        )

    def create_event_status(self, name: str, tenant: Tenant, **kwargs):
        """Create an EventStatus instance."""
        system_user = self.get_system_user()
        return event_models.EventStatus.objects.create(
            name=name,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

    def create_event_type(self, name: str, tenant: Tenant, **kwargs):
        """Create an EventType instance."""
        system_user = self.get_system_user()
        return event_models.EventType.objects.create(
            name=name,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

    def create_request(
        self,
        name: str,
        date: date,
        address: str,
        client: event_models.Client,
        distributor: event_models.Distributor,
        retailer: event_models.Retailer,
        request_type: event_models.RequestType,
        tenant: Tenant,
        start_time: time | None = None,
        end_time: time | None = None,
        status: event_models.RequestStatus | None = None,
        **kwargs
    ):
        """Create a Request instance."""
        system_user = self.get_system_user()
        return event_models.Request.objects.create(
            name=name,
            date=date,
            address=address,
            client=client,
            distributor=distributor,
            retailer=retailer,
            request_type=request_type,
            tenant=tenant,
            start_time=start_time,
            end_time=end_time,
            status=status,
            created_by=system_user,
            **kwargs
        )
