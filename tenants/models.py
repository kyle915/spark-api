from uuid6 import uuid7
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings

class Tenant(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100)
    created_at = models.DateField(auto_now_add=True)

class Role(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self): return self.name
    
class User(AbstractUser):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    role = models.ForeignKey(Role, on_delete=models.RESTRICT, related_name="users")
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.username

class TenantedUser(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tenanted_users")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tenanted_users")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} @ {self.tenant.name}"

class TenantedRole(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tenanted_roles")
    role = models.ForeignKey(Role, on_delete=models.RESTRICT, related_name="tenanted_roles")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.role.name} @ {self.tenant.name}"