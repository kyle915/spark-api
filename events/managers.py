from django.db import models
from asgiref.sync import sync_to_async

from tenants.models import User


class DefaultStatusManager(models.Manager):
    """
    Custom manager for `DefaultStatus` that provides helper shortcuts.
    """

    def get_default(self, tenant_id: int | None = None) -> models.Model | None:
        """
        Return the default status.

        If a tenant is provided, the lookup will be scoped to that tenant.
        Returns `None` when no default status exists.
        """
        queryset = self.get_queryset().filter(is_default=True)

        if tenant_id is not None:
            queryset = queryset.filter(tenant_id=tenant_id)

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

    def get_by_slug(self, slug: str, tenant=None) -> models.Model | None:
        """
        Return the status by slug.

        If a tenant is provided, the lookup will be scoped to that tenant.
        Returns `None` when no status with the given slug exists.
        """
        queryset = self.get_queryset().filter(slug=slug)

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

    async def from_request(
        self,
        request: models.Model,
        created_by: User,
        event_type: models.Model | None = None,
        status: models.Model | None = None,
    ) -> models.Model:
        """Create an event from a request."""
        from .models import EventType, EventStatus

        async def get_object_or_default(
            model_class: models.Model,
            tenant_id: int,
            model: models.Model | None = None,
        ) -> models.Model:
            if model:
                return model
            return await sync_to_async(model_class.objects.get_default)(tenant_id=tenant_id)

        event_type = await get_object_or_default(EventType, request.tenant_id, event_type)
        status = await get_object_or_default(EventStatus, request.tenant_id, status)

        if not status:
            raise ValueError("Event status not found.")

        return await sync_to_async(self.create)(
            request=request,
            tenant_id=request.tenant_id,
            created_by=created_by,
            event_type=event_type,
            status=status,
            name=request.name,
            start_time=request.start_time,
            end_time=request.end_time,
            address=request.address,
        )
