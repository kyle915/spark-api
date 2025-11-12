from django.db import models

from tenants.models import User


class DefaultStatusManager(models.Manager):
    """
    Custom manager for `DefaultStatus` that provides helper shortcuts.
    """

    def get_default(self, tenant=None) -> models.Model | None:
        """
        Return the default status.

        If a tenant is provided, the lookup will be scoped to that tenant.
        Returns `None` when no default status exists.
        """
        queryset = self.get_queryset().filter(is_default=True)

        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)

        return queryset.first()


class RequestStatusManager(DefaultStatusManager):
    """
    Custom manager for `RequestStatus` that provides helper shortcuts.
    """

    def get_for_approval(self, tenant=None) -> models.Model | None:
        """
        Return the status for approval.

        If a tenant is provided, the lookup will be scoped to that tenant.
        Returns `None` when no status for approval exists.
        """
        queryset = self.get_queryset().filter(create_event=True)

        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)

        return queryset.first()


class EventStatusManager(DefaultStatusManager):
    pass


class EventTypeManager(DefaultStatusManager):
    pass


class EventManager(models.Manager):
    """
    Custom manager for `Event` that provides helper shortcuts.
    """

    def from_request(
        self,
        request: models.Model,
        created_by: User,
        event_type: models.Model | None = None,
        status: models.Model | None = None,
    ) -> models.Model:
        """Create an event from a request."""
        from .models import EventType, EventStatus
        event_type = event_type or EventType.objects.get_default(
            tenant=request.tenant)
        status = status or EventStatus.objects.get_default(
            tenant=request.tenant)

        if not event_type:
            raise ValueError("Event type not found.")

        if not status:
            raise ValueError("Event status not found.")

        if not request.tenant:
            raise ValueError("Request tenant not found.")

        return self.create(
            request=request,
            tenant=request.tenant,
            created_by=created_by,
            event_type=event_type,
            status=status,
            name=request.name,
            start_time=request.start_time,
            end_time=request.end_time,
            address=request.address,
        )
