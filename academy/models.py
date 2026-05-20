from uuid6 import uuid7
from django.db import models
from django.conf import settings

from tenants.models import Tenant


class AcademyModule(models.Model):
    """A training/brand/playbook content module shown to Brand
    Ambassadors in the mobile Academy tab.

    Kept lightweight intentionally: title + free-form markdown body +
    a `kind` discriminator so the mobile app can render different
    chip colors per category (training vs. brand vs. playbook etc.).
    File uploads land in a sibling table later — for v1, embed image
    URLs inline in the markdown body.
    """

    KIND_CHOICES = [
        ("training", "Training"),
        ("brand", "Brand"),
        ("playbook", "Playbook"),
        ("policy", "Policy"),
        ("announcement", "Announcement"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        related_name="academy_modules",
    )

    title = models.CharField(max_length=200, null=False)
    kind = models.CharField(
        max_length=24,
        choices=KIND_CHOICES,
        default="training",
    )
    # Markdown content rendered on the mobile Academy tab.
    body = models.TextField(blank=True, default="")

    # Sort order within the academy list. Lower = higher on screen.
    # Defaults to 0 so newly-created modules float to the top.
    order = models.IntegerField(default=0)

    # When false, the module is invisible to BAs (admin draft). The
    # mobile `academyModules` query filters by published=True; the
    # admin-only `academyModulesAdmin` query returns everything.
    published = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="academy_modules_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="academy_modules_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "published", "order"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug only
        return f"{self.title} ({self.kind})"
