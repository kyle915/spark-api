"""Build the BA expense-receipt CSV + PDF bundle for a tenant and date range.

Exactly what the "export receipts" button on /recaps/list produces — this calls
the same `recaps.receipts_export` functions, so the two can't drift. It exists
because that button is a GraphQL mutation behind a login, and prod isn't
reachable locally; this runs from the secret-gated cron endpoint instead.

Scoped by EVENT date (falling back to created_at for recaps with no event
date), matching the mutation and the rest of the windowed reporting.

Uploads the PDF to GCS and prints its public URL. Dry-run prints the row
summary and totals without fetching images or uploading anything, which is
enough to confirm the window caught what you expected before spending the
image downloads.

Usage::

    python manage.py export_expense_receipts --tenant-id 11 \
        --start 2026-07-24 --end 2026-08-12
    python manage.py export_expense_receipts --tenant-id 11 \
        --start 2026-07-24 --end 2026-08-12 --apply
"""

from __future__ import annotations

import concurrent.futures as cf
from datetime import date as _date

from django.core.management.base import BaseCommand, CommandError

from tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Expense-receipt CSV + PDF bundle for a tenant/date range. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            dest="tenant_id",
            type=int,
            required=True,
            help="Tenant to export. Id only — names are ambiguous.",
        )
        parser.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive).")
        parser.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive).")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Fetch images, render the PDF and upload it. Omit for a summary.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from recaps.receipts_export import (
            build_expense_rows_csv,
            build_receipts_bundle_pdf,
            collect_expense_rows,
        )

        apply = bool(opts["apply"])
        tenant = Tenant.objects.filter(id=opts["tenant_id"]).first()
        if tenant is None:
            raise CommandError(f"No tenant with id={opts['tenant_id']}.")

        try:
            start = _date.fromisoformat(opts["start"])
            end = _date.fromisoformat(opts["end"])
        except ValueError as exc:
            raise CommandError(f"Dates must be YYYY-MM-DD: {exc}") from exc
        if end < start:
            raise CommandError("End date is before the start date.")

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TENANT: [{tenant.id}] {tenant.name!r}\n"
            f"WINDOW: {start} .. {end}  (by EVENT date)\n"
            f"MODE  : {'APPLY (render + upload)' if apply else 'DRY-RUN (summary only)'}"
        )
        self.stdout.write("=" * 72)

        rows = collect_expense_rows(tenant.id, start, end)
        if not rows:
            self.stdout.write(
                self.style.WARNING(
                    "\nNo expense receipts or spend in that window. Nothing to "
                    "export — widen the dates, or check the recaps actually "
                    "carry receipt-category files or a spend amount."
                )
            )
            return

        n_files = sum(len(r.get("files") or []) for r in rows)
        total = 0.0
        for r in rows:
            try:
                total += float(r.get("amount") or 0)
            except (TypeError, ValueError):
                pass

        self.stdout.write(
            f"\n  {len(rows)} recap(s) with expense evidence, "
            f"{n_files} receipt image(s), ${total:,.2f} total spend.\n"
        )
        for r in rows[:40]:
            self.stdout.write(
                f"    {str(r.get('event_date') or '?'):<12} "
                f"{str(r.get('ba_name') or '?')[:26]:<26} "
                f"{('$%.2f' % float(r.get('amount') or 0)):>10}  "
                f"{len(r.get('files') or [])} file(s)  "
                f"{str(r.get('event_name') or '')[:40]}"
            )
        if len(rows) > 40:
            self.stdout.write(f"    ... and {len(rows) - 40} more")

        csv_text = build_expense_rows_csv(rows)
        self.stdout.write(f"\n  CSV: {len(csv_text.splitlines())} line(s).")

        if not apply:
            self.stdout.write(
                "\nDRY-RUN — no images fetched, nothing rendered or uploaded. "
                "Re-run with --apply to build the PDF."
            )
            return

        images = self._fetch_images(rows)
        missing = sum(
            1
            for r in rows
            for f in (r.get("files") or [])
            if f["blob"] not in images
        )
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    f"  {missing} receipt image(s) could not be downloaded and "
                    "will be missing from the PDF."
                )
            )

        pdf = build_receipts_bundle_pdf(
            tenant_name=tenant.name,
            start=start,
            end=end,
            rows=rows,
            images_by_blob=images,
        )

        from django.utils import timezone as _tz

        from utils.gcs import public_url, upload_bytes

        ts = _tz.now().strftime("%Y%m%d%H%M%S")
        blob = (
            f"exports/receipts/{tenant.id}/"
            f"{start.isoformat()}_{end.isoformat()}_{ts}.pdf"
        )
        upload_bytes(blob, pdf, content_type="application/pdf")
        url = public_url(blob)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"PDF {len(pdf) // 1024}KB · {len(rows)} recap(s) · "
                f"{len(images)}/{n_files} image(s) embedded"
            )
        )
        self.stdout.write(f"PDF_URL: {url}")
        self.stdout.write("=" * 72)

    # ------------------------------------------------------------------

    def _fetch_images(self, rows: list[dict]) -> dict[str, bytes]:
        """Download every receipt blob. Same 16-worker fan-out the mutation
        uses; a blob that fails is skipped rather than failing the export, so
        one dead file doesn't cost the whole bundle."""
        from utils.gcs import download_blob_bytes

        blobs = [f["blob"] for r in rows for f in (r.get("files") or [])]
        out: dict[str, bytes] = {}
        if not blobs:
            return out

        def _one(blob: str):
            try:
                data = download_blob_bytes(blob)
            except Exception:  # noqa: BLE001 — skip, reported by caller
                return None
            if not data or len(data) > 25 * 1024 * 1024:
                return None
            return (blob, data)

        with cf.ThreadPoolExecutor(max_workers=16) as pool:
            for entry in pool.map(_one, blobs):
                if entry is not None:
                    out[entry[0]] = entry[1]
        return out
