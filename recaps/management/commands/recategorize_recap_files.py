"""Re-file SPECIFIC recap files into another photo category.

Receipts land in the wrong bucket constantly — a BA uploads one through the
generic photo grid and it files under "Sampling photos", which the expense
export can't see (it matches categories containing "receipt"). The recap looks
complete, the spend is recorded, and the receipt is simply invisible to the
export. That is how a client invoice ends up with an unsubstantiated line.

`backfill_girlbeer_receipts` fixes this at TENANT scope: every file in a source
category moves to a target. That is right when a whole tenant was mis-wired and
wrong here — a tenant whose other recaps hold genuine sampling photos would
have those re-filed as receipts, turning one wrong invoice into many.

So this takes explicit FILE IDS. Get them from `dump_tenant_receipts`, which
prints every file with its category verbatim and an is_receipt hint.

Never deletes a file, never moves a blob, never touches the image itself — only
the category pointer. The target category must ALREADY EXIST on that file's own
tenant: resolving it per-file prevents the cross-tenant leak that caused the
Girl Beer mess, and requiring it to exist stops a typo creating a junk
category that silently hides files all over again.

DRY-RUN by default; --execute writes.

Usage::

    python manage.py recategorize_recap_files --file-ids 15324,15325,15326 \
        --target "Expense Receipts"
    ... --execute
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Move specific recap files into another category. Dry-run unless --execute."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file-ids", dest="file_ids", required=True,
            help="Comma-separated CustomRecapFile ids (from dump_tenant_receipts).",
        )
        parser.add_argument(
            "--target", required=True,
            help="Target category NAME. Must already exist on the file's tenant.",
        )
        parser.add_argument(
            "--execute", action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from recaps.models import CustomRecapFile, FileRecapCategory

        execute = bool(opts["execute"])
        target_name = opts["target"].strip()
        try:
            ids = [int(p.strip()) for p in opts["file_ids"].split(",") if p.strip()]
        except ValueError as exc:
            raise CommandError(f"--file-ids must be integers: {exc}") from exc
        if not ids:
            raise CommandError("--file-ids is empty.")

        self.stdout.write("=" * 74)
        self.stdout.write(
            f"RECATEGORIZE RECAP FILES\n"
            f"TARGET: {target_name!r}\n"
            f"FILES : {ids}\n"
            f"MODE  : {'EXECUTE (writing)' if execute else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 74)

        rows = list(
            CustomRecapFile.objects.filter(id__in=ids)
            .select_related("file_recap_category", "custom_recap__tenant")
        )
        found = {r.id for r in rows}
        for missing in [i for i in ids if i not in found]:
            self.stdout.write(self.style.ERROR(f"  file {missing}: NOT FOUND"))

        plan = []
        for f in rows:
            recap = f.custom_recap
            tenant = getattr(recap, "tenant", None)
            tname = getattr(tenant, "name", "(unknown)")
            current = getattr(f.file_recap_category, "name", None)

            # Resolved on the FILE'S OWN tenant, never globally — a global
            # lookup is exactly how Girl Beer's receipts ended up pointing at
            # another tenant's category.
            target = FileRecapCategory.objects.filter(
                tenant_id=getattr(tenant, "id", None), name__iexact=target_name
            ).first()

            self.stdout.write(
                f"\n  file {f.id}  recap {recap.id}  [{getattr(tenant,'id','?')}] {tname}"
                f"\n     {current!r}  ->  {target_name!r}"
            )
            if target is None:
                self.stdout.write(
                    self.style.ERROR(
                        f"     SKIP — {target_name!r} does not exist on this tenant. "
                        "Create it first; refusing to invent a category."
                    )
                )
                continue
            if getattr(f.file_recap_category, "id", None) == target.id:
                self.stdout.write("     already there — no change")
                continue
            plan.append((f, target))

        if not execute:
            self.stdout.write(
                f"\nDRY-RUN — would move {len(plan)} file(s). Re-run with --execute."
            )
            return

        moved = 0
        with transaction.atomic():
            for f, target in plan:
                f.file_recap_category = target
                f.save(update_fields=["file_recap_category"])
                moved += 1

        self.stdout.write("")
        self.stdout.write("=" * 74)
        self.stdout.write(
            self.style.SUCCESS(f"Moved {moved} file(s) into {target_name!r}.")
        )
        self.stdout.write("No file deleted, no blob moved — category pointer only.")
        self.stdout.write("=" * 74)
