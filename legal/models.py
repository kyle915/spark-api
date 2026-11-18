from django.conf import settings
from django.db import models
from django.utils import timezone


class LegalAcceptance(models.Model):
    """One person accepting one version of one legal document.

    Append-only audit record: a version bump adds new rows, it never edits
    old ones. ``email`` / ``role`` are snapshots at acceptance time so the
    record still reads correctly if the account is later renamed or demoted.
    Users are never hard-deleted in Spark (deleteUser soft-deactivates), and
    PROTECT keeps it that way for anyone with an acceptance on file.
    """

    DOCUMENT_TERMS = "terms"
    DOCUMENT_PRIVACY = "privacy"
    DOCUMENT_AUP = "aup"
    DOCUMENT_CHOICES = [
        (DOCUMENT_TERMS, "Terms of Service"),
        (DOCUMENT_PRIVACY, "Privacy Policy"),
        (DOCUMENT_AUP, "Acceptable Use Policy"),
    ]

    METHOD_CLICKWRAP = "clickwrap"
    METHOD_CHOICES = [
        (METHOD_CLICKWRAP, "Clickwrap (checkbox + Continue in the web app)"),
    ]

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="legal_acceptances",
    )
    document = models.CharField(max_length=16, choices=DOCUMENT_CHOICES)
    version = models.CharField(max_length=32)
    accepted_at = models.DateTimeField(default=timezone.now, db_index=True)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")
    method = models.CharField(
        max_length=16, choices=METHOD_CHOICES, default=METHOD_CLICKWRAP
    )
    email = models.CharField(max_length=254, blank=True, default="")
    role = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        ordering = ["-accepted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "document", "version"],
                name="legal_acceptance_unique_user_document_version",
            )
        ]
        indexes = [models.Index(fields=["document", "version"])]
        verbose_name = "Legal acceptance"
        verbose_name_plural = "Legal acceptances"

    def __str__(self) -> str:
        return f"{self.email or self.user_id} accepted {self.document} {self.version}"
