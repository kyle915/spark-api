from uuid6 import uuid7
from django.utils.text import slugify
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings

from .managers import UserManager, TenantedUserManager


class Tenant(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="tenants_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="tenants_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Role(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True, null=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="role_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="role_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    async def is_ambassador(self) -> bool:
        return self.slug == 'ambassador'

    @property
    async def is_spark_admin(self) -> bool:
        return self.slug == 'spark-admin'

    @property
    async def is_client(self) -> bool:
        return self.slug == 'client'


class User(AbstractUser):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    role = models.ForeignKey(
        Role, on_delete=models.RESTRICT, related_name="users")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="user_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="user_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    objects = UserManager()

    def __str__(self):
        return self.username

    @property
    def tenant(self) -> Tenant:
        """Get the tenant for the user.

        @TODO: Maybe we should check performance of this property.

        Returns:
            Tenant: The tenant for the user.
        """
        return TenantedUser.objects.get(
            user=self, is_active=True
        ).tenant

    def get_tenant(self, tenant_id: int | None = None) -> Tenant | None:
        """Get the tenant for the user.

        @TODO: Maybe we should check performance of this method. 
        Maybe we should cache the tenant for the user for the given tenant_id.

        Returns:
            Tenant: The tenant for the user.
        """
        try:
            if tenant_id:
                return TenantedUser.objects.get(
                    user=self, tenant_id=tenant_id
                ).tenant
            return self.tenant
        except Tenant.DoesNotExist:
            return None


class TenantedUser(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name="tenanted_users",
    )
    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="tenanted_users"
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="tenanted_users_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="tenanted_users_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantedUserManager()

    def __str__(self):
        return f"{self.user.username} @ {self.tenant.name}"


class TenantedRole(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="tenanted_roles"
    )
    role = models.ForeignKey(
        Role, on_delete=models.RESTRICT, related_name="tenanted_roles"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="tenanted_roles_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="tenanted_roles_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.role.name} @ {self.tenant.name}"
