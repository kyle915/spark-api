from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models import QuerySet
from utils.models import BaseManager as UtilsBaseManager


class BaseManager(UtilsBaseManager):
    pass


class UserManager(BaseManager, DjangoUserManager):
    """
    Custom manager for `User` that provides helper shortcuts.
    """

    def get_queryset(self) -> QuerySet:
        """
        Return the queryset for the user.
        """
        return super().get_queryset().select_related("role")


class TenantedUserManager(BaseManager, models.Manager):
    """
    Custom manager for `TenantedUser` that provides helper shortcuts.
    """

    def get_queryset(self) -> QuerySet:
        """
        Return the queryset for the tenanted user.
        """
        return super().get_queryset().select_related("user", "tenant")


class TenantManager(BaseManager, models.Manager):
    """
    Custom manager for `Tenant` that provides helper shortcuts.
    """
    pass
