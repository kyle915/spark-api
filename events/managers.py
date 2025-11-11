from django.db import models


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


class RequestStatusManager(models.Manager):
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
