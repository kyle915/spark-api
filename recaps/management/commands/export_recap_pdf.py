"""Render one recap to PDF and upload it, printing the URL.

Exactly what the "Export PDF" button on a recap produces — this calls the same
`recaps.pdf.build_recap_pdf` and gathers images the same way, so the two can't
drift into producing different documents. It exists because that button is a
GraphQL mutation behind a login and prod isn't reachable locally; this runs from
the secret-gated cron endpoint instead.

WHICH RECAP
    A CustomRecap and a legacy Recap can share an id, and they hang their files
    off different models. The type is RESOLVED (CustomRecap first) and printed
    on every run rather than assumed, for the same reason
    `attach_fpo_recap_images` does it: writing to the wrong one is accepted by
    the database because the row it points at genuinely exists.

Images come from two places and both matter: the recap's attached files, and
image-type custom FIELDS whose value is a GCS blob path. Missing the second set
is why receipts once rendered as raw path text in the PDF.

Dry-run prints what would be embedded — file count, categories, total bytes —
without fetching images or uploading anything, which is enough to confirm you
have the right recap before spending the downloads.

Usage::

    python manage.py export_recap_pdf --recap-id 692
    python manage.py export_recap_pdf --recap-id 692 --apply
"""

from __future__ import annotations

import concurrent.futures as cf

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Render a recap to PDF and upload it. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument(
            "--recap-id",
            dest="recap_id",
            type=int,
            required=True,
            help="CustomRecap / Recap id to render.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Fetch images, render and upload. Omit for a summary.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from recaps.models import CustomRecap, Recap

        apply = bool(opts["apply"])
        recap_id = opts["recap_id"]

        recap = CustomRecap.objects.filter(id=recap_id).first()
        kind = "custom"
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
            f"MODE  : {'APPLY (render + upload)' if apply else 'DRY-RUN (summary)'}"
        )
        self.stdout.write("=" * 72)

        if kind != "custom":
            raise CommandError(
                "This command renders CUSTOM recaps. Recap "
                f"{recap_id} is a legacy Recap, which uses a different builder "
                "— use the Export PDF button on the recap page for that one."
            )

        candidates, field_candidates = self._collect(recap)

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

        if not apply:
            self.stdout.write(
                "\nDRY-RUN — no images fetched, nothing rendered or uploaded. "
                "Re-run with --apply to build the PDF."
            )
            return

        images, field_images = self._fetch(candidates, field_candidates)
        missing = len(candidates) - len(images)
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    f"  {missing} image(s) could not be downloaded and will be "
                    "missing from the PDF."
                )
            )

        from recaps.pdf import build_recap_pdf

        pdf = build_recap_pdf(recap, images, custom_field_images=field_images)

        from django.utils import timezone as _tz

        from utils.gcs import public_url, upload_bytes

        ts = _tz.now().strftime("%Y%m%d%H%M%S")
        blob = f"recaps/pdfs/custom-{recap.uuid}-{ts}.pdf"
        upload_bytes(blob, pdf, content_type="application/pdf")
        url = public_url(blob)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"PDF {len(pdf) // 1024}KB · {len(images)}/{len(candidates)} "
                f"image(s) embedded"
            )
        )
        self.stdout.write(f"PDF_URL: {url}")
        self.stdout.write("=" * 72)

    # ------------------------------------------------------------------

    def _collect(self, recap):
        """(attached-file candidates, image-field candidates).

        The second list is easy to forget and its absence is silent: an
        image-type custom field stores a blob PATH as its value, so leaving it
        out renders the path as literal text in the PDF instead of the photo.
        """
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
            # Keyed by the ORIGINAL value so the renderer can match it back
            # against CustomFieldValue.value.
            field_candidates.append((raw, blob_name))

        return candidates, field_candidates

    def _fetch(self, candidates, field_candidates):
        """Download both sets in parallel. A blob that fails is skipped rather
        than failing the export — one dead file shouldn't cost the whole PDF."""
        from recaps.pdf import is_image_bytes
        from utils.gcs import download_blob_bytes

        def _one(item):
            crf, blob_name = item
            try:
                data = download_blob_bytes(blob_name)
            except Exception:  # noqa: BLE001 — skipped, reported by caller
                return None
            if not data or not is_image_bytes(data):
                return None
            return {
                "name": crf.name,
                "bytes": data,
                "category": (
                    crf.file_recap_category.name
                    if crf.file_recap_category
                    else "Uncategorized"
                ),
            }

        def _one_field(item):
            value_key, blob_name = item
            try:
                data = download_blob_bytes(blob_name)
            except Exception:  # noqa: BLE001
                return None
            if not data or not is_image_bytes(data):
                return None
            return (value_key, data)

        images: list[dict] = []
        field_images: dict[str, bytes] = {}
        if candidates:
            with cf.ThreadPoolExecutor(max_workers=16) as pool:
                for entry in pool.map(_one, candidates):
                    if entry is not None:
                        images.append(entry)
        if field_candidates:
            with cf.ThreadPoolExecutor(max_workers=16) as pool:
                for result in pool.map(_one_field, field_candidates):
                    if result is not None:
                        field_images[result[0]] = result[1]
        return images, field_images
