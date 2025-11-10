from django.db import models


class RequestStatusManager(models.Manager):
    """
    Custom manager for `RequestStatus` that provides helper shortcuts.
    """

    def get_default(self, tenant=None):
        """
        Return the default status.

        If a tenant is provided, the lookup will be scoped to that tenant.
        Returns `None` when no default status exists.
        """
        queryset = self.get_queryset().filter(is_default=True)

        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)

        return queryset.first()
