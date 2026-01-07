from django.db import models
from utils.models import BaseManager


class StatusManager(BaseManager, models.Manager):
    """Manager for Status model with async support."""

    def get_invited(self, tenant_id: int, user):
        """
        Get or create the 'invited' status for a tenant.

        Args:
            tenant_id: Tenant ID
            user: User instance (not user_id) for created_by/updated_by

        Returns:
            Status: The invited status instance
        """
        try:
            status = self.get(slug="invited", tenant_id=tenant_id)
        except self.model.DoesNotExist:
            status = self.create(name="Invited",
                                 slug="invited",
                                 tenant_id=tenant_id,
                                 created_by=user,
                                 updated_by=user
                                 )
        return status


class JobManager(BaseManager, models.Manager):
    """Manager for Job model with async support."""

    pass


class AmbassadorJobManager(BaseManager, models.Manager):
    """Manager for AmbassadorJob model with async support."""

    pass
