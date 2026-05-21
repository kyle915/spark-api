"""
One-off backfill: convert every existing HEIC-only recap file into a JPG
sibling so the recap-list cards have a renderable hero photo.

Usage
-----
    python manage.py backfill_heic_jpg_siblings           # dry-run, prints what it would do
    python manage.py backfill_heic_jpg_siblings --apply   # actually run the conversion
    python manage.py backfill_heic_jpg_siblings --apply --limit 10   # convert N then stop

Idempotent
----------
ensure_jpg_sibling() short-circuits when a JPG sibling already exists for
the recap at the expected blob path. Safe to re-run after a partial pass
(e.g. if you Ctrl-C halfway through).

Performance
-----------
Each conversion downloads the HEIC from GCS, decodes via libheif,
encodes to JPEG, and uploads. Expect ~1-3 s per file plus network — about
4 min total for the current 84-file backfill.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from recaps import models
from recaps.heic_conversion import (
    HEIC_EXTS,
    ensure_jpg_sibling,
    jpg_blob_name_for,
)


class Command(BaseCommand):
    help = "Backfill JPG siblings for existing HEIC recap files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Without this flag, the command is a dry-run.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process at most N HEIC files (default: all).",
        )

    def handle(self, *args, apply: bool = False, limit: int | None = None, **kwargs):
        # Match either case — iPhone uploads sometimes come in as .HEIC.
        ext_filters = Q()
        for ext in HEIC_EXTS:
            ext_filters |= Q(file__iendswith=ext)
        heic_qs = (
            models.RecapFile.objects.filter(ext_filters)
            .select_related("file_type", "file_recap_category", "created_by", "recap")
            .order_by("id")
        )

        total = heic_qs.count()
        self.stdout.write(self.style.NOTICE(f"Found {total} HEIC recap files."))

        # Skip rows where a sibling already exists. We do this cheaply
        # in Python by collecting the expected sibling blobs and
        # checking the DB for matches.
        skipped = 0
        converted = 0
        failed = 0
        processed = 0

        for rf in heic_qs.iterator(chunk_size=50):
            if not rf.recap_id:
                continue  # Orphaned file; skip.
            heic_blob = str(rf.file)
            expected_jpg = jpg_blob_name_for(heic_blob)
            existing = models.RecapFile.objects.filter(
                recap_id=rf.recap_id, file=expected_jpg
            ).exists()
            if existing:
                skipped += 1
                continue

            self.stdout.write(
                f"  [{processed + 1}/{total}] recap={rf.recap_id} "
                f"{heic_blob} → {expected_jpg}",
            )

            if apply:
                result = ensure_jpg_sibling(
                    heic_blob_name=heic_blob,
                    recap_id=rf.recap_id,
                    file_type=rf.file_type,
                    file_recap_category=rf.file_recap_category,
                    created_by=rf.created_by,
                )
                if result is not None:
                    converted += 1
                else:
                    failed += 1
                    self.stdout.write(
                        self.style.WARNING(f"    failed (see logs): {heic_blob}")
                    )

            processed += 1
            if limit and processed >= limit:
                break

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDRY RUN — would convert {processed - skipped} files "
                    f"({skipped} already have a sibling). Re-run with --apply."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nDone. converted={converted} skipped={skipped} "
                    f"failed={failed} total_scanned={processed}"
                )
            )
