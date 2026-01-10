from asgiref.sync import sync_to_async
import strawberry

from django.db import models

from tenants.models import Tenant
from utils.models import BaseManager as UtilsBaseManager


class BaseManager(UtilsBaseManager):
    def by_tenant(self, tenant: Tenant):
        """Return by tenant."""
        return self.filter(tenant=tenant)

    def by_id(self, id: int | str | strawberry.ID):
        """Return by ID."""
        return self.select_related("user").get(pk=int(id))

    async def _by_id(self, id: int | str | strawberry.ID):
        """Return by ID but in async way."""
        return await sync_to_async(self.by_id)(int(id))


class AmbassadorManager(BaseManager, models.Manager):
    def active(self):
        """Return active ambassadors."""
        return self.filter(is_active=True)

    def inactive(self):
        """Return inactive ambassadors."""
        return self.filter(is_active=False)


class AmbassadorInvitationManager(BaseManager, models.Manager):
    def by_id(self, id: int | str | strawberry.ID):
        """Return by ID."""
        return self.select_related("tenant", "invited_by").get(pk=int(id))

    def by_token(self, token: str):
        """Return ambassador invitation by token."""
        return self.select_related("tenant", "invited_by").get(token=token)

    async def _by_token(self, token: str):
        """Return ambassador invitation by token but in async way."""
        return await sync_to_async(self.by_token)(token)


class AmbassadorReviewManager(BaseManager, models.Manager):
    def by_id(self, id: int | str | strawberry.ID):
        """Return by ID with select_related."""
        return self.select_related("ambassador", "client", "tenant").get(pk=int(id))

    def by_ambassador_and_client(self, ambassador_id: int, client_id: int):
        """Check if a review exists for this ambassador and client combination."""
        return self.filter(ambassador_id=ambassador_id, client_id=client_id).first()

    async def _exists_by_ambassador_and_client(self, ambassador_id: int, client_id: int):
        """Check if a review exists for this ambassador and client combination (async)."""
        return await sync_to_async(
            lambda: self.filter(ambassador_id=ambassador_id,
                                client_id=client_id).exists()
        )()


class SkillManager(BaseManager, models.Manager):
    """Manager for Skill model with async support."""

    def by_id(self, id: int | str | strawberry.ID):
        """Return by ID with select_related."""
        return self.select_related("tenant").get(pk=int(id))


class AmbassadorSkillManager(BaseManager, models.Manager):
    """Manager for AmbassadorSkill model with async support."""

    def by_id(self, id: int | str | strawberry.ID):
        """Return by ID with select_related."""
        return self.select_related("ambassador", "skill", "tenant").get(pk=int(id))

    async def _exists_by_ambassador_and_skill(self, ambassador_id: int, skill_id: int):
        """Check if an AmbassadorSkill exists for this ambassador and skill combination (async)."""
        return await sync_to_async(
            lambda: self.filter(ambassador_id=ambassador_id,
                                skill_id=skill_id).exists()
        )()


class GroupTypeManager(UtilsBaseManager, models.Manager):
    """Manager for GroupType model with async support."""

    pass


class AmbassadorGroupManager(UtilsBaseManager, models.Manager):
    """Manager for AmbassadorGroup model with async support."""

    async def _by_id(self, id: int | str | strawberry.ID):
        """Return by ID but in async way."""
        return await sync_to_async(self.by_id)(int(id))


class UserGroupManager(UtilsBaseManager, models.Manager):
    """Manager for UserGroup model with async support."""

    pass
