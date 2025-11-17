from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models import QuerySet


class UserManager(DjangoUserManager):
    """
    Custom manager for `User` that provides helper shortcuts.
    """

    def get_queryset(self) -> QuerySet:
        """
        Return the queryset for the user.
        """
        return super().get_queryset().select_related("role")


class TenantedUserManager(models.Manager):
    """
    Custom manager for `TenantedUser` that provides helper shortcuts.
    """

    def get_queryset(self) -> QuerySet:
        """
        Return the queryset for the tenanted user.
        """
        return super().get_queryset().select_related("user", "tenant")
