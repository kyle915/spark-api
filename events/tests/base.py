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

    @staticmethod
    def _normalize_to_datetime(value: date_type | datetime) -> datetime:
        """
        Convert a date or datetime to a timezone-aware datetime.

        Args:
            value: A date or datetime object

        Returns:
            A timezone-aware datetime object
        """
        if isinstance(value, datetime):
            return value if timezone.is_aware(value) else timezone.make_aware(value)
        if isinstance(value, date_type):
            return timezone.make_aware(datetime.combine(value, time.min))
        return value

    @staticmethod
    def _normalize_time_to_datetime(
        time_value: time | datetime | None,
        reference_date: date_type | datetime
    ) -> datetime | None:
        """
        Convert a time or datetime to a timezone-aware datetime using a reference date.

        Args:
            time_value: A time or datetime object, or None
            reference_date: The date to use when combining with a time object

        Returns:
            A timezone-aware datetime object, or None if time_value is None
        """
        if time_value is None:
            return None

        if isinstance(time_value, datetime):
            return time_value if timezone.is_aware(time_value) else timezone.make_aware(time_value)

        if isinstance(time_value, time):
            date_part = reference_date.date() if isinstance(
                reference_date, datetime) else reference_date
            return timezone.make_aware(datetime.combine(date_part, time_value))

        return time_value

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
    ) -> event_models.Request:
        """
        Create a Request instance.

        Note: The Request model uses DateTimeField for date, start_time, and end_time.
        This method automatically converts date and time objects to timezone-aware datetime
        objects to maintain compatibility with existing test code.

        Args:
            name: Name of the request
            date: Date or datetime for the request (will be normalized to datetime)
            address: Address of the request
            client: Client instance
            distributor: Distributor instance
            retailer: Retailer instance
            request_type: RequestType instance
            tenant: Tenant instance
            start_time: Optional time or datetime for start time
            end_time: Optional time or datetime for end time
            status: Optional RequestStatus instance
            **kwargs: Additional fields to set on the request

        Returns:
            The created Request instance
        """
        normalized_date = self._normalize_to_datetime(date)
        normalized_start_time = self._normalize_time_to_datetime(
            start_time, normalized_date)
        normalized_end_time = self._normalize_time_to_datetime(
            end_time, normalized_date)

        return event_models.Request.objects.create(
            name=name,
            date=normalized_date,
            address=address,
            client=client,
            distributor=distributor,
            retailer=retailer,
            request_type=request_type,
            tenant=tenant,
            start_time=normalized_start_time,
            end_time=normalized_end_time,
            status=status,
            created_by=self.get_system_user(),
            **kwargs
        )
