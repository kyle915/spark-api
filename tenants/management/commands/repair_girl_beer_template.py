"""
Bring an EXISTING Girl Beer CustomRecapTemplate up to the current
"Retail Sampling Recap" field spec — non-destructively.

Why this exists (and why it's NOT a migration):
    CustomRecapTemplate / RecapSection / CustomField are TENANT DATA, not
    schema. The rows live in each deployment's DB and were seeded by
    `onboard_girl_beer` at different points in time, so prod's Girl Beer
    template drifted behind the seed: it's missing ~10 fields (the four
    `(Total)` bought/sampled rows, the whole `Total sampled` group,
    "Number of Customers Engaged…") and has at least one label drift
    ("Foot Traffic (people walking by per hour)"). That's exactly why a
    Connecteam recap imported with only "8 of 41 fields recognized" — the
    template the parser matched against didn't cover / didn't label-match
    the PDF labels. A Django migration can't fix tenant data per-env;
    this command can, and it must be run against prod separately.

What it does — all idempotent and NON-DESTRUCTIVE:
    1. RENAME drifted labels in place (keeps the CustomField row, so its
       CustomFieldValues — historical recap answers — ride along; no data
       is dropped).
    2. ADD any CustomField from the spec that's missing, in the right
       section + with the right field type (creating the RecapSection
       and registering the field type if needed).
    3. Keep the template's `layout.sections` JSON in sync so the BA
       renderer shows new sections in order.

    It NEVER deletes a CustomField, never deletes a RecapSection, never
    touches a CustomFieldValue, and never renames a field onto a name that
    already exists on the template (that would orphan data). Safe to
    re-run; a clean template produces zero changes.

The field spec is imported from `onboard_girl_beer.SECTIONS` so the seed
and the repair can never disagree.

Usage:
    python manage.py repair_girl_beer_template --owner-email kyle@igniteproductions.co
    python manage.py repair_girl_beer_template --owner-email kyle@... --dry-run
    python manage.py repair_girl_beer_template --owner-email kyle@... --tenant-slug girl-beer
"""

from __future__ import annotations

import logging
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    RecapSection,
)
from tenants.models import Tenant

from .onboard_girl_beer import (
    FT_IMAGE,
    FT_LONGTEXT,
    FT_NUMBER,
    FT_TEXT,
    SECTIONS,
    TENANT_SLUG,
)

User = get_user_model()
logger = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    """Same normalization the importer uses to compare labels — lower,
    strip non-alphanumerics, collapse whitespace. Used here to detect a
    drifted label that should be RENAMED rather than re-ADDED (e.g. an
    old "Foot Traffic (people walking by per hour)" vs the spec's "Foot
    Traffic (number of people walking by demo table per hour)").

    NOTE: we intentionally use the *raw* alphanumeric form (NOT the
    importer's discriminating-parenthetical variant) so that, say,
    "Account Spend Amount ($)" and "Account Spend Amount" compare equal
    and the repair leaves the existing row alone instead of adding a
    duplicate.
    """
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


# Known label drifts: {spec_label: [old_labels_to_rename_from, ...]}.
# When a template still carries one of the old labels (and does NOT yet
# carry the spec label), we rename the existing row to the spec label so
# its historical CustomFieldValues survive. Matched case-insensitively /
# normalization-insensitively; the literal forms here document the exact
# strings seen in the wild.
LABEL_RENAMES: dict[str, list[str]] = {
    "Foot Traffic (number of people walking by demo table per hour)": [
        "Foot Traffic (people walking by per hour)",
        "Foot Traffic",
    ],
    "Number of Customers Engaged (talked to or sampled product)": [
        "Number of Customers Engaged",
    ],
    "Product purchase receipt (image)": [
        "Product purchase receipt",
    ],
    "Sampling pictures (photos)": [
        "Sampling pictures",
    ],
    # "Account Spend Amount ($)" -> "Account Spend Amount": normalization
    # treats them as equal, so this is a cosmetic rename only.
    "Account Spend Amount": [
        "Account Spend Amount ($)",
    ],
}


