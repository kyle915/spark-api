"""Tenant-wide FIELD-LEVEL recap export — one row per recap, every captured
field as a column, plus links to every photo and receipt.

The generalisation of ``dump_custom_recap`` (which dumps ONE recap by UUID) to a
whole tenant, and the field-level counterpart to ``campaign-to-date`` (which
returns per-Request KPI aggregates and carries no ``CustomFieldValue`` rows at
all). Data gathering lives in :mod:`recaps.recap_field_export`; XLSX/CSV
rendering in the Django-free :mod:`utils.workbook`.

Tenant-generic — the columns come from whatever ``CustomRecapTemplate``(s) the
tenant owns, and the file columns from its ``FileRecapCategory`` rows. Girl Beer
is just the first caller.

    # report only — row/column counts + category sanity, writes nothing
    python manage.py export_recap_fields --tenant girl-beer --dry-run

    # write the workbook + CSVs locally
    python manage.py export_recap_fields --tenant girl-beer --out ~/gb-recaps

    # emit the payload as JSON (what the cron endpoint returns)
    python manage.py export_recap_fields --tenant 10 --json

Date scoping is by EVENT date, never created/imported date. READ-ONLY.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from recaps.management.commands.audit_tenant_consumers import _resolve_tenant
from recaps.recap_field_export import build_recap_field_export

JSON_START = "RECAPFIELDS_JSON_START"
JSON_END = "RECAPFIELDS_JSON_END"


class Command(BaseCommand):
    help = "Read-only: export one row per recap with every captured field + file links."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="id, request-url-name, or name")
        parser.add_argument("--start", default=None, help="YYYY-MM-DD inclusive (EVENT date)")
        parser.add_argument("--end", default=None, help="YYYY-MM-DD inclusive (EVENT date)")
        parser.add_argument(
            "--out",
            default=None,
            help="path stem; writes <stem>.xlsx, <stem>.csv, <stem>-files.csv",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help=f"print the payload between {JSON_START}/{JSON_END} markers",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="print row/column counts + diagnostics only; write nothing",
        )
        parser.add_argument(
            "--no-heic-resolve",
            action="store_true",
            help="skip resolving HEIC blobs to their converted JPG sibling (faster)",
        )

    def handle(self, *args, **opts):
        tenant = _resolve_tenant(opts["tenant"])
        try:
            payload = build_recap_field_export(
                tenant,
                start=opts.get("start"),
                end=opts.get("end"),
                resolve_heic=not opts.get("no_heic_resolve"),
            )
        except ValueError as exc:
            raise CommandError(f"bad date (expected YYYY-MM-DD): {exc}") from exc
        payload["generated_at"] = timezone.now().isoformat()

        self._report(payload)

        if opts.get("dry_run"):
            self.stdout.write(self.style.WARNING("\nDry run — nothing written."))
            return

        if opts.get("out"):
            from utils.workbook import write_outputs

            written = write_outputs(payload, opts["out"])
            self.stdout.write("")
            for label, path in written.items():
                self.stdout.write(self.style.SUCCESS(f"  wrote {label:<10} {path}"))

        if opts.get("as_json"):
            self.stdout.write(JSON_START)
            self.stdout.write(json.dumps(payload, default=str))
            self.stdout.write(JSON_END)

    # ------------------------------------------------------------------
    def _report(self, payload: dict) -> None:
        """Human summary — the part a GH Actions log should carry."""
        meta = payload["meta"]
        diag = payload["diagnostics"]
        tenant = payload["tenant"]
        window = payload["window"]

        self.stdout.write(
            self.style.SUCCESS(
                f"{tenant['name']} (#{tenant['id']}) — field-level recap export"
            )
        )
        span = (
            f"{window['start'] or 'earliest'} → {window['end'] or 'latest'}"
            if (window["start"] or window["end"])
            else "all time"
        )
        self.stdout.write(f"  window:  {span}  (scoped by EVENT date)")
        self.stdout.write(f"  grain:   one row per RECAP")
        self.stdout.write(
            f"  rows:    {meta['row_count']}  "
            f"(of {diag['recaps_total_for_tenant']} recaps for this tenant)"
        )
        self.stdout.write(
            f"  columns: {meta['column_count']}  "
            f"({meta['identity_column_count']} identity + "
            f"{meta['field_column_count']} fields + {meta['file_column_count']} files)"
        )
        self.stdout.write(f"  files:   {meta['file_count']}")
        if meta["templates"]:
            names = ", ".join(str(v) for v in meta["templates"].values())
            self.stdout.write(f"  templates: {names}")

        if diag["recaps_excluded_out_of_window"]:
            self.stdout.write(
                f"  excluded (out of window): {diag['recaps_excluded_out_of_window']}"
            )
        if diag["recaps_without_event_date"]:
            self.stdout.write(
                self.style.WARNING(
                    f"  recaps with NO event date: {diag['recaps_without_event_date']}"
                    + (" — excluded, cannot be placed in the window" if (window["start"] or window["end"]) else "")
                )
            )
        self.stdout.write(
            f"  events in window with no recap: {diag['events_without_recap']}"
            " (reported, not rowed — a blank row would read as a measured zero)"
        )

        by_cat = diag["files_by_category"]
        if by_cat:
            self.stdout.write("\n  files by category:")
            for name, count in sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0])):
                self.stdout.write(f"    {count:>5}  {name}")

        if diag["files_without_a_link"]:
            self.stdout.write(
                self.style.ERROR(
                    f"\n  ⚠ {diag['files_without_a_link']} attached file(s) could NOT be "
                    f"linked (missing blob, or GS_BUCKET_NAME unset). Do not ship "
                    f"this export until that is understood — the photos exist but "
                    f"the deliverable would omit them silently."
                )
            )

        # The category-sanity check. Girl Beer's receipts once filed themselves
        # under a photo category; grouping without looking would put receipts
        # in the photo column and nobody would notice until a client did.
        mis = diag["receipt_looking_files_outside_receipt_categories"]
        if mis["count"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  ⚠ {mis['count']} receipt-looking file(s) in a NON-receipt category:"
                )
            )
            for s in mis["samples"][:10]:
                self.stdout.write(f"      [{s['category']}] {s['name'][:70]}")
        else:
            self.stdout.write(
                "\n  category check: no receipt-looking files outside receipt categories ✓"
            )
        odd = diag["non_receipt_looking_files_in_receipt_categories"]
        if odd["count"]:
            self.stdout.write(
                f"  note: {odd['count']} file(s) in a receipt category without a "
                f"receipt-looking name (normal — phone camera filenames)"
            )

        self.stdout.write(
            f"\n  structured sampled quantities (samplesDistributed basis): "
            f"{diag['structured_samples_total']:,}"
        )

        over = diag["consumers_exceeding_engagements"]
        if over["count"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  ⚠ {over['count']} recap(s) report MORE consumers sampled than "
                    f"total engagements — impossible; overstates consumers by "
                    f"{over['total_excess']:,}:"
                )
            )
            for r in over["rows"][:10]:
                self.stdout.write(
                    f"      eng={r['engagements']:>5} consumers={r['consumers_sampled']:>5} "
                    f"(+{r['excess']}) {r['event'][:38]}  [{r['ba']}]"
                )

        if diag["unapproved_or_draft_rows"] or diag["internal_demo_rows"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  ⚠ rows to review before sharing with a client: "
                    f"{diag['unapproved_or_draft_rows']} unapproved/draft, "
                    f"{diag['internal_demo_rows']} internal-demo. "
                    f"Included (nothing is auto-dropped) — decide deliberately."
                )
            )

        if diag["duplicate_field_names_collapsed"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  collapsed duplicate field names across templates: "
                    + ", ".join(diag["duplicate_field_names_collapsed"][:10])
                )
            )
        empties = diag["field_columns_entirely_empty"]
        if empties:
            self.stdout.write(
                f"\n  {len(empties)} field column(s) empty in every row: "
                + ", ".join(empties[:10])
                + ("…" if len(empties) > 10 else "")
            )
