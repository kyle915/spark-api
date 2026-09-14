"""Torch THC: rename On-Shelf Product → Before & After Shelf / Stock.

The walk-up Photos step already had a shelf/stock bucket labelled
"On-Shelf Product". BAs now file both before and after shots there, so the
label needs to say so — without minting a second FileRecapCategory (that would
orphan history already filed under the old name).

Idempotent. DRY-RUN by default; --apply writes.

Also the seed source of truth for Torch's retail photo-bucket labels: future
``setup_tenant_checkin`` dispatches should pass ``PHOTO_BUCKETS`` (or the
per-bucket name below) so re-applies keep this wording.
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

TENANT_SLUG = "torch-thc"
# Standing BA clock code — never remint.
CHECKIN_CODE = "TH-2HRV3D"

NEW_NAME = "Before & After Shelf / Stock"

# Live label today, plus near-miss spellings that should fold onto NEW_NAME.
OLD_NAMES = (
    "On-Shelf Product",
    "On Shelf Product",
    "On-shelf Product",
    "Onshelf Product",
)

# Desired Torch retail dropzones (Product Spend stays Product Spend, not
# Receipts). Pass this JSON to setup_tenant_checkin --photo-buckets so a
# future apply keeps the new label and still finds the old category row.
PHOTO_BUCKETS: list[dict] = [
    {"name": "Sampling photos"},
    {"name": "Table Set Up"},
    {"name": NEW_NAME, "aliases": list(OLD_NAMES)},
    {"name": "Product Spend"},
]


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


OLD_NORMS = {_norm(n) for n in OLD_NAMES}
NEW_NORM = _norm(NEW_NAME)


def _is_old_name(name: str | None) -> bool:
    return _norm(name) in OLD_NORMS


def _is_new_name(name: str | None) -> bool:
    return _norm(name) == NEW_NORM


def _scrub_bucket_entries(entries: list) -> tuple[list, bool]:
    if not isinstance(entries, list):
        return entries, False
    changed = False
    out: list = []
    have_new = False
    for entry in entries:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        name = entry.get("name") or ""
        if _is_old_name(name):
            changed = True
            if have_new:
                continue
            out.append({**entry, "name": NEW_NAME})
            have_new = True
            continue
        if _is_new_name(name):
            if have_new:
                changed = True
                continue
            if name != NEW_NAME:
                changed = True
                out.append({**entry, "name": NEW_NAME})
            else:
                out.append(entry)
            have_new = True
            continue
        out.append(entry)
    return out, changed


def scrub_checkin_photo_buckets(raw) -> tuple[object, bool]:
    """Rename On-Shelf → Before & After in Tenant.checkin_photo_buckets."""
    if raw is None:
        return None, False
    if isinstance(raw, list):
        return _scrub_bucket_entries(raw)
    if isinstance(raw, dict):
        changed_any = False
        new: dict = {}
        for key, entries in raw.items():
            scrubbed, changed = _scrub_bucket_entries(entries if entries else [])
            new[key] = scrubbed
            changed_any = changed_any or changed
        return new, changed_any
    return raw, False


class Command(BaseCommand):
    help = (
        "Torch THC: rename On-Shelf Product photo bucket to "
        f"{NEW_NAME!r} (FileRecapCategory + checkin_photo_buckets)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--tenant", default=TENANT_SLUG)

    def handle(self, *args, **opts):
        from recaps.models import FileRecapCategory
        from tenants.models import Tenant

        apply = bool(opts["apply"])
        slug = (opts["tenant"] or TENANT_SLUG).strip()
        matches = list(Tenant.objects.filter(slug=slug).order_by("id"))
        if not matches:
            raise CommandError(f"No tenant with slug {slug!r}.")
        if len(matches) > 1:
            ids = ", ".join(str(t.id) for t in matches)
            raise CommandError(f"{len(matches)} tenants share slug {slug!r} ({ids}).")
        tenant = matches[0]

        self.stdout.write("=" * 68)
        self.stdout.write(f"Tenant : [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")
        self.stdout.write(
            f"Code   : {tenant.checkin_code or '(none)'}  "
            f"(keep {CHECKIN_CODE}; never remint)"
        )
        self.stdout.write(f"Mode   : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")
        self.stdout.write("=" * 68)

        if tenant.checkin_code and tenant.checkin_code != CHECKIN_CODE:
            self.stdout.write(
                self.style.WARNING(
                    f"  ! checkin_code is {tenant.checkin_code!r}, expected "
                    f"{CHECKIN_CODE!r} — leaving it alone (do not remint)."
                )
            )

        cats = list(FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id"))
        new_cat = next((c for c in cats if _is_new_name(c.name)), None)
        old_cats = [
            c for c in cats if _is_old_name(c.name) and (new_cat is None or c.id != new_cat.id)
        ]

        if new_cat is not None and new_cat.name != NEW_NAME:
            self.stdout.write(
                f"  ~ category [{new_cat.id}] {new_cat.name!r} → {NEW_NAME!r} "
                "(normalize spelling)"
            )
            if apply:
                with transaction.atomic():
                    new_cat.name = NEW_NAME
                    new_cat.save(update_fields=["name", "updated_at"])
                self.stdout.write(
                    self.style.SUCCESS(f"  ~ relabelled [{new_cat.id}] → {NEW_NAME!r}")
                )
        elif new_cat is not None:
            self.stdout.write(f"  = {NEW_NAME!r} already exists [{new_cat.id}]")

        if not old_cats and new_cat is None:
            self.stdout.write(
                self.style.WARNING(
                    f"  ! no {OLD_NAMES[0]!r} (or {NEW_NAME!r}) category — "
                    "nothing to rename; create via setup_tenant_checkin if needed"
                )
            )
        elif not old_cats:
            self.stdout.write("  = no On-Shelf-named categories left to rename")

        for cat in old_cats:
            if new_cat is None:
                self.stdout.write(f"  ~ category [{cat.id}] {cat.name!r} → {NEW_NAME!r}")
                if apply:
                    with transaction.atomic():
                        cat.name = NEW_NAME
                        cat.save(update_fields=["name", "updated_at"])
                        new_cat = cat
                    self.stdout.write(
                        self.style.SUCCESS(f"  ~ relabelled [{cat.id}] → {NEW_NAME!r}")
                    )
                else:
                    new_cat = cat  # treat as target for dry-run reporting
            else:
                # Duplicate old row beside the already-renamed one: leave files
                # alone (would need a file move); just report.
                self.stdout.write(
                    self.style.WARNING(
                        f"  ! leftover [{cat.id}] {cat.name!r} beside "
                        f"[{new_cat.id}] {NEW_NAME!r} — not merging files; "
                        "reassign manually if needed"
                    )
                )

        buckets = getattr(tenant, "checkin_photo_buckets", None)
        new_buckets, buckets_changed = scrub_checkin_photo_buckets(buckets)
        if buckets_changed:
            self.stdout.write(
                f"  ~ checkin_photo_buckets: On-Shelf Product → {NEW_NAME}"
            )
            if apply:
                tenant.checkin_photo_buckets = new_buckets
                tenant.save(update_fields=["checkin_photo_buckets"])
                self.stdout.write(self.style.SUCCESS("  ~ checkin_photo_buckets updated"))
        else:
            self.stdout.write("  = checkin_photo_buckets already clean")

        self.stdout.write("-" * 68)
        self.stdout.write(
            "Seed buckets for setup_tenant_checkin --photo-buckets: "
            + str(PHOTO_BUCKETS)
        )
        if apply:
            self.stdout.write(self.style.SUCCESS(f"Done. label={NEW_NAME!r}"))
        else:
            self.stdout.write(
                self.style.WARNING("DRY-RUN complete — re-run with --apply to write.")
            )
