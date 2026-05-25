"""
Audit: walk every RecapFile + CustomRecapFile path in the DB and HEAD
each against the GCS bucket. Prints a count of missing blobs plus a
sample of paths so we can decide whether the orphan list is bridgeable
from another bucket or needs recovery from legacy infrastructure.

Run from Cloud Run (where the service account is already
authenticated) — local runs need ADC reauth which is a pain.

    python manage.py audit_recap_blobs [--tenant-id 1] [--limit 1000]

Outputs to stdout — capture via `gcloud run jobs execute --wait` log
stream, or just `print(...)` results.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand

from recaps import models as recap_models

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Audit DB-referenced recap blob paths against the live GCS bucket."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=int,
            default=None,
            help="Limit audit to a single tenant.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Cap on rows scanned per table (debug runs).",
        )
        parser.add_argument(
            "--show-missing",
            type=int,
            default=20,
            help="How many 404 paths to dump in the report.",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=20,
            help="Concurrent HEAD requests.",
        )

    def handle(self, *args, **opts):
        tenant_id = opts["tenant_id"]
        limit = opts["limit"]
        show_missing = opts["show_missing"]
        workers = opts["workers"]

        bucket = getattr(settings, "GS_BUCKET_NAME", "")
        if not bucket:
            self.stdout.write(self.style.ERROR("GS_BUCKET_NAME not set."))
            return
        base = f"https://storage.googleapis.com/{bucket}/"

        # Build the union of paths from both file tables. RecapFile.file
        # is a FileField; we coerce to its string form (blob name).
        # CustomRecapFile.url stores the path directly as a CharField.
        def _normalize(raw):
            if not raw:
                return None
            s = str(raw).strip()
            if not s:
                return None
            # Strip any leading slash + leading bucket prefix if a full
            # URL was stored.
            if s.startswith("http://") or s.startswith("https://"):
                # Pull blob name out of full URL.
                marker = f"/{bucket}/"
                idx = s.find(marker)
                if idx >= 0:
                    s = s[idx + len(marker):]
                else:
                    return None  # different host — caller's problem
            return s.lstrip("/")

        rf_qs = recap_models.RecapFile.objects.all()
        if tenant_id:
            rf_qs = rf_qs.filter(recap__event__tenant_id=tenant_id)
        if limit:
            rf_qs = rf_qs[:limit]
        recap_paths = [
            (rf.id, _normalize(rf.file), "RecapFile") for rf in rf_qs
        ]

        crf_qs = recap_models.CustomRecapFile.objects.all()
        if tenant_id:
            crf_qs = crf_qs.filter(custom_recap__tenant_id=tenant_id)
        if limit:
            crf_qs = crf_qs[:limit]
        custom_paths = [
            (crf.id, _normalize(crf.url), "CustomRecapFile") for crf in crf_qs
        ]

        rows = recap_paths + custom_paths
        # Drop blanks.
        rows = [(rid, p, tbl) for (rid, p, tbl) in rows if p]

        self.stdout.write(
            f"Auditing {len(rows)} blob paths against gs://{bucket}/ "
            f"(tenant_id={tenant_id or 'ALL'}, limit={limit or 'NONE'})…"
        )

        def _head(item):
            rid, path, tbl = item
            url = base + path
            req = urllib.request.Request(url, method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return (rid, path, tbl, resp.status)
            except urllib.error.HTTPError as e:
                return (rid, path, tbl, e.code)
            except Exception:
                return (rid, path, tbl, -1)

        missing = []
        ok_count = 0
        error_count = 0
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for rid, path, tbl, status in pool.map(_head, rows):
                if status == 200:
                    ok_count += 1
                elif status == 404:
                    missing.append((rid, path, tbl))
                else:
                    error_count += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== AUDIT RESULTS ==="))
        self.stdout.write(f"  Total paths scanned: {len(rows)}")
        self.stdout.write(self.style.SUCCESS(f"  200 OK:               {ok_count}"))
        if missing:
            self.stdout.write(
                self.style.ERROR(f"  404 MISSING:         {len(missing)}")
            )
        else:
            self.stdout.write(f"  404 MISSING:         {len(missing)}")
        self.stdout.write(f"  Other errors:        {error_count}")

        if missing:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"=== First {min(show_missing, len(missing))} missing paths ==="
                )
            )
            for rid, path, tbl in missing[:show_missing]:
                self.stdout.write(f"  {tbl}#{rid}  {path}")
            if len(missing) > show_missing:
                self.stdout.write(
                    f"  …and {len(missing) - show_missing} more"
                )

            # Breakdown by tenant if no tenant filter.
            if not tenant_id:
                from collections import Counter
                # Try to attribute back to tenant via FK chain — best
                # effort, swallow exceptions.
                tenant_counter: Counter[int] = Counter()
                for rid, path, tbl in missing:
                    try:
                        if tbl == "RecapFile":
                            tid = recap_models.RecapFile.objects.values_list(
                                "recap__event__tenant_id", flat=True,
                            ).get(id=rid)
                        else:
                            tid = recap_models.CustomRecapFile.objects.values_list(
                                "custom_recap__tenant_id", flat=True,
                            ).get(id=rid)
                        if tid:
                            tenant_counter[tid] += 1
                    except Exception:
                        pass
                if tenant_counter:
                    self.stdout.write("")
                    self.stdout.write(
                        self.style.WARNING("=== Missing by tenant_id ===")
                    )
                    for tid, n in tenant_counter.most_common():
                        self.stdout.write(f"  tenant_id={tid}: {n} missing")

        self.stdout.write("")
        self.stdout.write("Done.")
