from uuid6 import uuid7
from django.db import models
from django.conf import settings

from ambassadors.models import Ambassador


class DocumentType(models.TextChoices):
    """Compliance document categories a BA can store."""
    GOVERNMENT_ID = "government_id", "Government ID"
    FOOD_HANDLER = "food_handler", "Food Handler Card"
    ALCOHOL_CERT = "alcohol_cert", "Alcohol Server Certification (TABC/etc.)"
    DRIVERS_LICENSE = "drivers_license", "Driver's License"
    CERTIFICATION = "certification", "Other Certification"
    OTHER = "other", "Other"


class DocumentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    EXPIRED = "expired", "Expired"
    ARCHIVED = "archived", "Archived"


class AmbassadorDocument(models.Model):
    """A compliance document a BA uploads to their vault.

    `file` stores the GCS blob PATH (not a signed URL) — mirrors
    recaps.RecapFile.file. The blob is uploaded by the mobile client via
    the existing getUploadUrl signed-URL flow, then the returned blobName
    is sent here. `expires_on` is nullable: some docs (e.g. a permanent
    ID scan) never expire.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.CASCADE,
        related_name="documents",
    )

    doc_type = models.CharField(
        max_length=32,
        choices=DocumentType.choices,
        default=DocumentType.OTHER,
    )
    # Optional human label, e.g. "TX Food Handler" — falls back to the
    # doc_type display name when blank.
    title = models.CharField(max_length=255, null=True, blank=True)

    # GCS blob path. Same convention as recaps.RecapFile.file.
    file = models.FileField(upload_to="documents/", max_length=1024, null=True)
    # Original client filename + mime, for display + correct download.
    original_filename = models.CharField(max_length=255, null=True, blank=True)
    content_type = models.CharField(max_length=128, null=True, blank=True)

    expires_on = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=DocumentStatus.choices,
        default=DocumentStatus.ACTIVE,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="documents_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="documents_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ambassador", "status"]),
            models.Index(fields=["expires_on"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_doc_type_display()} ({self.ambassador_id})"
