from uuid6 import uuid7
from django.db import models
from django.conf import settings

from tenants.models import Tenant


class Announcement(models.Model):
    """A broadcast message from a tenant admin to that tenant's Brand
    Ambassadors. Shown in the mobile Announcements feed; a push is
    fanned out to every active BA in the tenant at create time.

    Kept lightweight (title + body + audience discriminator) like
    AcademyModule. `audience` is forward-compatible — v1 only fans out
    AUDIENCE_ALL_BAS (every active BA in the tenant), but the column
    lets us add role/subset targeting later without a migration.

    Read-state is NOT tracked per-row here (no AnnouncementRead join
    table in v1). The mobile unread badge compares the newest
    published_at against a locally-stored "last seen" timestamp — same
    cheap approach the app already uses elsewhere. A read-receipts
    table is a clean follow-up if we ever need per-BA delivery proof.
    """

    AUDIENCE_ALL_BAS = "all_bas"
    AUDIENCE_CHOICES = (
        (AUDIENCE_ALL_BAS, "All Brand Ambassadors in the tenant"),
    )

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="announcements",
    )

    title = models.CharField(max_length=200, null=False)
    body = models.TextField(blank=True, default="")

    audience = models.CharField(
        max_length=24,
        choices=AUDIENCE_CHOICES,
        default=AUDIENCE_ALL_BAS,
    )

    # Non-null = visible to BAs. Set at create time (announcements
    # publish immediately in v1). Nullable so a future "draft" path
    # can create unpublished rows without a schema change.
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="announcements_created_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-published_at", "-created_at")
        indexes = [
            models.Index(fields=["tenant", "published_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug only
        return f"{self.title} ({self.tenant_id})"
