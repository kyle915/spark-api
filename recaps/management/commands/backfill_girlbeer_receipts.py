"""Re-file recap receipts mis-categorized into "Table setup" (Girl Beer).

Background: the upload widgets send a positional sentinel "2" for the receipt
slot. For a tenant whose receipt category isn't named exactly "Receipts" (Girl
Beer's custom template), that sentinel used to fall through to the legacy PK
behavior and land the file under "Table setup" — see the keyword-fallback fix
in ``recaps.mutations._resolve_file_recap_category`` (#765). That fix stops NEW
mis-filings; this one-off command re-files the EXISTING ones.

It ONLY recategorizes a recap file (sets ``file_recap_category``). It never
deletes a file, moves a blob, or touches anything else. Tenant-scoped,
idempotent (only considers files currently in the SOURCE category), and
DRY-RUN by default.

Selection — you must pick one once the dry-run report shows what's there:
  --match <substring>   move only files whose name OR url contains <substring>
                        (case-insensitive), e.g. ``--match receipt``
  --move-all            move EVERY file in the source category (use only when
                        the dry-run confirms the source holds no legitimate
                        non-receipt files)
With neither, the command REPORTS the source-category files (with a
"looks-like-receipt" hint) and moves nothing — that's the review step.

Flags:
  --tenant-slug <slug>  default "girl-beer"
  --source <name>       source category name, default "Table setup"
  --target <name>       target category; default = the tenant's receipt
                        category ("Receipts", else name containing "receipt")
  --dry-run             report only (DEFAULT)
  --execute             actually write

Usage:
  python manage.py backfill_girlbeer_receipts                      # dry-run report
  python manage.py backfill_girlbeer_receipts --match receipt      # dry-run, filtered
  python manage.py backfill_girlbeer_receipts --match receipt --execute
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)

DEFAULT_TENANT_SLUG = "girl-beer"
DEFAULT_SOURCE = "Table setup"
# Cap how many per-file lines we print so a big backlog can't spam the cron
# endpoint response / Actions log. The counts in the summary are always exact.
_MAX_PRINT = 300


def _name_url(f) -> tuple[str, str]:
    name = f.name or ""
    # RecapFile uses .file; CustomRecapFile uses .url. Both are FileFields.
    raw = getattr(f, "url", None) or getattr(f, "file", None)
    return name, (str(raw) if raw else "")


def _looks_like_receipt(name: str, url: str) -> bool:
    return "receipt" in f"{name} {url}".lower()


class Command(BaseCommand):
    help = (
        "Re-file Girl Beer recap receipts mis-categorized into 'Table setup'. "
        "DRY-RUN by default; pass --execute to write. Pick --match or --move-all."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default=DEFAULT_TENANT_SLUG)
        parser.add_argument("--source", default=DEFAULT_SOURCE)
        parser.add_argument("--target", default=None)
        parser.add_argument(
            "--match",
            default=None,
            help="Move only files whose name/url contains this (case-insensitive).",
        )
        parser.add_argument(
            "--move-all",
            action="store_true",
            help="Move ALL files in the source category.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report only — the default. Pass --execute to write.",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually write (otherwise dry-run).",
        )

    def handle(self, *args, **opts):
        from recaps import models
        from tenants.models import Tenant

        tenant_slug = opts["tenant_slug"]
        source_name = opts["source"]
        target_name = opts.get("target")
        match = (opts.get("match") or "").strip() or None
        move_all = bool(opts.get("move_all"))
        execute = bool(opts.get("execute"))

        try:
            tenant = Tenant.objects.get(slug=tenant_slug)
        except Tenant.DoesNotExist:
            raise CommandError(f"No tenant with slug '{tenant_slug}'.")

        self._print_categories(tenant)

        source_cat = models.FileRecapCategory.objects.filter(
            tenant_id=tenant.id, name__iexact=source_name
        ).first()
        if source_cat is None:
            self.stdout.write(
                f"Tenant '{tenant_slug}' has no '{source_name}' category — nothing to do."
            )
            return

        if target_name:
            target_cat = models.FileRecapCategory.objects.filter(
                tenant_id=tenant.id, name__iexact=target_name
            ).first()
        else:
            target_cat = (
                models.FileRecapCategory.objects.filter(
                    tenant_id=tenant.id, name__iexact="Receipts"
                ).first()
                or models.FileRecapCategory.objects.filter(
                    tenant_id=tenant.id, name__icontains="receipt"
                )
                .order_by("id")
                .first()
            )
        if target_cat is None:
            raise CommandError(
                f"Tenant '{tenant_slug}' has no receipt category to move into. "
                "Create one (or pass --target) before backfilling."
            )
        if target_cat.id == source_cat.id:
            raise CommandError("Source and target categories are the same; aborting.")

        selection = (
            f"match={match!r}" if match else ("move-all" if move_all else "REPORT-ONLY")
        )
        self.stdout.write(
            f"Tenant '{tenant.name}' (slug={tenant_slug}, id={tenant.id}): "
            f"source='{source_cat.name}'(id={source_cat.id}) -> "
            f"target='{target_cat.name}'(id={target_cat.id}). "
            f"mode={'EXECUTE' if execute else 'DRY-RUN'}, {selection}."
        )

        # FileRecapCategory is per-tenant, so filtering by the source category
        # id is already tenant-scoped — a file in it belongs to this tenant.
        rows: list[tuple[str, object, str, str]] = []
        for f in models.RecapFile.objects.filter(
            file_recap_category_id=source_cat.id
        ).iterator():
            n, u = _name_url(f)
            rows.append(("RecapFile", f, n, u))
        for f in models.CustomRecapFile.objects.filter(
            file_recap_category_id=source_cat.id
        ).iterator():
            n, u = _name_url(f)
            rows.append(("CustomRecapFile", f, n, u))

        if not rows:
            self.stdout.write(
                f"No files in '{source_cat.name}' for this tenant. Nothing to do."
            )
            return

        def _selected(name: str, url: str) -> bool:
            if match is not None:
                return match.lower() in f"{name} {url}".lower()
            return move_all  # False when report-only

        receiptish = sum(1 for _, _, n, u in rows if _looks_like_receipt(n, u))
        to_move = [(k, f, n, u) for (k, f, n, u) in rows if _selected(n, u)]

        for i, (kind, f, name, url) in enumerate(rows):
            if i >= _MAX_PRINT:
                self.stdout.write(f"  … (+{len(rows) - _MAX_PRINT} more not printed)")
                break
            tag = "MOVE" if _selected(name, url) else "keep"
            hint = " [looks-like-receipt]" if _looks_like_receipt(name, url) else ""
            self.stdout.write(
                f"  - [{tag}] {kind} id={getattr(f, 'id', '?')} "
                f"name={name!r} url={url!r}{hint}"
            )

        moved = 0
        if execute and to_move:
            with transaction.atomic():
                for kind, f, _n, _u in to_move:
                    f.file_recap_category = target_cat
                    f.save(update_fields=["file_recap_category", "updated_at"])
                    moved += 1

        verb = "moved" if execute else "would move"
        tail = (
            ""
            if (match or move_all)
            else " REPORT-ONLY — re-run with --match <substr> or --move-all to act."
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {len(rows)} file(s) in '{source_cat.name}' "
                f"({receiptish} look like receipts); {verb} {len(to_move)}"
                f"{(' (' + str(moved) + ' written)') if execute else ''}.{tail}"
            )
        )

    def _print_categories(self, tenant) -> None:
        from recaps import models

        self.stdout.write(f"File categories for tenant id={tenant.id} '{tenant.name}':")
        for c in models.FileRecapCategory.objects.filter(
            tenant_id=tenant.id
        ).order_by("id"):
            self.stdout.write(f"    id={c.id} name={c.name!r}")
