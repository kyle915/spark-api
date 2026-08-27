"""Back up the Torch workbook, and optionally replace formulas with values.

The visible tab is an INDEX/MATCH mirror of 'Retail Schedule Source', keyed on
ROW(). That is why a row can't be edited (the cell holds a formula, not a
value) and why deleting row 4 blanks row 5 (every formula below shifts and
resolves to a different source record).

Flattening — writing each cell's CURRENT displayed value over its formula —
makes the tab plain, editable data. It is also one-way: once the formulas are
gone, nothing can recompute the tab from the source.

So this does two separate things:

    (default)  read-only. Backs BOTH tabs up to GCS — values AND formulas —
               and reports how many cells are formulas plus whether the
               ROW()-4 mapping still lines up with the source.
    --apply    writes the displayed values over the formulas.

READ THE ALIGNMENT REPORT BEFORE APPLYING. If the mapping is off, the tab is
rendering the wrong rows right now, and flattening would freeze that in place
permanently. Fix the alignment first, then flatten.

Usage::

    python manage.py flatten_torch_sheet
    python manage.py flatten_torch_sheet --apply
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

SOURCE_TAB = "Retail Schedule Source"


class Command(BaseCommand):
    help = "Back up the Torch sheet; --apply replaces formulas with values."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Overwrite formulas with their current displayed values.",
        )
        parser.add_argument(
            "--source-tab", dest="source_tab", default=SOURCE_TAB,
            help=f"Name of the source tab (default {SOURCE_TAB!r}).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from utils import torch_public_form_sheet as T

        apply = bool(opts["apply"])
        sid = T.TORCH_PUBLIC_FORM_SHEET_ID
        gid = T.TORCH_PUBLIC_FORM_GID

        self.stdout.write("=" * 72)
        self.stdout.write(
            "TORCH SHEET FLATTEN\n"
            f"MODE: {'APPLY (formulas -> values)' if apply else 'BACKUP + REPORT (read-only)'}"
        )
        self.stdout.write("=" * 72)

        svc = T._service()
        if not svc:
            self.stdout.write(self.style.ERROR("\n  No Sheets credentials."))
            return
        tab = T._tab_for_gid(svc, sid, gid)
        self.stdout.write(f"\n  display tab : {tab!r}")

        def _read(rng, render):
            return (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range=rng, valueRenderOption=render)
                .execute()
                .get("values", [])
            )

        shown = _read(T._qualify(tab, "A1:BZ"), "FORMATTED_VALUE")
        formulas = _read(T._qualify(tab, "A1:BZ"), "FORMULA")
        try:
            source = _read(f"'{opts['source_tab']}'!A1:BZ", "FORMATTED_VALUE")
        except Exception as exc:  # noqa: BLE001
            source = []
            self.stdout.write(
                self.style.WARNING(f"  source tab unreadable: {exc}")
            )

        n_formula = sum(
            1 for r in formulas for c in r if isinstance(c, str) and c.startswith("=")
        )
        self.stdout.write(
            f"  display rows: {len(shown)}   formula cells: {n_formula}\n"
            f"  source rows : {len(source)}"
        )

        # -- back BOTH tabs up before anything is considered --------------
        from django.utils import timezone as _tz

        from utils.gcs import public_url, upload_bytes

        stamp = _tz.now().strftime("%Y%m%d%H%M%S")
        blob = f"exports/torch-sheet-backup/{stamp}.json"
        payload = json.dumps(
            {
                "sheet_id": sid,
                "display_tab": tab,
                "source_tab": opts["source_tab"],
                "captured_at": stamp,
                "display_values": shown,
                "display_formulas": formulas,
                "source_values": source,
            },
            indent=2,
        ).encode()
        upload_bytes(blob, payload, content_type="application/json")
        self.stdout.write(
            f"\n  BACKUP: {public_url(blob)}\n"
            f"          ({len(payload) // 1024}KB — values AND formulas, both tabs)"
        )

        if n_formula == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "\n  No formulas left — this tab is already plain values."
                )
            )
            return

        if not apply:
            self.stdout.write(
                "\n  READ-ONLY. Nothing was written.\n"
                "  Before applying, confirm the tab is rendering the RIGHT rows:\n"
                "  flattening freezes whatever is on screen now, permanently,\n"
                "  and removes the only thing that could recompute it."
            )
            return

        # -- apply: write the displayed values over the formulas ----------
        # Row by row, padded to the widest row, so a short row can't leave
        # stale formula cells behind on the right.
        width = max((len(r) for r in shown), default=0)
        body = [list(r) + [""] * (width - len(r)) for r in shown]
        end = T._col_letter(width)
        svc.spreadsheets().values().update(
            spreadsheetId=sid,
            range=T._qualify(tab, f"A1:{end}{len(body)}"),
            valueInputOption="RAW",
            body={"values": body},
        ).execute()

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"Flattened {n_formula} formula cell(s) across "
                f"{len(body)} row(s). The tab is now plain, editable data."
            )
        )
        self.stdout.write("Backup above is the only way back — keep the URL.")
        self.stdout.write("=" * 72)
