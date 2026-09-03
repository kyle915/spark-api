"""Seed Liquid Death's Product Seeding recap template.

Creates (or reconciles) event type ``Product Seeding`` and template
``Liquid Death-Product Seeding`` with:

* Location product dropped (venue / retailer / address)
* Total cases dropped (aggregate)
* Cases Dropped by SKU (catalog-driven multiselect + ``product_samples`` qty)
* Total mileage

Template name deliberately avoids ``event`` / ``activation`` / ``festival`` /
``pop-up`` so Recaps list ``?activation=event`` does not mis-bucket seeding
rows; they stay unclassified and appear under View all.

Photos stay on walk-up ``FileRecapCategory`` buckets from
``setup_ld_retail_checkin`` — this SPEC does NOT add template image fields.

SKU options prefer the live tenant Product catalog (same source as Retail /
Event Products Sampled); hardcoded spark-form list is the empty-catalog
fallback via ``setup_ld_retail_checkin.product_options_for_tenant``.

Idempotent. DRY-RUN by default. Run via
``/internal/cron/seed-ld-product-seeding-recap-template`` (or the GitHub
Action) against prod, then re-run ``setup_ld_retail_checkin --apply`` so the
standing ``LD-`` link offers Product Seeding alongside Retail + Event.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

TEMPLATE_NAME = "Liquid Death-Product Seeding"
EVENT_TYPE_NAME = "Product Seeding"

# BA-facing label; catalog bridge treats this as a Products Sampled alias.
CASES_BY_SKU_FIELD = "Cases Dropped by SKU"

SECTION_ORDER = {
    "Drop-off Details": 0,
    "Cases by SKU": 1,
}


def _product_options(tenant) -> list[str]:
    from recaps.management.commands.setup_ld_retail_checkin import (
        product_options_for_tenant,
    )

    return product_options_for_tenant(tenant)


def _build_spec(options: list[str]):
    return [
        (
            "Drop-off Details",
            [
                ("Location product dropped", "text", True, []),
                ("Total cases dropped", "number", True, []),
                ("Total mileage", "number", True, []),
            ],
        ),
        (
            "Cases by SKU",
            [
                (CASES_BY_SKU_FIELD, "multiselect", False, list(options)),
            ],
        ),
    ]


def _match_field_type(kind: str, name_lower: str) -> bool:
    """Does an existing CustomRecapFieldType named `name_lower` serve `kind`?"""
    if kind == "image":
        return any(t in name_lower for t in ("image", "photo", "img"))
    if kind == "multiselect":
        return name_lower == "multiselect" or "multi" in name_lower
    if kind == "select":
        return name_lower == "select" or "dropdown" in name_lower
    if kind == "number":
        return (
            name_lower == "number"
            or "num" in name_lower
            or "integer" in name_lower
        )
    if kind == "longtext":
        return (
            "long" in name_lower
            or "textarea" in name_lower
            or "paragraph" in name_lower
        )
    if kind == "text":
        return name_lower == "text"
    return False


class Command(BaseCommand):
    help = (
        "Seed Liquid Death Product Seeding recap template "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="liquid death")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing).",
        )

    def _resolve_tenant(self, needle: str):
        from tenants.models import Tenant

        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if len(matches) > 1:
            exact = [
                t
                for t in matches
                if (t.slug or "").lower() in ("liquid-death", "liquiddeath")
                or (t.name or "").strip().lower() == "liquid death"
            ]
            if len(exact) == 1:
                return exact[0]
        if len(matches) == 1:
            return matches[0]
        self.stdout.write(self.style.WARNING("Tenants in this database:"))
        for t in Tenant.objects.order_by("id"):
            self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
        if not matches:
            raise CommandError(f"No tenant matches {needle!r}.")
        raise CommandError(
            f"{needle!r} matched {len(matches)} tenants — narrow --tenant."
        )

    def _resolve_creator(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        creator = (
            User.objects.filter(is_superuser=True).order_by("id").first()
            or User.objects.order_by("id").first()
        )
        if creator is None:
            raise CommandError("No user available to own the created rows.")
        return creator

    def _ensure_event_type(self, tenant, name: str, creator, apply: bool):
        from events.models import EventType

        existing = EventType.objects.filter(
            tenant_id=tenant.id, name__iexact=name
        ).first()
        if existing:
            return existing
        if not apply:
            self.stdout.write(f"  would create event type {name!r}")
            return None
        et = EventType.objects.create(
            name=name, tenant=tenant, created_by=creator
        )
        self.stdout.write(f"  + event type {name!r} [{et.id}]")
        return et

    def _resolve_field_type(self, kind: str, creator, apply: bool, cache: dict):
        if kind in cache:
            return cache[kind]
        from recaps.models import CustomRecapFieldType

        existing = None
        for ft in CustomRecapFieldType.objects.all():
            if _match_field_type(kind, (ft.name or "").lower()):
                existing = ft
                break
        if existing is None:
            if apply:
                existing = CustomRecapFieldType.objects.create(
                    name=kind, created_by=creator
                )
                self.stdout.write(f"    (created field type {kind!r})")
            else:
                existing = f"<would-create '{kind}'>"
        cache[kind] = existing
        return existing

    def _field_value_count(self, field) -> int:
        from recaps.models import CustomFieldValue

        return CustomFieldValue.objects.filter(custom_field=field).count()

    def _prune_obsolete_fields(self, template, keep_names: set[str], apply: bool):
        from recaps.models import CustomField

        if template is None or getattr(template, "id", None) is None:
            self.stdout.write("    (n/a until apply)")
            return 0, 0

        removed = 0
        kept_with_data = 0
        for field in CustomField.objects.filter(custom_recap_template=template):
            if field.name in keep_names:
                continue
            n_vals = self._field_value_count(field)
            if n_vals:
                kept_with_data += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"    keep obsolete {field.name!r} — "
                        f"{n_vals} value(s) on file"
                    )
                )
                continue
            if apply:
                field.delete()
                removed += 1
                self.stdout.write(f"    - pruned {field.name!r}")
            else:
                removed += 1
                self.stdout.write(f"    - would prune {field.name!r}")
        return removed, kept_with_data

    def _resolve_template(self, tenant, event_type, creator, apply: bool):
        from recaps.models import CustomRecapTemplate

        if event_type is None and not apply:
            return None, True, "would-create"

        existing = CustomRecapTemplate.objects.filter(
            tenant_id=tenant.id, name=TEMPLATE_NAME
        ).first()
        if existing is None and event_type is not None:
            existing = CustomRecapTemplate.objects.filter(
                tenant_id=tenant.id, event_type=event_type
            ).first()
        if existing is not None:
            return existing, False, "exists"

        if not apply:
            return None, True, "would-create"
        template = CustomRecapTemplate.objects.create(
            tenant_id=tenant.id,
            name=TEMPLATE_NAME,
            event_type=event_type,
            product_samples=True,
            sales_performance=False,
            layout={},
            created_by=creator,
        )
        return template, True, "created"

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        tenant = self._resolve_tenant(opts["tenant"].strip())
        creator = self._resolve_creator()
        options = _product_options(tenant)
        spec = _build_spec(options)

        self.stdout.write(
            f"Tenant  : [{tenant.id}] {tenant.name!r} / {tenant.slug!r}"
        )
        self.stdout.write(
            f"Program : {EVENT_TYPE_NAME} → {TEMPLATE_NAME!r}"
        )
        self.stdout.write(f"Products: {len(options)} SKU option(s)")

        ft_cache: dict = {}
        for _, fields in spec:
            for _, kind, _, _ in fields:
                self._resolve_field_type(kind, creator, apply, ft_cache)

        with transaction.atomic():
            event_type = self._ensure_event_type(
                tenant, EVENT_TYPE_NAME, creator, apply
            )
            if apply and event_type is None:
                raise CommandError(
                    f"Could not ensure event type {EVENT_TYPE_NAME!r}."
                )

            template, _made, how = self._resolve_template(
                tenant, event_type, creator, apply
            )
            if apply and template is not None:
                changed = []
                if template.name != TEMPLATE_NAME:
                    template.name = TEMPLATE_NAME
                    changed.append("name")
                if (
                    event_type is not None
                    and template.event_type_id != event_type.id
                ):
                    template.event_type = event_type
                    changed.append("event_type")
                if template.product_samples is not True:
                    template.product_samples = True
                    changed.append("product_samples")
                if changed:
                    changed.append("updated_at")
                    template.save(update_fields=changed)
                self.stdout.write(
                    f"Template {how.upper()} "
                    f"(id {template.id}, uuid {template.uuid})"
                )
            else:
                self.stdout.write(f"Template {how}")

            keep_names: set[str] = set()
            created = {"sections": 0, "fields": 0}
            updated = {"fields": 0, "sections": 0}

            from recaps.models import CustomField, RecapSection

            for section_name, fields in spec:
                s_idx = SECTION_ORDER.get(section_name, 99)
                self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
                section = None
                if apply and template is not None:
                    section, made_sec = RecapSection.objects.get_or_create(
                        tenant_id=tenant.id,
                        name=section_name,
                        defaults={"order": s_idx, "created_by": creator},
                    )
                    if made_sec:
                        created["sections"] += 1
                    elif section.order != s_idx:
                        section.order = s_idx
                        section.save(update_fields=["order", "updated_at"])
                        updated["sections"] += 1

                for f_idx, (fname, kind, required, field_opts) in enumerate(
                    fields
                ):
                    keep_names.add(fname)
                    ft = ft_cache[kind]
                    req = "REQUIRED" if required else "optional"
                    opt = (
                        f" options=[{len(field_opts)} SKUs]"
                        if field_opts and len(field_opts) > 5
                        else (f" options={field_opts}" if field_opts else "")
                    )
                    self.stdout.write(f"    - {fname!r}  [{kind}] {req}{opt}")
                    if not apply or template is None:
                        continue
                    field, made_f = CustomField.objects.get_or_create(
                        custom_recap_template=template,
                        name=fname,
                        defaults={
                            "recap_section": section,
                            "custom_field_type": ft,
                            "required": required,
                            "options": list(field_opts),
                            "order": f_idx,
                            "created_by": creator,
                        },
                    )
                    if made_f:
                        created["fields"] += 1
                    else:
                        changed = []
                        if field.custom_field_type_id != getattr(ft, "id", None):
                            field.custom_field_type = ft
                            changed.append("custom_field_type")
                        if field.required != required:
                            field.required = required
                            changed.append("required")
                        if list(field.options or []) != list(field_opts):
                            field.options = list(field_opts)
                            changed.append("options")
                        if field.order != f_idx:
                            field.order = f_idx
                            changed.append("order")
                        if (
                            section is not None
                            and field.recap_section_id != section.id
                        ):
                            field.recap_section = section
                            changed.append("recap_section")
                        if changed:
                            changed.append("updated_at")
                            field.save(update_fields=changed)
                            updated["fields"] += 1

            self.stdout.write("\nObsolete fields (not in SPEC):")
            self._prune_obsolete_fields(template, keep_names, apply=apply)

            self.stdout.write("")
            self.stdout.write(
                f"sections +{created['sections']}  fields +{created['fields']} "
                f"~{updated['fields']}"
            )

            if not apply:
                self.stdout.write("")
                self.stdout.write("DRY RUN — re-run with --apply to write.")
                transaction.set_rollback(True)
                return

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {TEMPLATE_NAME!r}. Next: "
                "setup_ld_retail_checkin --apply so the standing LD- link "
                "offers Product Seeding."
            )
        )
