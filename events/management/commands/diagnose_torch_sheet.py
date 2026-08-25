"""Report whether the Torch public-form Sheet is actually reachable and current.

`append_torch_public_form_row` returns False for TWO different reasons — the
row is already on the sheet, or the write soft-failed — and the backfill prints
the same "skipped (already on sheet or no-op)" for both. That conflation hides
the one failure that matters: the sheet not being shared with the service
account, which is silent by design because a Sheets error must never 500 the
public form.

This answers it directly, read-only:

  * do Sheets credentials resolve at all?
  * can we open the workbook, and what is the tab for gid 0?
  * are the Spark mapping headers present on row 1?
  * how many data rows are there, and are the given request UUIDs among them?

Nothing is written — this only reads. If it reports credentials or the open
failing, share the workbook as Editor with the service account printed below.

Usage::

    python manage.py diagnose_torch_sheet
    python manage.py diagnose_torch_sheet --ids 1992
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Diagnose the Torch public-form Google Sheet connection (read-only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids", default="",
            help="Comma-separated Request ids to look for on the sheet.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from utils import torch_public_form_sheet as T

        sid = T.TORCH_PUBLIC_FORM_SHEET_ID
        self.stdout.write("=" * 72)
        self.stdout.write(
            "TORCH PUBLIC-FORM SHEET DIAGNOSTIC (read-only)\n"
            f"  sheet   : {sid}\n"
            f"  gid     : {T.TORCH_PUBLIC_FORM_GID}\n"
            f"  service account: {T.SERVICE_ACCOUNT_EMAIL}"
        )
        self.stdout.write("=" * 72)

        svc = T._service()
        if not svc:
            self.stdout.write(
                self.style.ERROR(
                    "\n  CREDENTIALS: none resolved.\n"
                    "  Nothing can be written. This is the failure the public "
                    "form hides — it swallows the error so submissions never "
                    "500."
                )
            )
            return
        self.stdout.write(self.style.SUCCESS("\n  CREDENTIALS: resolved."))

        try:
            tab = T._tab_for_gid(svc, sid, T.TORCH_PUBLIC_FORM_GID)
        except Exception as exc:  # noqa: BLE001 — this IS the diagnosis
            self.stdout.write(
                self.style.ERROR(
                    f"  OPEN WORKBOOK: FAILED — {type(exc).__name__}: {exc}\n"
                    f"  Share the workbook as EDITOR with "
                    f"{T.SERVICE_ACCOUNT_EMAIL}"
                )
            )
            return
        self.stdout.write(self.style.SUCCESS(f"  OPEN WORKBOOK: ok — tab {tab!r}"))

        try:
            header = T._ensure_extra_headers(svc, sid, tab)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(
                self.style.ERROR(
                    f"  HEADER: FAILED — {type(exc).__name__}: {exc}\n"
                    "  Read worked but WRITE did not: the account is probably "
                    "Viewer, not Editor."
                )
            )
            return

        present = [h for h in T.SPARK_EXTRA_HEADERS if h in header]
        missing = [h for h in T.SPARK_EXTRA_HEADERS if h not in header]
        self.stdout.write(
            f"  HEADER: {len(header)} column(s); "
            f"{len(present)}/{len(T.SPARK_EXTRA_HEADERS)} Spark columns present"
        )
        if missing:
            self.stdout.write(self.style.WARNING(f"      missing: {missing}"))

        try:
            rows = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range=T._qualify(tab, "A:A"))
                .execute()
                .get("values", [])
            )
            self.stdout.write(f"  ROWS: {max(len(rows) - 1, 0)} data row(s)")
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"  ROWS: unreadable — {exc}"))

        raw = (opts["ids"] or "").strip()
        if not raw:
            self.stdout.write(
                "\n  Pass --ids to check whether specific requests landed."
            )
            return

        from events.models import Request

        self.stdout.write("")
        for part in [p.strip() for p in raw.split(",") if p.strip()]:
            try:
                rid = int(part)
            except ValueError:
                self.stdout.write(self.style.WARNING(f"  {part!r} is not an id"))
                continue
            req = Request.objects.filter(id=rid).first()
            if req is None:
                self.stdout.write(self.style.WARNING(f"  REQ-{rid}: no such request"))
                continue
            found = T._find_uuid_row(svc, sid, tab, header, str(req.uuid))
            if found:
                self.stdout.write(
                    self.style.SUCCESS(f"  REQ-{rid}: ON THE SHEET (row {found})")
                )
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  REQ-{rid}: NOT on the sheet — a 'skipped' from the "
                        "backfill therefore means it FAILED, not deduped."
                    )
                )

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write("Read-only — nothing was modified.")
        self.stdout.write("=" * 72)
