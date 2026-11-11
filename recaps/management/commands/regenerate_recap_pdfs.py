"""Re-render the stored Spark PDF on approved recaps with the current layout.

Why: the recap PDF layout changed, but a recap keeps the PDF that was
rendered when it was approved. Clients reopening older recaps still download
the old layout until the file is rebuilt.

What it touches — deliberately narrow:

* approved, non-archived CUSTOM recaps whose EVENT date is in the window;
* ONLY recaps that already have a Spark-generated PDF. A recap that never
  had one is left alone (nothing new appears on a client's recap);
* the Spark-generated PDF row(s) are replaced by one fresh render. Other
  attachments (photos, receipts, uploaded PDFs) are never touched;
* no email is sent and approval state is not changed.

DRY-RUN by default: lists what would be rebuilt. ``--apply`` does it.
Renders are slow (photos are downloaded and embedded), so work is paged:
``--limit`` recaps per run, resuming with ``--after-id``. The last line of
output is a ``JSON_RESULT:`` object including ``next_after_id`` (null when
the window is finished).
"""

from __future__ import annotations

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from recaps import models
from recaps.tenant_overview import _filter_event_window, _inclusive_dates_to_window


def _parse(raw: str | None, name: str) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise CommandError(f"--{name} must be YYYY-MM-DD") from exc


class Command(BaseCommand):
    help = "Re-render stored Spark recap PDFs with the current layout (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--start", required=True, help="Event date from (YYYY-MM-DD, inclusive).")
        parser.add_argument("--end", required=True, help="Event date to (YYYY-MM-DD, inclusive).")
        parser.add_argument("--tenant-id", type=int, default=None, help="Limit to one tenant id.")
        parser.add_argument("--limit", type=int, default=25, help="Recaps per run (default 25).")
        parser.add_argument("--after-id", type=int, default=0, help="Resume after this recap id.")
        parser.add_argument("--apply", action="store_true", help="Actually rebuild the PDFs.")

    def handle(self, *args, **opts):
        from recaps.mutation_parts.pdf_helpers import (
            _list_spark_generated_pdfs,
            _render_and_store_recap_pdf_sync,
        )

        start = _parse(opts["start"], "start")
        end = _parse(opts["end"], "end")
        if start and end and end < start:
            raise CommandError("--end is before --start")
        limit = max(1, min(int(opts["limit"] or 25), 100))
        after_id = int(opts["after_id"] or 0)
        apply = bool(opts["apply"])

        qs = models.CustomRecap.objects.filter(
            approved=True, archived_at__isnull=True, id__gt=after_id
        )
        if opts["tenant_id"]:
            qs = qs.filter(tenant_id=opts["tenant_id"])
        qs = _filter_event_window(
            qs, "event__", _inclusive_dates_to_window(start, end)
        ).order_by("id")

        self.stdout.write(
            f"Recap PDFs — event date {start} .. {end}"
            f"{f' · tenant {opts['tenant_id']}' if opts['tenant_id'] else ''}"
            f" · {'APPLY' if apply else 'DRY-RUN'} · after id {after_id} · limit {limit}"
        )

        seen = rebuilt = skipped_no_pdf = failed = 0
        last_id = after_id
        more = False
        # Walk in id order; only recaps that already have a Spark PDF count
        # toward the page limit, so a run always makes progress.
        for recap in qs.iterator(chunk_size=50):
            if rebuilt + failed >= limit and apply:
                more = True
                break
            if seen >= limit and not apply:
                more = True
                break
            last_id = recap.id
            if not _list_spark_generated_pdfs(recap):
                skipped_no_pdf += 1
                continue
            seen += 1
            if not apply:
                self.stdout.write(f"  would rebuild recap {recap.id} · {recap.name[:70]}")
                continue
            try:
                result = _render_and_store_recap_pdf_sync(recap, force=True)
            except Exception as exc:  # noqa: BLE001 - keep going, report it
                result = None
                self.stdout.write(f"  FAILED recap {recap.id}: {exc}")
            if result is None:
                failed += 1
                self.stdout.write(f"  not rebuilt recap {recap.id} (render returned nothing)")
            else:
                rebuilt += 1
                self.stdout.write(f"  rebuilt recap {recap.id} · {recap.name[:70]}")

        summary = {
            "apply": apply,
            "start": str(start),
            "end": str(end),
            "tenant_id": opts["tenant_id"],
            "with_spark_pdf": seen,
            "rebuilt": rebuilt,
            "failed": failed,
            "skipped_no_spark_pdf": skipped_no_pdf,
            "next_after_id": last_id if more else None,
        }
        self.stdout.write("")
        self.stdout.write(
            f"{seen} recap(s) with a Spark PDF in this page; "
            f"{rebuilt} rebuilt, {failed} failed, {skipped_no_pdf} skipped (no Spark PDF)."
        )
        if more:
            self.stdout.write(f"More remain — run again with --after-id {last_id}.")
        self.stdout.write("JSON_RESULT: " + json.dumps(summary))
