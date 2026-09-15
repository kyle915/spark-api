"""Render one recap to PDF, upload it, and attach it as the Spark PDF row.

Exactly what the "Download PDF" / generateCustomRecapPdf path produces — this
calls ``_render_and_store_recap_pdf_sync(force=True)`` so the CustomRecapFile
(or RecapFile) row points at a real GCS object. The older version of this
command uploaded a blob but left the DB row untouched, so Download PDF kept
opening a stale NoSuchKey URL.

It exists because that button is a GraphQL mutation behind a login and prod
isn't always reachable locally; this runs from the secret-gated cron endpoint.

WHICH RECAP
    A CustomRecap and a legacy Recap can share an id, and they hang their files
    off different models. Prefer ``--uuid`` when you have it (unique). With
    ``--recap-id``, CustomRecap is tried first (same as before).

Dry-run prints what would be embedded — file count, categories, field count —
without fetching images or uploading anything.

Usage::

    python manage.py export_recap_pdf --uuid 01a09cc9-a145-788e-9827-92d217296c8b
    python manage.py export_recap_pdf --uuid 01a09cc9-… --apply
    python manage.py export_recap_pdf --recap-id 692 --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Render a recap to PDF, upload it, and attach the Spark PDF file row."

    def add_arguments(self, parser):
        parser.add_argument(
            "--recap-id",
            dest="recap_id",
            type=int,
            default=None,
            help="CustomRecap / Recap integer id to render.",
        )
        parser.add_argument(
            "--uuid",
            dest="recap_uuid",
            default=None,
            help="CustomRecap / Recap uuid (preferred when known).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Fetch images, render, upload, and attach. Omit for a summary.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from recaps.models import CustomRecap, Recap
        from recaps.mutation_parts.pdf_helpers import (
            _render_and_store_recap_pdf_sync,
        )
        from utils.gcs import public_url

        apply = bool(opts["apply"])
        recap_id = opts.get("recap_id")
        recap_uuid = (opts.get("recap_uuid") or "").strip() or None

        if not recap_id and not recap_uuid:
            raise CommandError("Pass --uuid or --recap-id.")

        recap = None
        kind = "custom"
        if recap_uuid:
            recap = CustomRecap.objects.filter(uuid=recap_uuid).first()
            if recap is None:
                recap = Recap.objects.filter(uuid=recap_uuid).first()
                kind = "legacy"
            if recap is None:
                raise CommandError(f"No recap with uuid={recap_uuid}.")
        else:
            recap = CustomRecap.objects.filter(id=recap_id).first()
            if recap is None:
                recap = Recap.objects.filter(id=recap_id).first()
                kind = "legacy"
            if recap is None:
                raise CommandError(f"No recap with id={recap_id}.")

        tenant = getattr(recap, "tenant", None) or getattr(
            getattr(recap, "event", None), "tenant", None
        )

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"RECAP : id={recap.id} uuid={recap.uuid}  "
            f"({'CustomRecap' if kind == 'custom' else 'Recap (legacy)'})\n"
            f"TENANT: [{getattr(tenant, 'id', '?')}] "
            f"{getattr(tenant, 'name', '(unknown)')!r}\n"
            f"EVENT : {getattr(getattr(recap, 'event', None), 'name', '(none)')!r}\n"
            f"MODE  : {'APPLY (render + upload + attach)' if apply else 'DRY-RUN (summary)'}"
        )
        self.stdout.write("=" * 72)

        if kind == "custom":
            candidates, field_candidates = self._collect_custom(recap)
            self.stdout.write(
                f"\n  {len(candidates)} attached image(s), "
                f"{len(field_candidates)} image-type field value(s)."
            )
            for crf, _ in candidates[:20]:
                cat = (
                    crf.file_recap_category.name
                    if crf.file_recap_category
                    else "Uncategorized"
                )
                self.stdout.write(f"    {cat:<32} {crf.name}")
            if len(candidates) > 20:
                self.stdout.write(f"    ... and {len(candidates) - 20} more")
            n_values = recap.custom_field_value.count()
            self.stdout.write(f"  {n_values} field value(s) will render.")
        else:
            from recaps.pdf import should_embed_recap_file
            from utils.gcs import extract_blob_name_from_url

            n_embed = 0
            for rf in recap.recap_files.all():
                if should_embed_recap_file(rf) and extract_blob_name_from_url(
                    str(rf.file)
                ):
                    n_embed += 1
            self.stdout.write(f"\n  {n_embed} embeddable image file(s).")

        if not apply:
            self.stdout.write(
                "\nDRY-RUN — no images fetched, nothing rendered or uploaded. "
                "Re-run with --apply to build and attach the PDF."
            )
            return

        stored = _render_and_store_recap_pdf_sync(recap, force=True)
        if stored is None:
            raise CommandError(
                "PDF render/store returned None — check logs "
                "(missing created_by / FileType / WeasyPrint failure)."
            )

        blob_val = getattr(stored, "url", None) or getattr(stored, "file", None)
        blob = str(blob_val or "")
        url = public_url(blob)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"Attached Spark PDF row id={stored.id} name={stored.name!r}"
            )
        )
        self.stdout.write(f"PDF_URL: {url}")
        self.stdout.write(f"PDF_BLOB: {blob}")
        self.stdout.write("=" * 72)

    # ------------------------------------------------------------------

    def _collect_custom(self, recap):
        """(attached-file candidates, image-field candidates)."""
        from recaps.pdf import IMAGE_EXTENSIONS, should_embed_recap_file
        from utils.gcs import extract_blob_name_from_url

        candidates: list[tuple[object, str]] = []
        for crf in recap.custom_recap_files.all():
            if not should_embed_recap_file(crf):
                continue
            blob_name = extract_blob_name_from_url(str(crf.url))
            if not blob_name:
                continue
            candidates.append((crf, blob_name))

        field_candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for cfv in recap.custom_field_value.all():
            raw = cfv.value
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = raw.split("?", 1)[0].split("#", 1)[0]
            _, _, ext = path.rpartition(".")
            if not ext or f".{ext.lower()}" not in IMAGE_EXTENSIONS:
                continue
            if raw in seen:
                continue
            blob_name = extract_blob_name_from_url(raw)
            if not blob_name:
                continue
            seen.add(raw)
            field_candidates.append((raw, blob_name))

        return candidates, field_candidates
