import re

from uuid6 import uuid7
from asgiref.sync import sync_to_async

from django.utils.text import slugify
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings

from .managers import UserManager, TenantedUserManager, TenantManager
from utils.models import Asyncable
from utils.utils import default_tenant_theme


# Ops retires a client by renaming its tenant with an "[ARCHIVED]" prefix —
# there is no boolean flag on the model, the rename IS the convention.
# Tenant.active() applies it everywhere (tenant pickers, client lists, digest
# crons) so a dead client stops appearing in the UI and stops getting email.
# Reversible: rename the tenant back to un-archive it.
ARCHIVED_NAME_PREFIX = "[ARCHIVED]"


def parse_recipient_emails(raw: str | None) -> list[str]:
    """Parse a free-text recipient field into a deduped list of emails.

    Splits ``raw`` on commas / newlines / semicolons (the format
    ``Tenant.recap_recipient_emails`` documents), keeps only tokens that look
    like an email (contain ``@`` and ``.`` — the SAME loose check
    ``recaps.mutations._notify_recap_approved_to_rmm_or_clients`` uses), and
    de-dupes case-insensitively while preserving first-seen order. Returns
    ``[]`` for empty/None input. Shared so the scheduled-report cron and the
    recap-approval path agree on what a "recipient list" means.
    """
    seen: set[str] = set()
    out: list[str] = []
    for token in re.split(r"[,\n;]+", raw or ""):
        candidate = token.strip()
        if "@" in candidate and "." in candidate:
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


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
    # When set, ALL external (public-form) requests for this tenant route
    # to this user as the assigned RMM/approver, overriding territory
    # logic. Chosen on the Team page. SET_NULL so removing the user from
    # the tenant doesn't break request creation.
    default_external_rmm = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_external_rmm_for_tenants",
    )
    # Explicit email addresses that should receive recap-approval emails
    # for this brand, on top of the RMM, the tenant's client-role users,
    # and the original requestor. Lets staff route approved recaps to a
    # brand contact even when that brand has no client-role user set up.
    # Free text (comma/newline/semicolon-separated) — parsed at send time
    # in recaps.mutations._notify_recap_approved_to_rmm_or_clients.
    recap_recipient_emails = models.TextField(
        blank=True,
        default="",
        help_text="Extra email addresses (comma/newline/semicolon-separated) that receive recap-approval emails for this brand, in addition to the RMM, client-role users, and requestor.",
    )
    # Opt-in switch for the scheduled monthly client performance report
    # (the `send_scheduled_client_reports` cron). SAFE DEFAULT: OFF — a
    # tenant is NEVER emailed a scheduled report until Ignite explicitly
    # flips this on, so deploying the feature mails nobody. The report's
    # recipients reuse `recap_recipient_emails` (the same client contacts
    # the recap-approval emails go to); see `scheduled_report_recipients()`.
    scheduled_report_enabled = models.BooleanField(
        default=False,
        help_text="When ON, this brand receives an automated monthly performance-report PDF by email (sent to recap_recipient_emails). OFF by default — opt-in only.",
    )
    # Opt-in switch for the WEEKLY client digest (the
    # `send_client_weekly_digest` cron). Its own flag — independent from the
    # monthly report above — so Ignite can roll each out per tenant
    # separately; the digest previously piggybacked on
    # `scheduled_report_enabled`, and the migration copies that value so
    # tenants already receiving it keep receiving it. Recipients reuse
    # `scheduled_report_recipients()`. SAFE DEFAULT: OFF.
    client_weekly_digest_enabled = models.BooleanField(
        default=False,
        help_text="When ON, this brand receives the weekly field-marketing digest email (sent to recap_recipient_emails). OFF by default — opt-in only.",
    )
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

    @classmethod
    def active(cls):
        """Tenants that haven't been archived-by-convention.

        Excludes any tenant whose name starts with the "[ARCHIVED]" prefix.
        Use this anywhere a dead client should not surface (tenant pickers,
        client lists, scheduled digests). Call Tenant.objects to include
        archived tenants on purpose (Django admin, explicit single-tenant
        operations).
        """
        return cls.objects.exclude(name__istartswith=ARCHIVED_NAME_PREFIX)

    @property
    def is_archived(self) -> bool:
        """True when this tenant was archived by the "[ARCHIVED]" rename."""
        return (self.name or "").upper().startswith(ARCHIVED_NAME_PREFIX)

    def scheduled_report_recipients(self) -> list[str]:
        """The client email addresses the scheduled monthly report goes to.

        REUSES ``recap_recipient_emails`` (the same client contacts the
        recap-approval emails reach) rather than introducing a second list to
        keep in sync — parsed/deduped via :func:`parse_recipient_emails`.
        Returns ``[]`` when no recipients are configured; the cron uses an
        empty result to SKIP a tenant (no recipients → nothing sent), which —
        combined with ``scheduled_report_enabled`` defaulting to ``False`` —
        is what makes the feature mail nobody until it is explicitly set up.
        """
        return parse_recipient_emails(self.recap_recipient_emails)


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
    # Flipped True by admin-created flows (createAmbassadorWithUser
    # with an admin-set temp password). Mobile uses this to force the
    # user through ChangePasswordScreen on first sign-in instead of
    # letting them into the app with a credential the admin chose for
    # them. Cleared on successful changeUserPassword.
    requires_password_change = models.BooleanField(default=False)

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


