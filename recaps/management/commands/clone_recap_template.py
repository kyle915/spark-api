"""Copy a CustomRecapTemplate (sections + fields) from one tenant to another.

Written for "give Torch the same recap Liquid Death retail uses", but nothing
here is brand-specific — onboarding a client who should report exactly like an
existing one is the common case, and hand-authoring a second seeder per brand
is how two templates silently drift apart.

WHAT IS AND ISN'T COPIED
    Copied: template name, product_samples / sales_performance flags, layout,
    every RecapSection (name + order) and every CustomField (name, type,
    required, order, options).

    NOT copied: recaps. This clones the FORM, never anyone's submitted data.

    RecapSection is tenant-scoped, so the target gets its own sections rather
    than pointing at the source tenant's — sharing them would leak one client's
    structure into another's editor. CustomRecapFieldType is global and is
    reused as-is.

THREE MODES, ALL SAFE BY DEFAULT
    --source-tenant alone      : list that tenant's templates and exit
    + --source-template-id     : dump the full structure and what would be made
    + --target-tenant-id --apply : write it

    Dry-run is the default everywhere; --apply is the only thing that writes.
    Idempotent: re-running matches sections/fields by name and creates only
    what is missing, so an interrupted run can simply be repeated.

Usage::

    python manage.py clone_recap_template --source-tenant "liquid death"
    python manage.py clone_recap_template --source-template-id 9 --target-tenant-id 17
    python manage.py clone_recap_template --source-template-id 9 --target-tenant-id 17 \
        --event-type "Retail Sampling" --name "Torch THC Retail Recap" --apply
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from events.models import EventType
from recaps.models import (
    CustomField,
    CustomRecapTemplate,
    RecapSection,
)
from tenants.models import Tenant

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Copy a recap template (sections + fields) between tenants. "
        "Lists / dumps / clones; dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-tenant",
            dest="source_tenant",
            default=None,
            help="Name/slug substring; lists that tenant's templates and exits.",
        )
        parser.add_argument(
            "--source-template-id",
            dest="source_template_id",
            type=int,
            default=None,
            help="Template to copy FROM (get the id from --source-tenant).",
        )
        parser.add_argument(
            "--target-tenant-id",
            dest="target_tenant_id",
            type=int,
            default=None,
            help="Tenant to copy INTO. Id only — names are ambiguous.",
        )
        parser.add_argument(
            "--name",
            default=None,
            help="Name for the new template (default: the source's name).",
        )
        parser.add_argument(
            "--event-type",
            dest="event_type",
            default=None,
            help=(
                "EventType name on the TARGET tenant to attach the template to. "
                "Default: the source template's event type name."
            ),
        )
        parser.add_argument(
            "--owner-email",
            dest="owner_email",
            default=None,
            help="created_by for new rows. Defaults to the source template's owner.",
        )
        parser.add_argument(
            "--replace-text",
            dest="replace_text",
            action="append",
            default=[],
            help=(
                "OLD=NEW substring swap applied to the template name and every "
                "field label. Repeatable. Use it for brand names baked into "
                "question wording, e.g. --replace-text 'Liquid Death=Torch THC'."
            ),
        )
        parser.add_argument(
            "--products-from-target",
            dest="products_from_target",
            action="store_true",
            help=(
                "Repopulate the choice field named by --products-field with the "
                "TARGET tenant's own products. Without this, a clone hands the "
                "new client a dropdown of the source brand's SKUs."
            ),
        )
        parser.add_argument(
            "--products-field",
            dest="products_field",
            default="Products Sampled",
            help="Field whose options --products-from-target rewrites.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])

        if opts["source_tenant"] and not opts["source_template_id"]:
            self._list_templates(opts["source_tenant"])
            return

        if not opts["source_template_id"]:
            raise CommandError(
                "Give --source-tenant to list templates, or --source-template-id "
                "to dump/clone one."
            )

        source = (
            CustomRecapTemplate.objects.filter(id=opts["source_template_id"])
            .select_related("tenant", "event_type", "created_by")
            .first()
        )
        if source is None:
            raise CommandError(f"No template with id={opts['source_template_id']}.")

        sections, fields_by_section = self._read_structure(source)

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"SOURCE: [{source.id}] {source.name!r}\n"
            f"        tenant [{source.tenant_id}] {source.tenant.name!r}  "
            f"event_type={source.event_type.name!r}\n"
            f"        product_samples={source.product_samples}  "
            f"sales_performance={source.sales_performance}"
        )
        self.stdout.write("=" * 72)
        self._print_structure(sections, fields_by_section)

        if not opts["target_tenant_id"]:
            self.stdout.write(
                "\nNo --target-tenant-id given — structure dump only, nothing "
                "was written. Add --target-tenant-id to see the clone plan."
            )
            return

        target = Tenant.objects.filter(id=opts["target_tenant_id"]).first()
        if target is None:
            raise CommandError(f"No tenant with id={opts['target_tenant_id']}.")
        if target.id == source.tenant_id:
            raise CommandError(
                "Source and target are the same tenant; that would duplicate the "
                "template rather than copy it to a client."
            )

        owner = self._resolve_owner(opts["owner_email"], source)
        swaps = self._parse_swaps(opts["replace_text"])
        new_name = opts["name"] or self._swap(source.name, swaps)

        target_options: list[str] | None = None
        if opts["products_from_target"]:
            target_options = self._target_product_options(opts["target_tenant_id"])
        et_name = opts["event_type"] or source.event_type.name
        event_type = EventType.objects.filter(
            tenant_id=target.id, name__iexact=et_name
        ).first()

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TARGET: [{target.id}] {target.name!r}\n"
            f"        template name : {new_name!r}\n"
            f"        event type    : {et_name!r} "
            f"{'(found id=%d)' % event_type.id if event_type else '(MISSING)'}\n"
            f"        created_by    : {owner.email}\n"
            f"MODE  : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)

        if event_type is None:
            available = list(
                EventType.objects.filter(tenant_id=target.id)
                .order_by("name")
                .values_list("name", flat=True)
            )
            raise CommandError(
                f"Tenant {target.id} has no EventType named {et_name!r}. "
                f"It has: {available or 'none'}. Pass --event-type with one of "
                "those, or create the event type first — a template attached to "
                "the wrong event type serves the wrong form to BAs."
            )

        clash = CustomRecapTemplate.objects.filter(
            tenant_id=target.id, name__iexact=new_name
        ).first()
        if clash:
            self.stdout.write(
                self.style.WARNING(
                    f"  Template {new_name!r} already exists on this tenant "
                    f"(id={clash.id}); reusing it and filling in anything missing."
                )
            )

        if swaps:
            self.stdout.write("\n  Text swaps applied to the name and every label:")
            for a, b in swaps:
                self.stdout.write(f"    {a!r} -> {b!r}")
            for section in sections:
                for f in fields_by_section.get(section.id, []):
                    swapped = self._swap(f.name, swaps)
                    if swapped != f.name:
                        self.stdout.write(f"      {f.name!r}\n        -> {swapped!r}")

        if target_options is not None:
            self.stdout.write(
                f"\n  {opts['products_field']!r} options replaced with "
                f"{len(target_options)} product(s) from the target tenant:"
            )
            for opt in target_options[:6]:
                self.stdout.write(f"    - {opt}")
            if len(target_options) > 6:
                self.stdout.write(f"    ... and {len(target_options) - 6} more")
            if not target_options:
                raise CommandError(
                    "--products-from-target was given but the target tenant has "
                    "no products. Seed its catalog first, or the field would end "
                    "up with an empty dropdown."
                )

        if not apply:
            n_fields = sum(len(v) for v in fields_by_section.values())
            self.stdout.write(
                f"\nDRY-RUN — would create/confirm 1 template, {len(sections)} "
                f"section(s) and {n_fields} field(s) on tenant [{target.id}]. "
                "Re-run with --apply to write."
            )
            return

        self._clone(
            source, sections, fields_by_section, target, owner, new_name,
            event_type, swaps, target_options, opts["products_field"],
        )

    # ------------------------------------------------------------------

    def _list_templates(self, needle: str) -> None:
        tenants = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if not tenants:
            raise CommandError(f"No tenant matches {needle!r}.")

        for tenant in tenants:
            self.stdout.write("=" * 72)
            self.stdout.write(f"[{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")
            self.stdout.write("=" * 72)
            templates = (
                CustomRecapTemplate.objects.filter(tenant_id=tenant.id)
                .select_related("event_type")
                .order_by("id")
            )
            if not templates:
                self.stdout.write("  (no recap templates)")
                continue
            for t in templates:
                n_fields = CustomField.objects.filter(
                    custom_recap_template_id=t.id
                ).count()
                n_sections = (
                    CustomField.objects.filter(custom_recap_template_id=t.id)
                    .values("recap_section_id")
                    .distinct()
                    .count()
                )
                self.stdout.write(
                    f"  id={t.id:<4} {t.name!r}\n"
                    f"        event_type={t.event_type.name!r}  "
                    f"{n_sections} section(s), {n_fields} field(s)  "
                    f"product_samples={t.product_samples} "
                    f"sales_performance={t.sales_performance}"
                )
        self.stdout.write(
            "\nRead-only. Re-run with --source-template-id <id> to dump one."
        )

    def _read_structure(self, template):
        """Return (ordered sections, {section_id: ordered fields})."""
        fields = list(
            CustomField.objects.filter(custom_recap_template_id=template.id)
            .select_related("recap_section", "custom_field_type")
            .order_by("recap_section__order", "recap_section_id", "order", "id")
        )
        sections, seen = [], set()
        by_section: dict[int, list] = {}
        for f in fields:
            if f.recap_section_id not in seen:
                seen.add(f.recap_section_id)
                sections.append(f.recap_section)
            by_section.setdefault(f.recap_section_id, []).append(f)
        sections.sort(key=lambda s: (s.order, s.id))
        return sections, by_section

    def _print_structure(self, sections, fields_by_section) -> None:
        total = 0
        for section in sections:
            rows = fields_by_section.get(section.id, [])
            total += len(rows)
            self.stdout.write(
                f"\n  [{section.order}] {section.name}  ({len(rows)} field(s))"
            )
            for f in rows:
                req = " *required" if f.required else ""
                opts_txt = f"  options={f.options}" if f.options else ""
                self.stdout.write(
                    f"      {f.order:>3}. {f.name}  "
                    f"<{f.custom_field_type.name}>{req}{opts_txt}"
                )
        self.stdout.write(
            f"\n  {len(sections)} section(s), {total} field(s) total."
        )

    def _parse_swaps(self, raw: list[str]) -> list[tuple[str, str]]:
        swaps = []
        for item in raw:
            if "=" not in item:
                raise CommandError(
                    f"--replace-text expects OLD=NEW, got {item!r}."
                )
            old, new = item.split("=", 1)
            if not old:
                raise CommandError("--replace-text OLD side cannot be empty.")
            swaps.append((old, new))
        return swaps

    def _swap(self, text: str, swaps: list[tuple[str, str]]) -> str:
        for old, new in swaps:
            text = text.replace(old, new)
        return text

    def _target_product_options(self, tenant_id: int) -> list[str]:
        """The target tenant's product names, grouped by line then name.

        The source's choice list is the source brand's SKUs; copying it
        verbatim is the "BA gets the wrong brand's form" failure in a
        different costume, and it is silent — the form renders fine, it just
        asks about someone else's products.
        """
        from events.models import Product

        return list(
            Product.objects.filter(tenant_id=tenant_id)
            .select_related("product_type")
            .order_by("product_type__name", "name")
            .values_list("name", flat=True)
        )

    def _resolve_owner(self, owner_email: str | None, source):
        if owner_email:
            owner = User.objects.filter(email__iexact=owner_email).first()
            if owner is None:
                raise CommandError(f"No user with email {owner_email!r}.")
            return owner
        return source.created_by

    def _clone(
        self, source, sections, fields_by_section, target, owner, new_name,
        event_type, swaps, target_options, products_field,
    ) -> None:
        made = {"template": 0, "sections": 0, "fields": 0}
        found = {"sections": 0, "fields": 0}

        with transaction.atomic():
            template, created = CustomRecapTemplate.objects.get_or_create(
                tenant_id=target.id,
                name=new_name,
                defaults={
                    "event_type": event_type,
                    "product_samples": source.product_samples,
                    "sales_performance": source.sales_performance,
                    "layout": source.layout or {},
                    "created_by": owner,
                },
            )
            made["template"] += int(created)
            self.stdout.write(
                f"  {'+' if created else '='} Template id={template.id} {new_name!r}"
            )

            for section in sections:
                # Sections belong to the tenant, not the template, so match on
                # (tenant, name) and reuse one the client already has.
                new_section, s_created = RecapSection.objects.get_or_create(
                    tenant_id=target.id,
                    name=section.name,
                    defaults={"order": section.order, "created_by": owner},
                )
                made["sections"] += int(s_created)
                found["sections"] += int(not s_created)
                self.stdout.write(
                    f"    {'+' if s_created else '='} Section id={new_section.id} "
                    f"{section.name!r}"
                )

                for f in fields_by_section.get(section.id, []):
                    field_name = self._swap(f.name, swaps)
                    options = list(f.options or [])
                    if (
                        target_options is not None
                        and field_name.strip().lower()
                        == products_field.strip().lower()
                    ):
                        options = target_options
                    new_field, f_created = CustomField.objects.get_or_create(
                        custom_recap_template_id=template.id,
                        recap_section_id=new_section.id,
                        name=field_name,
                        defaults={
                            "custom_field_type": f.custom_field_type,
                            "required": f.required,
                            "order": f.order,
                            "options": options,
                            "created_by": owner,
                        },
                    )
                    made["fields"] += int(f_created)
                    found["fields"] += int(not f_created)
                    # Re-running after the catalog grew should refresh the
                    # choice list, not leave a stale one behind.
                    if not f_created and options and new_field.options != options:
                        new_field.options = options
                        new_field.updated_by = owner
                        new_field.save(
                            update_fields=["options", "updated_by", "updated_at"]
                        )
                        self.stdout.write(
                            f"      ~ refreshed {field_name!r} options "
                            f"({len(options)} choice(s))"
                        )

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"Template: {'created' if made['template'] else 'reused'}.  "
                f"Sections: {made['sections']} created, {found['sections']} reused.  "
                f"Fields: {made['fields']} created, {found['fields']} already present."
            )
        )
        self.stdout.write(
            f"Tenant [{target.id}] {target.name!r} now has "
            f"{CustomRecapTemplate.objects.filter(tenant_id=target.id).count()} "
            "recap template(s)."
        )
        self.stdout.write("=" * 72)
