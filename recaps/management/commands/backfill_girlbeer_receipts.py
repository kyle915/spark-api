"""Fix Girl Beer recap receipts mis-filed into a foreign "Table setup".

Root cause (found via the dry-run): Girl Beer was onboarded WITHOUT its own
``FileRecapCategory`` rows. The upload widgets send a positional sentinel "2"
for the receipt slot; with no "Receipts" category to match, that sentinel fell
all the way through ``_resolve_file_recap_category`` to the PK fallback and
landed on the GLOBAL PK-2 category — another tenant's "Table setup" (a
cross-tenant leak). Photos (sentinel "1") similarly leak to PK-1
"Sampling photos".

This command does the real fix, in two safe steps:

  1. SEED — create Girl Beer's own default file categories ("Sampling photos",
     "Table setup", "Receipts") if missing. This makes NEW receipt uploads
     resolve to Girl Beer's own "Receipts" (exact-name match) and stops the
     cross-tenant leak. Idempotent.
  2. BACKFILL — re-file the EXISTING mis-filed receipts: Girl Beer recap files
     (scoped by the file's RECAP tenant, so it catches the foreign-category
     leak) whose current category NAME is the source ("Table setup") are moved
     to Girl Beer's own "Receipts".

RECATEGORIZE + SEED only — it never deletes a file or moves a blob. DRY-RUN by
default; pass --execute to write. Idempotent (re-running finds nothing left).

Flags:
  --tenant-slug <slug>  default "girl-beer"
  --source <name>       mis-fil category name to drain, default "Table setup"
  --target <name>       destination category name, default "Receipts"
  --execute             actually write (seed + move). Default: dry-run report.

Usage:
  python manage.py backfill_girlbeer_receipts                 # dry-run report
  python manage.py backfill_girlbeer_receipts --execute       # seed + backfill
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)

DEFAULT_TENANT_SLUG = "girl-beer"
DEFAULT_SOURCE = "Table setup"
DEFAULT_TARGET = "Receipts"
_MAX_PRINT = 300


def _name_url(f) -> tuple[str, str]:
    name = f.name or ""
    raw = getattr(f, "url", None) or getattr(f, "file", None)
    return name, (str(raw) if raw else "")


class Command(BaseCommand):
    help = (
        "Seed Girl Beer's file categories + re-file receipts mis-filed into a "
        "foreign 'Table setup'. DRY-RUN by default; pass --execute to write."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default=DEFAULT_TENANT_SLUG)
        parser.add_argument("--source", default=DEFAULT_SOURCE)
        parser.add_argument("--target", default=DEFAULT_TARGET)
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually write (seed + move). Default: dry-run report.",
        )

    def handle(self, *args, **opts):
        from recaps import models
        from tenants.models import Tenant

        try:
            from tenants.mutations import DEFAULT_FILE_RECAP_CATEGORIES
        except Exception:  # pragma: no cover - defensive
            DEFAULT_FILE_RECAP_CATEGORIES = [
                "Sampling photos",
                "Table setup",
                "Receipts",
            ]

        tenant_slug = opts["tenant_slug"]
        source_name = opts["source"]
        target_name = opts["target"]
        execute = bool(opts.get("execute"))

        try:
            tenant = Tenant.objects.get(slug=tenant_slug)
        except Tenant.DoesNotExist:
            raise CommandError(f"No tenant with slug '{tenant_slug}'.")

        self.stdout.write(
            f"Tenant '{tenant.name}' (slug={tenant_slug}, id={tenant.id}), "
            f"mode={'EXECUTE' if execute else 'DRY-RUN'}."
        )

        own = {
            c.name.lower(): c
            for c in models.FileRecapCategory.objects.filter(tenant_id=tenant.id)
        }
        self.stdout.write(
            "  Own file categories: "
            + (
                ", ".join(f"{c.name}(id={c.id})" for c in own.values())
                if own
                else "(NONE — this is why receipts leaked to a shared 'Table setup')"
            )
        )

        # --- Step 1: ensure Girl Beer's own default categories exist. ---
        created = []
        for name in DEFAULT_FILE_RECAP_CATEGORIES:
            if name.lower() in own:
                continue
            if execute:
                cat = models.FileRecapCategory.objects.create(
                    name=name, tenant_id=tenant.id
                )
                own[name.lower()] = cat
                created.append(f"{name}(id={cat.id})")
            else:
                created.append(f"{name}(would create)")
        if created:
            self.stdout.write(f"  Seed categories: {', '.join(created)}.")

        target_cat = own.get(target_name.lower())
        if target_cat is None and not execute:
            self.stdout.write(
                f"  Target '{target_name}' would be created on --execute."
            )

        # --- Step 2: find Girl Beer recap files currently in a `source`-named
        #     category (ANY tenant's — the leak points at a foreign one), scoped
        #     by the FILE's recap tenant. ---
        rows: list[tuple[str, object, str, str, object]] = []
        for f in (
            models.CustomRecapFile.objects.filter(
                custom_recap__tenant_id=tenant.id,
                file_recap_category__name__iexact=source_name,
            )
            .select_related("file_recap_category", "file_recap_category__tenant")
            .iterator()
        ):
            n, u = _name_url(f)
            rows.append(("CustomRecapFile", f, n, u, f.file_recap_category))
        for f in (
            models.RecapFile.objects.filter(
                recap__event__tenant_id=tenant.id,
                file_recap_category__name__iexact=source_name,
            )
            .select_related("file_recap_category", "file_recap_category__tenant")
            .iterator()
        ):
            n, u = _name_url(f)
            rows.append(("RecapFile", f, n, u, f.file_recap_category))

        if not rows:
            self.stdout.write(
                f"  No Girl Beer recap files in a '{source_name}' category. "
                "Nothing to backfill."
                + ("" if execute else " (Run with --execute to seed categories.)")
            )
            return

        self.stdout.write(
            f"  Found {len(rows)} Girl Beer recap file(s) in a '{source_name}' "
            f"category (these are the mis-filed receipts):"
        )
        for i, (kind, f, name, url, cat) in enumerate(rows):
            if i >= _MAX_PRINT:
                self.stdout.write(f"    … (+{len(rows) - _MAX_PRINT} more)")
                break
            owner = getattr(getattr(cat, "tenant", None), "id", None)
            foreign = " FOREIGN-CATEGORY" if owner not in (None, tenant.id) else ""
            self.stdout.write(
                f"    - {kind} id={getattr(f, 'id', '?')} name={name!r} "
                f"cat='{getattr(cat, 'name', '?')}'(id={getattr(cat, 'id', '?')}, "
                f"tenant={owner}){foreign}"
            )

        if not execute:
            self.stdout.write(
                self.style.SUCCESS(
                    f"DRY-RUN: would seed Girl Beer's '{target_name}' category and "
                    f"move {len(rows)} file(s) into it. Re-run with --execute."
                )
            )
            return

        if target_cat is None:
            raise CommandError(
                f"Target category '{target_name}' missing and could not be created."
            )

        moved = 0
        with transaction.atomic():
            for kind, f, _n, _u, _cat in rows:
                f.file_recap_category = target_cat
                f.save(update_fields=["file_recap_category", "updated_at"])
                moved += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Seeded {len([c for c in created if 'id=' in c])} "
                f"categor(ies); moved {moved} file(s) from '{source_name}' to "
                f"'{target_cat.name}'(id={target_cat.id})."
            )
        )