class TenantInsightSnapshot(models.Model):
    """A cached set of proactive "what's notable" AI insights for a tenant.

    Distinct from :class:`Insights` / :class:`InsightReport` (which analyze
    ConsumerFeedback text over a date range): this is the server-side cache
    for the dashboard's PROACTIVE insights — a small list of auto-generated
    headline observations about the client's whole program, surfaced without
    the user asking. Each snapshot is one generation; the newest one younger
    than the read freshness window is served, and a daily cron precomputes a
    fresh snapshot so dashboard reads stay fast (see
    :func:`recaps.tenant_insights.get_or_refresh_tenant_insights`).

    ``items`` is the parsed list of insight dicts straight off the model
    (``{title, detail, sentiment, metric}`` each); it is stored verbatim so
    the GraphQL layer can shape it without a second model table.
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="insight_snapshots"
    )
    generated_at = models.DateTimeField(auto_now_add=True, db_index=True)
    items = models.JSONField(default=list)

    def __str__(self) -> str:
        return f"Insight snapshot for tenant {self.tenant_id} @ {self.generated_at}"


class TenantSentimentSnapshot(models.Model):
    """A cached "What people are saying" consumer-sentiment read for a tenant.

    The AI-backed sibling of :class:`TenantInsightSnapshot` (which caches the
    now-deterministic proactive buckets): this stores the OpenAI-summarized
    consumer sentiment for a tenant's free-text recap feedback — an overall
    sentiment, a positive-percentage estimate, a one-line summary, the
    recurring themes, and a few verbatim quotes. Because the read costs an
    OpenAI call, it is cached here and refreshed at most daily; the newest
    snapshot younger than the read freshness window is served, and a daily cron
    precomputes a fresh one so dashboard reads stay fast (see
    :func:`recaps.tenant_sentiment.get_or_refresh_tenant_sentiment`).

    ``payload`` is the cleaned structured dict straight off
    :func:`recaps.tenant_sentiment.build_tenant_sentiment`
    (``{overall_sentiment, positive_pct, summary, themes, quotes}``), stored
    verbatim so the GraphQL layer can shape it without a second table.
    ``sample_size`` is the number of feedback snippets the summary was built
    from. ``year`` partitions the cache: ``None`` is the all-time snapshot, an
    integer is that calendar year's snapshot (mirrors the ``year`` argument the
    tenant aggregates accept), so per-year and all-time reads never collide.

    NOTE on ``related_name``: :class:`TenantInsightSnapshot` already owns
    ``Tenant.insight_snapshots``; two FKs to ``Tenant`` cannot share one
    reverse accessor (Django ``fields.E304``), so this uses
    ``related_name="sentiment_snapshots"`` to stay distinct (the
    ``TenantGoal.kpi_goals`` lesson).
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="sentiment_snapshots"
    )
    # All-time when null; otherwise the calendar year this snapshot summarizes.
    year = models.IntegerField(null=True, blank=True, db_index=True)
    payload = models.JSONField(default=dict)
    sample_size = models.IntegerField(default=0)
    generated_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self) -> str:
        scope = "all-time" if self.year is None else str(self.year)
        return (
            f"Sentiment snapshot ({scope}) for tenant {self.tenant_id} "
            f"@ {self.generated_at}"
        )


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