class Command(BaseCommand):
    help = (
        "Repair an existing Girl Beer CustomRecapTemplate: rename drifted "
        "field labels and add any missing fields. Non-destructive + idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner-email",
            required=True,
            help="Email of the Spark admin to attribute created_by/updated_by to.",
        )
        parser.add_argument(
            "--tenant-slug",
            default=TENANT_SLUG,
            help=f"Tenant slug to repair (default: {TENANT_SLUG}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what WOULD change without writing to the DB.",
        )

    def handle(self, *args, **opts):
        owner_email: str = opts["owner_email"]
        tenant_slug: str = opts["tenant_slug"]
        dry_run: bool = opts["dry_run"]

        try:
            owner = User.objects.get(email__iexact=owner_email)
        except User.DoesNotExist:
            raise CommandError(f"No user with email {owner_email}")

        try:
            tenant = Tenant.objects.get(slug=tenant_slug)
        except Tenant.DoesNotExist:
            raise CommandError(
                f"No tenant with slug '{tenant_slug}'. Run onboard_girl_beer "
                f"first to provision it."
            )

        templates = list(
            CustomRecapTemplate.objects.filter(tenant=tenant).order_by("id")
        )
        if not templates:
            raise CommandError(
                f"Tenant '{tenant.name}' (id={tenant.id}) has no "
                f"CustomRecapTemplate to repair. Run onboard_girl_beer first."
            )

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nRepairing Girl Beer template(s) · tenant='{tenant.name}' "
            f"(id={tenant.id}) · owner={owner.email}"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE("DRY RUN — no DB writes.\n"))

        # Build the field types once (or stage them for dry-run).
        if dry_run:
            field_types = self._peek_field_types()
        else:
            field_types = self._ensure_field_types(owner)

        totals = {"renamed": 0, "added": 0, "sections_added": 0}
        for template in templates:
            self.stdout.write(self.style.HTTP_INFO(
                f"\n  Template id={template.id} '{template.name}'"
            ))
            if dry_run:
                self._repair_template(
                    template, tenant, field_types, owner, totals, dry_run=True
                )
            else:
                with transaction.atomic():
                    self._repair_template(
                        template, tenant, field_types, owner, totals, dry_run=False
                    )

        verb = "Would apply" if dry_run else "Applied"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb}: {totals['renamed']} rename(s), {totals['added']} "
            f"field(s) added, {totals['sections_added']} section(s) added "
            f"across {len(templates)} template(s)."
        ))
        if not dry_run and (totals["renamed"] or totals["added"]):
            logger.info(
                "repair_girl_beer_template: tenant=%s renamed=%s added=%s "
                "sections_added=%s",
                tenant.slug, totals["renamed"], totals["added"],
                totals["sections_added"],
            )

    # ─── Helpers ────────────────────────────────────────────────────

    def _peek_field_types(self) -> dict[str, CustomRecapFieldType | None]:
        """Dry-run: look up existing field types without creating any."""
        out: dict[str, CustomRecapFieldType | None] = {}
        for name in (FT_TEXT, FT_NUMBER, FT_IMAGE, FT_LONGTEXT):
            out[name] = CustomRecapFieldType.objects.filter(name=name).first()
        return out

    def _ensure_field_types(self, owner) -> dict[str, CustomRecapFieldType]:
        out: dict[str, CustomRecapFieldType] = {}
        for name in (FT_TEXT, FT_NUMBER, FT_IMAGE, FT_LONGTEXT):
            ft, created = CustomRecapFieldType.objects.get_or_create(
                name=name, defaults={"created_by": owner},
            )
            if created:
                self.stdout.write(
                    f"  + CustomRecapFieldType '{name}' (id={ft.id})"
                )
            out[name] = ft
        return out

    def _resolve_field_type(
        self, field_types: dict, type_name: str, owner, dry_run: bool
    ) -> CustomRecapFieldType | None:
        """Return the requested field type, falling back to text, creating
        on the fly if a needed type somehow doesn't exist yet (non-dry)."""
        ft = field_types.get(type_name) or field_types.get(FT_TEXT)
        if ft is None and not dry_run:
            # Extremely unlikely (we ensured all four up front) but never
            # crash a prod repair over a missing lookup row.
            ft, _ = CustomRecapFieldType.objects.get_or_create(
                name=type_name, defaults={"created_by": owner},
            )
            field_types[type_name] = ft
        return ft

    def _repair_template(
        self, template, tenant, field_types, owner, totals, *, dry_run: bool
    ) -> None:
        # Existing fields on THIS template, keyed by normalized label, with
        # the raw row so we can rename in place.
        existing = list(
            CustomField.objects.filter(custom_recap_template=template)
            .select_related("recap_section", "custom_field_type")
        )
        by_norm: dict[str, CustomField] = {}
        for f in existing:
            by_norm.setdefault(_normalize(f.name), f)
        existing_exact = {f.name for f in existing}

        # 1) Renames first, so the subsequent "is this field present?"
        #    check sees the corrected labels and doesn't add a duplicate.
        for spec_label, old_labels in LABEL_RENAMES.items():
            spec_norm = _normalize(spec_label)
            # If a field already carries the spec label, nothing to do.
            if spec_label in existing_exact:
                continue
            for old in old_labels:
                old_norm = _normalize(old)
                row = by_norm.get(old_norm)
                if row is None or row.name == spec_label:
                    continue
                # Guard: refuse to rename onto a name that already exists
                # on this template (would create a duplicate / orphan).
                if spec_label in existing_exact:
                    self.stdout.write(self.style.WARNING(
                        f"    ! skip rename '{row.name}' -> '{spec_label}' "
                        f"(target already exists)"
                    ))
                    break
                self.stdout.write(
                    f"    ~ rename '{row.name}' -> '{spec_label}'"
                )
                if not dry_run:
                    row.name = spec_label
                    row.updated_by = owner
                    row.save(update_fields=["name", "updated_by"])
                # Keep our local indexes consistent for the add pass.
                existing_exact.discard(old)
                existing_exact.add(spec_label)
                by_norm.pop(old_norm, None)
                by_norm[spec_norm] = row
                totals["renamed"] += 1
                break  # one old-label match per spec label is enough

        # 2) Adds — every spec field missing from the template, in the
        #    right section. Section is created if absent. Because there's
        #    no explicit order column, new fields append after existing
        #    ones; the template `layout.sections` carries section order.
        layout = template.layout if isinstance(template.layout, dict) else {}
        layout_sections = list(layout.get("sections") or [])
        layout_changed = False

        for section_name, fields in SECTIONS:
            section = self._get_or_create_section(
                tenant, section_name, owner, dry_run
            )
            for field_name, type_name, required in fields:
                norm = _normalize(field_name)
                if field_name in existing_exact or norm in by_norm:
                    # Already present (exact, post-rename, or normalization
                    # equal) — leave it untouched. Never flip data.
                    continue
                ftype = self._resolve_field_type(
                    field_types, type_name, owner, dry_run
                )
                tname = ftype.name if ftype else type_name
                self.stdout.write(
                    f"    + add '{field_name}' [{tname}]"
                    f"{' *' if required else ''} → '{section_name}'"
                )
                if not dry_run:
                    row = CustomField.objects.create(
                        custom_recap_template=template,
                        recap_section=section,
                        name=field_name,
                        custom_field_type=ftype,
                        required=required,
                        created_by=owner,
                    )
                    by_norm[norm] = row
                existing_exact.add(field_name)
                totals["added"] += 1

            # Keep section ordering hint in sync.
            if section_name not in layout_sections:
                layout_sections.append(section_name)
                layout_changed = True
                totals["sections_added"] += 1

        if layout_changed and not dry_run:
            layout["sections"] = layout_sections
            layout.setdefault("version", 1)
            template.layout = layout
            template.updated_by = owner
            template.save(update_fields=["layout", "updated_by"])

    def _get_or_create_section(
        self, tenant, section_name, owner, dry_run: bool
    ) -> RecapSection | None:
        section = RecapSection.objects.filter(
            tenant=tenant, name=section_name
        ).first()
        if section is not None:
            return section
        self.stdout.write(f"    + RecapSection '{section_name}'")
        if dry_run:
            return None
        return RecapSection.objects.create(
            tenant=tenant, name=section_name, created_by=owner,
        )
