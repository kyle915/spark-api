from uuid6 import uuid7
from asgiref.sync import sync_to_async

from django.utils.text import slugify
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings

from .managers import UserManager, TenantedUserManager, TenantManager
from utils.models import Asyncable
from utils.utils import default_tenant_theme


class Tenant(Asyncable, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100)
    image = models.ImageField(upload_to="tenants/images", null=True)
    request_url_name = models.CharField(max_length=100, unique=True, null=True)
    slug = models.SlugField(max_length=50, null=True)
    # Per-tenant Google Sheet that mirrors the Master Tracker. Set by
    # admins via the front-end "Link Sheet" chip; the "Copy for Sheets"
    # TSV path expects this URL to live somewhere persistent. Storing
    # here (instead of localStorage) means every teammate sees the
    # same link from any device, and Phase 2 sync workers know which
    # sheet to write back to.
    linked_sheet_url = models.URLField(max_length=512, null=True, blank=True)
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

    objects = TenantManager()


class TenantTheme(models.Model):
    """
    Per-tenant visual theme configuration compatible with DaisyUI.

    The frontend can use `css_variables` directly to construct a theme
    definition or apply CSS custom properties.
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="themes",
    )

    # Optional human-readable / daisyUI theme name
    name = models.CharField(max_length=64, default="default")

    # High-level color scheme hint (e.g. for prefers-color-scheme)
    color_scheme = models.CharField(
        max_length=16,
        choices=[("light", "Light"), ("dark", "Dark")],
        default="dark",
    )

    # Raw DaisyUI-compatible variables
    css_variables = models.JSONField(default=default_tenant_theme)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="tenant_themes_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="tenant_themes_updated",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Each tenant may have multiple themes (e.g. light/dark) but only
        # one per color_scheme.
        unique_together = ("tenant", "color_scheme")

    def __str__(self) -> str:
        return f"Theme '{self.name}' ({self.color_scheme}) for tenant {self.tenant_id}"


class Role(models.Model):
    AMBASSADOR_SLUG = "ambassador"
    SPARK_ADMIN_SLUG = "spark-admin"
    CLIENT_SLUG = "client"

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
        return self._is_ambassador

    @property
    async def is_spark_admin(self) -> bool:
        return self._is_spark_admin

    @property
    async def is_client(self) -> bool:
        return self._is_client

    @property
    def _is_spark_admin(self) -> bool:
        return self.slug == Role.SPARK_ADMIN_SLUG

    @property
    def _is_client(self) -> bool:
        return self.slug == Role.CLIENT_SLUG

    @property
    def _is_ambassador(self) -> bool:
        return self.slug == Role.AMBASSADOR_SLUG

    @staticmethod
    async def get_ambassador_role() -> "Role":
        return await sync_to_async(Role.objects.get)(slug=Role.AMBASSADOR_SLUG)

    @staticmethod
    async def get_spark_admin_role() -> "Role":
        return await sync_to_async(Role.objects.get)(slug=Role.SPARK_ADMIN_SLUG)

    @staticmethod
    async def get_client_role() -> "Role":
        return await sync_to_async(Role.objects.get)(slug=Role.CLIENT_SLUG)


class User(AbstractUser):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    image = models.ImageField(upload_to="users/images", null=True)
    role = models.ForeignKey(Role, on_delete=models.RESTRICT, related_name="users")

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
        return TenantedUser.objects.get(user=self, is_active=True).tenant

    def get_tenant(
        self,
        tenant_id: int | None = None,
        tenant_uuid: str | None = None,
    ) -> Tenant | None:
        """Get the tenant for the user by id or uuid.

        @TODO: Maybe we should check performance of this method.
        Maybe we should cache the tenant for the user for the given tenant_id.

        Returns:
            Tenant: The tenant for the user.
        """
        try:
            if not tenant_id and not tenant_uuid:
                return self.tenant

            filters = {
                "user": self,
                "is_active": True,
            }

            if tenant_id:
                filters["tenant_id"] = tenant_id
            if tenant_uuid:
                filters["tenant__uuid"] = tenant_uuid

            return TenantedUser.objects.get(**filters).tenant
        except (Tenant.DoesNotExist, TenantedUser.DoesNotExist):
            raise Tenant.DoesNotExist


class PasswordResetCode(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_reset_codes",
    )
    code = models.CharField(max_length=4)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["user", "code", "is_used", "expires_at"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"Password reset code for {self.user.email}"


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


class GoogleCalendarConnection(models.Model):
    """Model to store Google Calendar OAuth connection for users."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="google_calendar_connection",
    )

    # Encrypted OAuth tokens
    access_token = models.TextField(null=False)
    refresh_token = models.TextField(null=True)
    token_expiry = models.DateTimeField(null=True)

    calendar_id = models.CharField(max_length=255, default="primary")
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="google_calendar_connections_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="google_calendar_connections_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Google Calendar: {self.user.username}"

    def get_access_token(self) -> str:
        """Get decrypted access token."""
        from utils.encryption import decrypt_token

        return decrypt_token(self.access_token)

    def set_access_token(self, token: str):
        """Set encrypted access token."""
        from utils.encryption import encrypt_token

        self.access_token = encrypt_token(token)

    def get_refresh_token(self) -> str | None:
        """Get decrypted refresh token."""
        if not self.refresh_token:
            return None
        from utils.encryption import decrypt_token

        return decrypt_token(self.refresh_token)

    def set_refresh_token(self, token: str | None):
        """Set encrypted refresh token."""
        if not token:
            self.refresh_token = None
            return
        from utils.encryption import encrypt_token

        self.refresh_token = encrypt_token(token)


class Insights(models.Model):
    """Model to store AI-generated insights analysis for a tenant."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="insights"
    )
    from_date = models.DateField(null=False)
    to_date = models.DateField(null=False)
    total_feedback_count = models.IntegerField(null=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="insights_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="insights_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Insights for {self.tenant.name} ({self.from_date} to {self.to_date})"


class InsightReport(models.Model):
    """Model to store individual insight reports generated by AI analysis."""

    PRIORITY_CHOICES = [
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    insights = models.ForeignKey(
        Insights, on_delete=models.RESTRICT, related_name="reports"
    )
    title = models.CharField(max_length=200, null=False)
    content = models.TextField(null=False)
    priority = models.CharField(
        max_length=10, choices=PRIORITY_CHOICES, default="low", null=False
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="insight_reports_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="insight_reports_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} ({self.priority})"


class Goal(models.Model):
    """
    Per-user, per-tenant, per-year goals (target values only).
    Current values are computed at query time from events and ConsumerEngagements.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name="goals")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name="goals",
    )
    year = models.IntegerField(null=False)

    # Target values (nullable so only set goals are stored)
    event_target_goal = models.IntegerField(null=True, blank=True)
    consumer_sampling_goal = models.IntegerField(null=True, blank=True)
    brand_awareness_goal = models.FloatField(null=True, blank=True)
    purchase_intent_goal = models.FloatField(null=True, blank=True)
    female_participation_goal = models.FloatField(null=True, blank=True)
    first_time_buyers_goal = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user", "year"],
                name="tenants_goal_tenant_user_year_uniq",
            )
        ]

    def __str__(self):
        return f"Goals {self.year} for user {self.user_id} @ tenant {self.tenant_id}"