class UserPreference(models.Model):
    """Per-user Settings preferences, persisted server-side.

    Backs the web Settings page (``SparkSettings.tsx``), which previously
    kept these UI prefs only in ``localStorage`` (under ``@spark.settings.*``)
    so they did not follow the user across devices/browsers. One row per
    user.

    ``prefs`` is a free-form JSON blob (not typed columns) so adding a new
    Settings toggle later needs no migration — the GraphQL layer owns the
    shape. The keys we mirror today (see ``DEFAULT_PREFS``):

    * ``timezone``    — IANA tz string (default ``"America/Chicago"``).
    * ``currency``    — display currency label (default ``"USD ($)"``).
    * ``activations`` — map of activation-type id -> enabled bool
      (default ``{"retail": True, "onprem": True, "event": True}``).

    Reads merge stored values over ``DEFAULT_PREFS`` so a user who has never
    saved — or who is missing a newly added key — still gets sane defaults.
    """

    # Source-of-truth defaults, mirrored from SparkSettings.tsx's
    # localStorage fallbacks. Kept here so both the GraphQL resolver and any
    # future server-side reader agree on the baseline.
    DEFAULT_PREFS: dict = {
        "timezone": "America/Chicago",
        "currency": "USD ($)",
        "activations": {"retail": True, "onprem": True, "event": True},
    }

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preference",
    )
    prefs = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Preferences for user {self.user_id}"

    def merged(self) -> dict:
        """Stored prefs layered over :attr:`DEFAULT_PREFS`.

        Defaults fill any key the user has never saved (or that was added
        after they last saved), so reads always return a complete object.
        """
        base = dict(self.DEFAULT_PREFS)
        if isinstance(self.prefs, dict):
            base.update(self.prefs)
        return base


