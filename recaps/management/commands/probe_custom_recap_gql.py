"""
Run the exact GraphQL query the frontend uses for a single CustomRecap
detail page, against the local Django ORM (in-process), and dump the
serialized response. Verifies whether the customRecapFiles list and
their .url fields are what the frontend expects.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from recaps import models as recap_models
from utils.gcs import public_url, extract_blob_name_from_url


class Command(BaseCommand):
    help = "Probe a CustomRecap's files as if served by GraphQL."

    def add_arguments(self, parser):
        parser.add_argument("uuid", type=str)

    def handle(self, *args, **opts):
        try:
            cr = recap_models.CustomRecap.objects.get(uuid=opts["uuid"])
        except recap_models.CustomRecap.DoesNotExist:
            self.stdout.write(self.style.ERROR("not found"))
            return

        self.stdout.write(f"CustomRecap uuid={cr.uuid} id={cr.id}")
        self.stdout.write(f"  approved={cr.approved}")
        self.stdout.write("")

        files = list(
            recap_models.CustomRecapFile.objects.filter(custom_recap=cr)
            .select_related("file_type", "file_recap_category")
            .order_by("id")
        )

        self.stdout.write(f"customRecapFiles[{len(files)}]:")
        for f in files[:10]:
            # Reproduce the exact url_str() resolver logic
            field_file = f.__dict__.get("url")
            if field_file is None:
                field_file = getattr(f, "url", None)
            if not field_file:
                url_out = None
            else:
                try:
                    blob = field_file.name
                except Exception:
                    blob = str(field_file)
                blob_name = extract_blob_name_from_url(blob)
                url_out = public_url(blob_name)

            ext = (f.file_type.name if f.file_type else "?")
            cat = (f.file_recap_category.name if f.file_recap_category else "?")
            self.stdout.write(f"  - id={f.id} ext={ext!r} cat={cat!r}")
            self.stdout.write(f"      raw_field    = {str(f.url)!r}")
            self.stdout.write(f"      resolved_url = {url_out!r}")

        if len(files) > 10:
            self.stdout.write(f"  ... and {len(files) - 10} more")
        self.stdout.write("")
        self.stdout.write("Done.")
