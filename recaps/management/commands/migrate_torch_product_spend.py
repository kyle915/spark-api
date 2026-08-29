"""Torch THC: Product Spend is the receipt bucket; retire Receipts.

Idempotent. DRY-RUN by default; --apply writes.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

TENANT_SLUG = "torch-thc"
PRODUCT_SPEND_NAME = "Product Spend"
RECEIPTS_ALIASES = (
    "receipts",
    "receipt",
    "upload receipt",
    "expense receipts",
    "expense receipt",
)


def _is_receipts_name(name: str | None) -> bool:
    n = (name or "").strip().lower()
    return n in RECEIPTS_ALIASES or n == "receipts"


def _scrub_bucket_entries(entries: list) -> tuple[list, bool]:
    if not isinstance(entries, list):
        return entries, False
    changed = False
    out: list = []
    have_spend = False
    for entry in entries:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        name = entry.get("name") or ""
        if _is_receipts_name(name):
            changed = True
            if have_spend:
                continue
            out.append({**entry, "name": PRODUCT_SPEND_NAME})
            have_spend = True
            continue
        if (name or "").strip().lower() == PRODUCT_SPEND_NAME.lower():
            if have_spend:
                changed = True
                continue
            have_spend = True
        out.append(entry)
    return out, changed


def scrub_checkin_photo_buckets(raw) -> tuple[object, bool]:
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
    help = "Torch THC: Product Spend replaces Receipts for files + photo buckets."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--tenant", default=TENANT_SLUG)

    def handle(self, *args, **opts):
        from recaps.models import CustomRecapFile, FileRecapCategory, RecapFile
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
        self.stdout.write(f"Mode   : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")
        self.stdout.write("=" * 68)

        spend = FileRecapCategory.objects.filter(
            tenant_id=tenant.id, name__iexact=PRODUCT_SPEND_NAME
        ).first()
        if spend is None:
            self.stdout.write(f"  + would create {PRODUCT_SPEND_NAME!r}")
            if apply:
                spend = FileRecapCategory.objects.create(name=PRODUCT_SPEND_NAME, tenant=tenant)
                self.stdout.write(self.style.SUCCESS(f"  + created {PRODUCT_SPEND_NAME!r} [{spend.id}]"))
        else:
            self.stdout.write(f"  = {PRODUCT_SPEND_NAME!r} already exists [{spend.id}]")

        receipts_cats = [
            c for c in FileRecapCategory.objects.filter(tenant_id=tenant.id)
            if _is_receipts_name(c.name) and (spend is None or c.id != spend.id)
        ]
        if not receipts_cats:
            self.stdout.write("  = no Receipts-named categories to retire")

        moved_custom = moved_legacy = 0
        for cat in receipts_cats:
            custom_n = CustomRecapFile.objects.filter(file_recap_category_id=cat.id).count()
            legacy_n = RecapFile.objects.filter(file_recap_category_id=cat.id).count()
            self.stdout.write(
                f"  ~ category [{cat.id}] {cat.name!r}: "
                f"{custom_n} custom / {legacy_n} legacy → {PRODUCT_SPEND_NAME!r}"
            )
            if apply and spend is not None:
                with transaction.atomic():
                    moved_custom += CustomRecapFile.objects.filter(
                        file_recap_category_id=cat.id
                    ).update(file_recap_category_id=spend.id)
                    moved_legacy += RecapFile.objects.filter(
                        file_recap_category_id=cat.id
                    ).update(file_recap_category_id=spend.id)

        buckets = getattr(tenant, "checkin_photo_buckets", None)
        new_buckets, buckets_changed = scrub_checkin_photo_buckets(buckets)
        if buckets_changed:
            self.stdout.write("  ~ checkin_photo_buckets: Receipts → Product Spend")
            if apply:
                tenant.checkin_photo_buckets = new_buckets
                tenant.save(update_fields=["checkin_photo_buckets"])
                self.stdout.write(self.style.SUCCESS("  ~ checkin_photo_buckets updated"))
        else:
            self.stdout.write("  = checkin_photo_buckets already clean")

        deleted = 0
        for cat in receipts_cats:
            still = (
                CustomRecapFile.objects.filter(file_recap_category_id=cat.id).count()
                + RecapFile.objects.filter(file_recap_category_id=cat.id).count()
            )
            if still:
                self.stdout.write(self.style.WARNING(
                    f"  ! skip delete [{cat.id}] {cat.name!r} — {still} file(s) still attached"
                    + ("" if apply else " (dry-run)")
                ))
                continue
            self.stdout.write(f"  - would delete category [{cat.id}] {cat.name!r}")
            if apply:
                cat.delete()
                deleted += 1
                self.stdout.write(self.style.SUCCESS(f"  - deleted [{cat.id}] {cat.name!r}"))

        self.stdout.write("-" * 68)
        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"Done. moved_custom={moved_custom} moved_legacy={moved_legacy} deleted_categories={deleted}"
            ))
        else:
            self.stdout.write(self.style.WARNING("DRY-RUN complete — re-run with --apply to write."))