class CustomForm(models.Model):
    """A tenant-scoped custom form DEFINITION built in the web Form Builder.

    Backs the web Form Builder page (``SparkFormBuilder.tsx``), which
    previously kept every form definition only in ``localStorage`` (under
    ``@spark.formBuilder/<tenantId>``) — so a cache-clear lost them and they
    never synced across devices or teammates. One row per built form.

    ``schema`` is the whole field-definition blob straight off the builder
    (NOT typed columns) so the builder can grow new field kinds / settings
    without a backend migration each time — the front-end owns the shape.
    Today the builder stores (see ``FormDef`` in ``SparkFormBuilder.tsx``):

    * ``description`` (str) — what the form is for.
    * ``internal`` (bool) — publish to the internal ``/requests/create`` queue.
    * ``external`` (bool) — publish to the public ``/spark-form/<slug>`` link.
    * ``fields`` (list) — ordered field defs, each
      ``{id, label, kind, required, helpText?, options?}`` where ``kind`` is
      one of text/longtext/number/email/date/time/select/checkbox/file and
      ``options`` is the choice list for ``select``.

    The builder's own ``id`` / ``name`` / ``updatedAt`` map onto this row's
    ``pk`` / :attr:`name` / :attr:`updated_at`; everything else lives in
    ``schema`` verbatim, so a saved blob round-trips through the GraphQL layer
    unchanged. SUBMISSIONS (people filling out a published form) are a
    separate, future concern — this model persists DEFINITIONS only.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="custom_forms",
    )
    name = models.CharField(max_length=255)
    # The whole builder field-definition blob (description, internal/external
    # flags, ordered fields). Free-form so the builder can evolve without a
    # migration; the GraphQL layer / front-end own the shape.
    schema = models.JSONField(default=dict, blank=True)
    # Whether this definition has been published (made available to the
    # internal/external request surfaces). Defaults False — a freshly built
    # form is a draft until explicitly published.
    is_published = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="custom_forms_created",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Newest-first is the builder's grid order (it prepends new forms).
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "-updated_at"]),
        ]

    def __str__(self) -> str:
        return f"CustomForm '{self.name}' @ tenant {self.tenant_id}"


class SupportTicket(models.Model):
    """A support request submitted from the web Help page (``SparkHelp.tsx``).

    Backs the Help page's "How can we help?" form. Previously that page was
    fully static (FAQs + mailto links); this captures the request as a row so
    nothing relies on a user actually opening their mail client, and so we keep
    a record. On create, the ``createSupportTicket`` mutation also notifies the
    Ignite team by REUSING the same recipient resolution the request-approval
    email uses (``events/mutations.py`` — ``IGNITE_REVIEW_CC`` + active
    spark-admins + ``REQUEST_REVIEW_COPY_EMAILS``).

    ``tenant`` is nullable: a signed-in user without a bound tenant can still
    file a ticket (we just notify Ignite without a brand name). ``created_by``
    is the authenticated submitter; ``SET_NULL`` so deleting a user doesn't
    erase the support history.
    """

    # Category choices — kept loose (a plain CharField with choices for the
    # admin) so the front-end can offer a dropdown without a migration per new
    # bucket. Defaults to "other" when the form omits it.
    CATEGORY_QUESTION = "question"
    CATEGORY_BUG = "bug"
    CATEGORY_BILLING = "billing"
    CATEGORY_OTHER = "other"
    CATEGORY_CHOICES = [
        (CATEGORY_QUESTION, "Question"),
        (CATEGORY_BUG, "Bug"),
        (CATEGORY_BILLING, "Billing"),
        (CATEGORY_OTHER, "Other"),
    ]

    # Status lifecycle — "open" on create; "closed"/"resolved" are for a future
    # admin triage surface. Loose CharField for the same reason as category.
    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="support_tickets",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_tickets_created",
    )
    subject = models.CharField(max_length=255)
    body = models.TextField()
    category = models.CharField(
        max_length=32,
        choices=CATEGORY_CHOICES,
        default=CATEGORY_OTHER,
        blank=True,
    )
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Newest-first for an admin triage list.
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"SupportTicket #{self.pk} '{self.subject}' @ tenant {self.tenant_id}"


class TenantGoal(models.Model):
    """Per-CLIENT (tenant-level), per-year KPI targets for the headline KPIs.

    The client-level sibling of :class:`Goal` (which stores per-USER targets
    for the team dashboard). One row per (tenant, year) holds the brand's
    annual targets for the four headline KPIs the report surface tracks.
    Pace-to-goal is computed at query time by comparing each target against
    the live actuals from
    :func:`recaps.tenant_overview.tenant_kpi_totals` (year-filtered), so no
    "current" value is stored here.

    NOTE on ``related_name``: the spec asked for ``related_name="goals"``,
    but :class:`Goal` already owns ``Tenant.goals`` (its per-user reverse
    accessor). Two FKs to ``Tenant`` cannot share one reverse accessor
    (Django ``fields.E304``), so this uses ``related_name="kpi_goals"`` to
    keep ``manage.py check`` green while leaving the existing per-user
    ``Goal`` accessor untouched.
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="kpi_goals",
    )
    year = models.IntegerField()

    # Annual targets for the four headline KPIs (0 = no target set). These
    # mirror the like-named fields on
    # :class:`recaps.tenant_overview.TenantKpiTotals`, which supplies the
    # matching "current" actuals at query time.
    target_consumers_reached = models.IntegerField(default=0)
    target_samples_distributed = models.IntegerField(default=0)
    target_products_sold = models.IntegerField(default=0)
    target_total_engagements = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("tenant", "year")

    def __str__(self) -> str:
        return f"KPI goals {self.year} @ tenant {self.tenant_id}"
