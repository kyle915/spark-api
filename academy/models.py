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


class TrainingHub(models.Model):
    """A shareable, no-login training page for one brand's BAs.

    ``AcademyModule`` above is the *in-app* Academy tab: it needs a login,
    a Spark account, and a tenant membership. A brand's BAs are frequently
    none of those things at the moment training matters — they're hired for
    one campaign, they get a text with a link, and they need the guide, the
    video and the product sheets before their first shift. So this is the
    same posture as the web check-in link (``Event.walkup_code``) and the
    tokenized client-live page: possession of the ``code`` in the URL is the
    only credential, and the page is read-only, so there's nothing to leak
    beyond the training material the brand wants every BA to have.

    One hub per brand per campaign; resources hang off it in ``order``.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        related_name="training_hubs",
    )

    # The shareable credential. Brand-prefixed and human-readable so it
    # survives being read aloud or retyped off a text message: "LD-4KX9T2".
    code = models.CharField(max_length=32, unique=True, db_index=True)

    title = models.CharField(max_length=200)
    subtitle = models.CharField(max_length=300, blank=True, default="")
    # Short markdown-free welcome paragraph above the resource list.
    intro = models.TextField(blank=True, default="")

    # Turning this off 404s the link without deleting the content, so a
    # finished campaign's link stops working but stays re-openable.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant_id", "title"]

    def __str__(self) -> str:  # pragma: no cover - debug only
        return f"{self.title} [{self.code}]"


class TrainingResource(models.Model):
    """One item on a :class:`TrainingHub` — a video, a PDF, or a link.

    ``kind`` drives how the page renders the item, not where it's stored:
    every resource is ultimately a URL. Video URLs are expected to be
    embeddable (YouTube/Vimeo) rather than raw MP4s — a 100MB file served
    flat has no adaptive bitrate, so a BA on store LTE waits minutes and
    gets no seeking, while an unlisted YouTube embed streams at whatever
    their connection supports.
    """

    KIND_CHOICES = [
        ("video", "Video"),
        ("pdf", "PDF"),
        ("link", "Link"),
        ("page", "Page"),
    ]

    id = models.BigAutoField(primary_key=True)

    hub = models.ForeignKey(
        TrainingHub,
        on_delete=models.CASCADE,
        related_name="resources",
    )

    order = models.IntegerField(default=0)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="link")

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    # Where the resource lives. Relative paths ("/training/ld/faq.pdf") are
    # served by the front-end host; absolute URLs point anywhere.
    url = models.CharField(max_length=500, blank=True, default="")

    # Free-form badge shown on the card — "12 min", "18 pages", "Website".
    meta_label = models.CharField(max_length=60, blank=True, default="")

    # A resource can be present but not yet ready (e.g. the video is still
    # uploading). Unpublished resources are hidden from the public page.
    published = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["hub", "published", "order"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug only
        return f"{self.title} ({self.kind})"
