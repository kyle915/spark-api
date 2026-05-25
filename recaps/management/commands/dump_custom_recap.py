"""
Diagnostic: dump every file attached to a CustomRecap by UUID, with
each file's URL, type, and live GCS HTTP status. Used when "the
pictures look broken" for one specific recap and we need to know
whether it's missing data, bad URLs, or a render-time problem.

    python manage.py dump_custom_recap 019e46c1-4cb5-7e5f-86e7-c651de93b549
"""
from __future__ import annotations

import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand

from recaps import models as recap_models


class Command(BaseCommand):
    help = "Dump a CustomRecap's files + live GCS status."

    def add_arguments(self, parser):
        parser.add_argument("uuid", type=str)

    def handle(self, *args, **opts):
        bucket = settings.GS_BUCKET_NAME
        base = f"https://storage.googleapis.com/{bucket}/"

        try:
            cr = recap_models.CustomRecap.objects.select_related(
                "event", "event__tenant", "ambassador", "ambassador__user"
            ).get(uuid=opts["uuid"])
        except recap_models.CustomRecap.DoesNotExist:
            self.stdout.write(self.style.ERROR("CustomRecap not found"))
            return

        self.stdout.write(self.style.SUCCESS(f"CustomRecap #{cr.id} ({cr.uuid})"))
        self.stdout.write(f"  name:    {cr.name}")
        self.stdout.write(f"  tenant:  #{cr.event.tenant_id} {cr.event.tenant.name if cr.event and cr.event.tenant else ''}")
        self.stdout.write(f"  event:   #{cr.event_id} {cr.event.name if cr.event else ''}")
        ba_name = "-"
        if cr.ambassador and cr.ambassador.user:
            u = cr.ambassador.user
            ba_name = f"{u.first_name} {u.last_name} <{u.email}>"
        self.stdout.write(f"  BA:      {ba_name}")
        self.stdout.write(f"  approved:{cr.approved}")
        self.stdout.write("")

        files = list(
            recap_models.CustomRecapFile.objects.filter(custom_recap=cr)
            .select_related("file_type", "file_recap_category")
            .order_by("id")
        )
        self.stdout.write(
            self.style.SUCCESS(f"  {len(files)} CustomRecapFile rows")
        )
        for f in files:
            ext_name = (f.file_type.name if f.file_type else "?")
            cat = (f.file_recap_category.name if f.file_recap_category else "?")
            # CustomRecapFile.url is a FileField → coerce to str (blob
            # path), not the FieldFile descriptor itself.
            raw = f.url
            url_stored = (str(raw) if raw else "").strip()
            # Build the URL the same way the frontend does
            if url_stored.startswith("http://") or url_stored.startswith("https://"):
                public_url = url_stored
            else:
                public_url = base + url_stored.lstrip("/")
            try:
                req = urllib.request.Request(public_url, method="HEAD")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    code = resp.status
                    ctype = resp.headers.get("content-type", "")
            except urllib.error.HTTPError as e:
                code = e.code
                ctype = "-"
            except Exception as e:
                code = -1
                ctype = str(e)[:40]

            self.stdout.write(
                f"  #{f.id:>5}  [{ext_name:>5}|{cat[:8]:>8}]  {code}  {ctype:<20}  {url_stored}"
            )

        self.stdout.write("")
        self.stdout.write("Done.")
