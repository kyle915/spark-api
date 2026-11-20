"""One row of tags per recap photo.

Written once by the tagging pass (or by the best-photo scorer, from the
same vision call) and read by the photo library search. A photo is tagged
again only when its file changes: ``source_path`` and ``source_sha`` are the
file's stored path and content hash when it was tagged.

Nothing here decides who may see a photo. The search applies the rules at
read time from the recap (approved, not archived, live request, the
caller's own brand), so a recap unapproved after tagging drops out at once.
Nothing about a person is stored beyond ``people_present`` (yes or no) and
``crowd_size`` (a rough count bucket).
"""

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from tenants.models import Tenant


class RecapPhotoTag(models.Model):
    STATUS_TAGGED = "tagged"
    # Set aside without a model call, or the model said receipt / screenshot.
    STATUS_EXCLUDED = "excluded"
    # The file is gone or is not a picture; tried again only if it changes.
    STATUS_UNREADABLE = "unreadable"

    id = models.BigAutoField(primary_key=True)
    custom_recap_file = models.OneToOneField(
        "recaps.CustomRecapFile", on_delete=models.CASCADE, related_name="library_tag"
    )
    # Copied from the recap for fast filtering; the search also checks the
    # recap's own tenant, so a moved recap can never leak.
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="recap_photo_tags"
    )
    status = models.CharField(max_length=12, default=STATUS_TAGGED, db_index=True)
    excluded_reason = models.CharField(max_length=24, blank=True, default="")

    shows = ArrayField(models.CharField(max_length=24), blank=True, default=list)
    product_ids = ArrayField(models.BigIntegerField(), blank=True, default=list)
    product_names = ArrayField(models.CharField(max_length=80), blank=True, default=list)
    people_present = models.BooleanField(null=True, blank=True)
    crowd_size = models.CharField(max_length=8, blank=True, default="")
    setting = models.CharField(max_length=16, blank=True, default="")
    issues = ArrayField(models.CharField(max_length=16), blank=True, default=list)
    quality_score = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    caption = models.CharField(max_length=140, blank=True, default="")
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    dhash = models.CharField(max_length=16, blank=True, default="")

    # What the tags were read from. A change to either means tag again.
    source_path = models.CharField(max_length=1024, blank=True, default="")
    source_sha = models.CharField(max_length=64, blank=True, default="")
    # ``vision`` = one model call for this photo; ``copied`` = same file or a
    # near copy of a tagged photo; ``rules`` = no call (receipt bucket, gone).
    origin = models.CharField(max_length=8, blank=True, default="")
    # The model's raw answer (no caption), so the best-photo scorer can reuse
    # it instead of calling again.
    vision = models.JSONField(blank=True, default=dict)

    search_text = models.TextField(blank=True, default="")
    search_vector = SearchVectorField(null=True, blank=True)
    tagged_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            GinIndex(fields=["search_vector"], name="photo_lib_search_gin"),
            GinIndex(fields=["shows"], name="photo_lib_shows_gin"),
            GinIndex(fields=["product_ids"], name="photo_lib_products_gin"),
            models.Index(fields=["tenant", "status"], name="photo_lib_tenant_status"),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"file {self.custom_recap_file_id}: {self.status} {','.join(self.shows)}"
