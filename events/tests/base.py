"""
Base test class for events app tests.

This module provides helper methods for creating event-related models
(Client, Distributor, Retailer, Request, Event, etc.) for testing.
"""
from datetime import date as date_type, time, datetime
from django.utils import timezone
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
        date: date_type | datetime,
        address: str,
        client: event_models.Client,
        distributor: event_models.Distributor,
        retailer: event_models.Retailer,
        request_type: event_models.RequestType,
        tenant: Tenant,
        start_time: time | datetime | None = None,
        end_time: time | datetime | None = None,
        status: event_models.RequestStatus | None = None,
        **kwargs
    ):
        """Create a Request instance."""
        system_user = self.get_system_user()

        # Convert date to datetime if needed (Request.date is now DateTimeField)
        # datetime is a subclass of date, so check specifically for date but not datetime
        if isinstance(date, date_type) and not isinstance(date, datetime):
            date_value = timezone.make_aware(datetime.combine(date, time.min))
        elif isinstance(date, datetime):
            date_value = date
        else:
            date_value = date

        # Convert start_time to datetime if needed (Request.start_time is now DateTimeField)
        if start_time is not None:
            if isinstance(start_time, time) and not isinstance(start_time, datetime):
                # Combine with the date
                if isinstance(date_value, datetime):
                    date_only = date_value.date()
                else:
                    date_only = date_value
                start_time_value = timezone.make_aware(
                    datetime.combine(date_only, start_time))
            else:
                start_time_value = start_time
        else:
            start_time_value = None

        # Convert end_time to datetime if needed (Request.end_time is now DateTimeField)
        if end_time is not None:
            if isinstance(end_time, time) and not isinstance(end_time, datetime):
                # Combine with the date
                if isinstance(date_value, datetime):
                    date_only = date_value.date()
                else:
                    date_only = date_value
                end_time_value = timezone.make_aware(
                    datetime.combine(date_only, end_time))
            else:
                end_time_value = end_time
        else:
            end_time_value = None

        return event_models.Request.objects.create(
            name=name,
            date=date_value,
            address=address,
            client=client,
            distributor=distributor,
            retailer=retailer,
            request_type=request_type,
            tenant=tenant,
            start_time=start_time_value,
            end_time=end_time_value,
            status=status,
            created_by=system_user,
            **kwargs
        )
