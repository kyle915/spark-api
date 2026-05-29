"""
One-off backfill: convert every existing HEIC recap file into a JPG
sibling so the recap views render a real photo instead of the
"HEIC / OPEN →" fallback tile.

Covers BOTH recap-file models:
  * legacy ``RecapFile``  → creates a sibling .jpg *DB row* (the recap-
    list hero picker scans for a renderable .jpg file row).
  * ``CustomRecapFile``   → creates a sibling .jpg *blob in GCS* only;
    the GraphQL ``displayUrl`` resolver rewrites a .heic path to the
    .jpg sibling when the blob exists, so no extra DB row is needed.

Usage
-----
    python manage.py backfill_heic_jpg_siblings              # dry-run, prints what it would do
    python manage.py backfill_heic_jpg_siblings --apply      # actually run the conversion
    python manage.py backfill_heic_jpg_siblings --apply --limit 10        # convert N then stop
    python manage.py backfill_heic_jpg_siblings --apply --model custom    # only CustomRecapFile
    python manage.py backfill_heic_jpg_siblings --apply --model legacy    # only RecapFile

Idempotent
----------
Both paths short-circuit when a JPG sibling already exists (a sibling
RecapFile row for legacy, or the sibling blob in GCS for custom). Safe
to re-run after a partial pass (e.g. Ctrl-C halfway through).

Performance
-----------
Each conversion downloads the HEIC from GCS, decodes via libheif,
encodes to JPEG, and uploads. Expect ~1-3 s per file plus network.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from recaps import models
from recaps.heic_conversion import (
    HEIC_EXTS,
    ensure_jpg_sibling,
    ensure_jpg_sibling_blob,
    jpg_blob_name_for,
)
from utils.gcs import blob_exists


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
            help="Process at most N HEIC files per model (default: all).",
        )
        parser.add_argument(
            "--model",
            choices=["all", "legacy", "custom"],
            default="all",
            help=(
                "Which model to backfill: 'legacy' (RecapFile), "
                "'custom' (CustomRecapFile), or 'all' (default)."
            ),
        )

    def _heic_q(self, field: str) -> Q:
        # Match either case — iPhone uploads sometimes come in as .HEIC.
        q = Q()
        for ext in HEIC_EXTS:
            q |= Q(**{f"{field}__iendswith": ext})
        return q

    def handle(
        self,
        *args,
        apply: bool = False,
        limit: int | None = None,
        model: str = "all",
        **kwargs,
    ):
        if model in ("all", "legacy"):
            self._backfill_legacy(apply=apply, limit=limit)
        if model in ("all", "custom"):
            self._backfill_custom(apply=apply, limit=limit)

    def _backfill_legacy(self, *, apply: bool, limit: int | None):
        heic_qs = (
            models.RecapFile.objects.filter(self._heic_q("file"))
            .select_related("file_type", "file_recap_category", "created_by", "recap")
            .order_by("id")
        )

        total = heic_qs.count()
        self.stdout.write(
            self.style.NOTICE(f"[legacy RecapFile] Found {total} HEIC recap files.")
        )

        skipped = converted = failed = processed = 0

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
                f"  [legacy {processed + 1}/{total}] recap={rf.recap_id} "
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

        self._report("legacy RecapFile", apply, processed, skipped, converted, failed)

    def _backfill_custom(self, *, apply: bool, limit: int | None):
        heic_qs = (
            models.CustomRecapFile.objects.filter(self._heic_q("url"))
            .order_by("id")
        )

        total = heic_qs.count()
        self.stdout.write(
            self.style.NOTICE(
                f"[CustomRecapFile] Found {total} HEIC custom recap files."
            )
        )

        skipped = converted = failed = processed = 0

        for rf in heic_qs.iterator(chunk_size=50):
            heic_blob = str(rf.url)
            if not heic_blob:
                continue
            expected_jpg = jpg_blob_name_for(heic_blob)
            # Sibling lives as a GCS blob (no DB row for custom files).
            if blob_exists(expected_jpg):
                skipped += 1
                continue

            self.stdout.write(
                f"  [custom {processed + 1}/{total}] custom_recap={rf.custom_recap_id} "
                f"{heic_blob} → {expected_jpg}",
            )

            if apply:
                result = ensure_jpg_sibling_blob(heic_blob)
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

        self._report("CustomRecapFile", apply, processed, skipped, converted, failed)

    def _report(self, label, apply, processed, skipped, converted, failed):
        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"[{label}] DRY RUN — would convert {processed - skipped} files "
                    f"({skipped} already have a sibling). Re-run with --apply."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{label}] Done. converted={converted} skipped={skipped} "
                    f"failed={failed} total_scanned={processed}"
                )
            )
