"""Seed Neutonic Event Activation recap template.

Mirrors Mark Anthony Brands / Liquid Death Event Activation field-for-field,
with Neutonic brand copy and Products Sampled options from the live Neutonic
Product catalog (no hardcoded second SKU list). GraphQL also refreshes
Products Sampled from the catalog at read time.

Photos stay on the walk-up ``FileRecapCategory`` buckets from
``setup_neutonic_checkin``; this SPEC does NOT add template image fields.

Only seeds **Event Activation** — Neutonic's existing Retail / Event /
On-Premise forms are left alone.

DRY-RUN by default. Run via ``/internal/cron/seed-neutonic-recap-template``
(or the "Seed Neutonic recap template" GitHub Action) against prod.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

EVENT_TEMPLATE_NAME = "Neutonic-Event Activation"
EVENT_PROGRAM = "Event Activation"

SECTION_ORDER = {
    "Consumer Engagement": 0,
    "Feedback & Account Notes": 1,
    "Products Sampled": 3,
}

# Field-for-field off MAB/LD Event Activation, Neutonic brand copy.
# Products Sampled options are filled at seed time from the tenant catalog.
EVENT_SPEC_FIELDS: list[tuple[str, list[tuple[str, str, bool]]]] = [
    (
        "Consumer Engagement",
        [
            (
                "How many consumers would be willing to purchase the product "
                "after tasing it?",
                "number",
                True,
            ),
            (
                "How many consumers that were engaged with knew about "
                "Neutonic product/brand?",
                "number",
                True,
            ),
            (
                "How many consumers had tried a Neutonic product before?",
                "number",
                True,
            ),
            ("How many TOTAL consumers did you sample?", "number", True),
        ],
    ),
    (
        "Feedback & Account Notes",
        [
            ("Demographics", "longtext", True),
            (
                "What were the top 5 frequently asked questions you received "
                "from consumers?",
                "longtext",
                True,
            ),
            ("Helpful feedback", "longtext", True),
        ],
    ),
]


def build_event_spec(product_opts: list[str]) -> list[
    tuple[str, list[tuple[str, str, bool, list[str]]]]
]:
    """Assemble EVENT_SPEC with catalog-backed Products Sampled options."""
    sections: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = []
    for section_name, fields in EVENT_SPEC_FIELDS:
        sections.append(
            (
                section_name,
                [(name, kind, req, []) for name, kind, req in fields],
            )
        )
    sections.append(
        (
            "Products Sampled",
            [("Products Sampled", "multiselect", True, list(product_opts))],
        )
    )
    return sections


# Module-level default (empty catalog) for import-time tests; seed fills live.
EVENT_SPEC = build_event_spec([])

PROGRAMS: list[dict] = [
    {
        "event_type": EVENT_PROGRAM,
        "template_name": EVENT_TEMPLATE_NAME,
        "archive_legacy": False,
    },
]


def _match_field_type(kind: str, name_lower: str) -> bool:
    """Does an existing CustomRecapFieldType named `name_lower` serve `kind`?

    Mirrors the FE `customFieldKind` fuzzy rules so a field renders as the
    intended control. Order matters: multiselect before select (both contain
    'select'); text is an exact match so it never swallows longtext/multiselect.
    """
    if kind == "image":
        return any(t in name_lower for t in ("image", "photo", "img"))
    if kind == "multiselect":
        return name_lower == "multiselect" or "multi" in name_lower
    if kind == "select":
        return name_lower == "select" or "dropdown" in name_lower
    if kind == "number":
        return name_lower == "number" or "num" in name_lower or "integer" in name_lower
    if kind == "longtext":
        return "long" in name_lower or "textarea" in name_lower or "paragraph" in name_lower
    if kind == "text":
        return name_lower == "text"
    return False


class Command(BaseCommand):
    help = (
        "Seed Neutonic's Event Activation recap template "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="neutonic",
            help=(
                "tenant name/slug substring (case-insensitive). "
                "Default: 'neutonic'."
            ),
        )
        parser.add_argument(
            "--template-name",
            dest="template_name",
            default=None,
            help="If set, only seed programs whose template name matches.",
        )
        parser.add_argument(
            "--event-type",
            dest="event_type",
            default=None,
            help="If set, only seed programs whose event type matches.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing).",
        )

    def _resolve_tenant(self, needle: str):
        from tenants.models import Tenant

        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle)
                | Q(slug__icontains=needle)
                | Q(slug__iexact="neutonic")
            )
            .distinct()
            .order_by("id")
        )
        if len(matches) > 1:
            exact = [t for t in matches if (t.slug or "").lower() == "neutonic"]
            if len(exact) == 1:
                return exact[0]
        if len(matches) == 1:
            return matches[0]
        self.stdout.write(self.style.WARNING("Tenants in this database:"))
        for t in Tenant.objects.order_by("id"):
            self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
        if not matches:
            raise CommandError(
                f"No tenant matches {needle!r}. Neutonic needs onboarding first."
            )
        raise CommandError(
            f"{needle!r} matched {len(matches)} tenants "
            f"({', '.join(repr(t.slug) for t in matches)}) — narrow --tenant."
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

    def _product_options(self, tenant) -> list[str]:
        from recaps.products_sampled import products_sampled_options_for_tenant

        opts = products_sampled_options_for_tenant(tenant)
        if opts:
            self.stdout.write(
                f"Products   : {len(opts)} from Neutonic Product catalog"
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Products   : catalog empty — Products Sampled options "
                    "will refresh from catalog at form render time"
                )
            )
        return opts

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
        et = EventType.objects.create(name=name, tenant=tenant, created_by=creator)
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

    def _spec_field_names(self, spec) -> set[str]:
        return {fname for _, fields in spec for fname, *_ in fields}

    def _field_value_count(self, field) -> int:
        from recaps.models import CustomFieldValue

        return CustomFieldValue.objects.filter(custom_field=field).count()

    def _resolve_template(
        self, tenant, template_name: str, event_type, creator, apply: bool
    ):
        from recaps.models import CustomRecapTemplate

        if event_type is None and not apply:
            return None, True, "would-create"

        existing = CustomRecapTemplate.objects.filter(
            tenant_id=tenant.id, name=template_name, event_type=event_type
        ).first()
        if existing is not None:
            return existing, False, "exists"

        if not apply:
            return None, True, "would-create"
        template = CustomRecapTemplate.objects.create(
            tenant_id=tenant.id,
            name=template_name,
            event_type=event_type,
            product_samples=True,
            sales_performance=False,
            layout={},
            created_by=creator,
        )
        return template, True, "created"

    def _prune_obsolete_fields(
        self, template, keep_names: set[str], apply: bool
    ) -> tuple[int, int]:
        from recaps.models import CustomField

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
                        f"    keep obsolete {field.name!r} — {n_vals} value(s) on file"
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

    def _programs_to_seed(self, opts) -> list[dict]:
        et_hint = (opts.get("event_type") or "").strip().lower()
        name_hint = (opts.get("template_name") or "").strip().lower()
        out = []
        for program in PROGRAMS:
            if et_hint and et_hint not in program["event_type"].lower():
                continue
            if name_hint and name_hint not in program["template_name"].lower():
                continue
            out.append(program)
        if not out:
            raise CommandError(
                "No Neutonic programs matched "
                f"--event-type={opts.get('event_type')!r} "
                f"--template-name={opts.get('template_name')!r}."
            )
        return out

    def _seed_program(
        self,
        tenant,
        creator,
        apply: bool,
        program: dict,
        ft_cache: dict,
        product_opts: list[str],
    ) -> None:
        from recaps.models import CustomField, RecapSection

        spec = build_event_spec(product_opts)
        template_name = program["template_name"]
        event_type_name = program["event_type"]

        self.stdout.write("\n" + "-" * 68)
        self.stdout.write(f"PROGRAM: {event_type_name} → {template_name!r}")
        self.stdout.write("-" * 68)

        event_type = self._ensure_event_type(
            tenant, event_type_name, creator, apply
        )
        if event_type is None and apply:
            raise CommandError(
                f"Could not ensure event type {event_type_name!r}."
            )

        for _, fields in spec:
            for _, kind, _, _ in fields:
                self._resolve_field_type(kind, creator, apply, ft_cache)

        created = {"sections": 0, "fields": 0}
        updated = {"sections": 0, "fields": 0}

        template, made, how = self._resolve_template(
            tenant, template_name, event_type, creator, apply
        )
        if apply and template is not None:
            self.stdout.write(
                f"Template {how.upper()} "
                f"(id {template.id}, uuid {template.uuid})"
            )
            if not made and template.product_samples is not True:
                template.product_samples = True
                template.save(update_fields=["product_samples", "updated_at"])
        else:
            self.stdout.write(f"Template {how}")

        keep_names: set[str] = set()
        for section_name, fields in spec:
            s_idx = SECTION_ORDER.get(section_name, 99)
            self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
            section = None
            if apply:
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

            for f_idx, (fname, kind, required, options) in enumerate(fields):
                keep_names.add(fname)
                ft = ft_cache[kind]
                req = "REQUIRED" if required else "optional"
                opt = f" options={len(options)} choices" if options else ""
                if kind == "multiselect" and not options:
                    opt = " options=(catalog at render)"
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}{opt}")
                if not apply:
                    continue
                field, made_f = CustomField.objects.get_or_create(
                    custom_recap_template=template,
                    recap_section=section,
                    name=fname,
                    defaults={
                        "custom_field_type": ft,
                        "required": required,
                        "options": list(options),
                        "order": f_idx,
                        "created_by": creator,
                    },
                )
                if made_f:
                    created["fields"] += 1
                else:
                    changed = []
                    if field.custom_field_type_id != ft.id:
                        field.custom_field_type = ft
                        changed.append("custom_field_type")
                    if field.required != required:
                        field.required = required
                        changed.append("required")
                    if list(field.options or []) != list(options):
                        field.options = list(options)
                        changed.append("options")
                    if field.order != f_idx:
                        field.order = f_idx
                        changed.append("order")
                    if field.recap_section_id != section.id:
                        field.recap_section = section
                        changed.append("recap_section")
                    if changed:
                        changed.append("updated_at")
                        field.save(update_fields=changed)
                        updated["fields"] += 1

        self.stdout.write("\nObsolete fields (not in this program SPEC):")
        if apply and template is not None:
            pruned, kept = self._prune_obsolete_fields(
                template, keep_names, apply=True
            )
        else:
            from recaps.models import CustomRecapTemplate

            preview = CustomRecapTemplate.objects.filter(
                tenant_id=tenant.id, name=template_name
            ).first()
            if preview is None:
                self.stdout.write("    (none — no existing template)")
                pruned, kept = 0, 0
            else:
                pruned, kept = self._prune_obsolete_fields(
                    preview, keep_names, apply=False
                )

        total_fields = sum(len(f) for _, f in spec)
        if apply and template is not None:
            self.stdout.write(
                self.style.SUCCESS(
                    f"  APPLIED {template_name!r} — "
                    f"sections +{created['sections']}/~{updated['sections']} · "
                    f"fields +{created['fields']}/~{updated['fields']} · "
                    f"pruned {pruned} · kept-with-data {kept}."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"  DRY-RUN {template_name!r} — "
                    f"{len(spec)} sections + {total_fields} fields "
                    f"(prune ~{pruned}, keep-with-data {kept})."
                )
            )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        tenant = self._resolve_tenant(opts["tenant"])
        creator = self._resolve_creator()
        programs = self._programs_to_seed(opts)
        product_opts = self._product_options(tenant)

        self.stdout.write("=" * 68)
        self.stdout.write(
            f"Tenant     : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
        )
        self.stdout.write(
            "Programs   : "
            + ", ".join(p["event_type"] for p in programs)
        )
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        ft_cache: dict = {}
        if apply:
            with transaction.atomic():
                for program in programs:
                    self._seed_program(
                        tenant, creator, True, program, ft_cache, product_opts
                    )
        else:
            for program in programs:
                self._seed_program(
                    tenant, creator, False, program, ft_cache, product_opts
                )

        self.stdout.write("")
        if apply:
            self.stdout.write(
                self.style.SUCCESS(
                    f"APPLIED — seeded {len(programs)} Neutonic "
                    "Event Activation template(s)."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "DRY-RUN complete — nothing written. Re-run with --apply."
                )
            )
