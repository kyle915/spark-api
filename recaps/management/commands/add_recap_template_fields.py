"""Add questions to an existing CustomRecapTemplate.

The web template editor can do this too; this exists for the cases the editor
is awkward for — adding several fields at once, setting a long option list, or
doing it against prod from a workflow without clicking through the UI.

Fields are described as JSON so one call can add several, and so the exact
spec is visible in the run log afterwards:

    --spec '[{"section": "Consumer Engagement",
              "type": "select",
              "name": "Roughly what percentage ...?",
              "required": true,
              "options": ["0-25%", "26-50%", "51-75%", "76-100%"],
              "order": 8}]'

``--list-types`` prints the CustomRecapFieldType rows first, because the type
token has to match one that exists — the renderers key off it, and a field
created with an unknown type renders as nothing at all.

Sections are matched by name on the template's tenant and must already exist,
unless ``--create-sections`` is passed. A typo would otherwise silently create
a second section with a near-identical name and split the form in two.

DRY-RUN by default; --apply writes. Idempotent — a field with the same name in
the same section is left alone (its options are refreshed if the spec changed).

Usage::

    python manage.py add_recap_template_fields --list-types
    python manage.py add_recap_template_fields --template-id 20 --spec '[...]'
    python manage.py add_recap_template_fields --template-id 20 --spec '[...]' --apply
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    RecapSection,
)

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Add fields to an existing recap template from a JSON spec. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--list-types",
            dest="list_types",
            action="store_true",
            help="Print the available CustomRecapFieldType rows and exit.",
        )
        parser.add_argument(
            "--template-id",
            dest="template_id",
            type=int,
            default=None,
            help="CustomRecapTemplate to add to.",
        )
        parser.add_argument(
            "--spec",
            default=None,
            help="JSON array of {section, type, name, required, options, order}.",
        )
        parser.add_argument(
            "--create-sections",
            dest="create_sections",
            action="store_true",
            help="Create a named section if the tenant doesn't have it yet.",
        )
        parser.add_argument(
            "--owner-email",
            dest="owner_email",
            default=None,
            help="created_by for new rows. Defaults to the template's owner.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        if opts["list_types"]:
            self._list_types()
            return

        apply = bool(opts["apply"])
        if not opts["template_id"] or not opts["spec"]:
            raise CommandError(
                "--template-id and --spec are both required (or use --list-types)."
            )

        template = (
            CustomRecapTemplate.objects.filter(id=opts["template_id"])
            .select_related("tenant", "created_by")
            .first()
        )
        if template is None:
            raise CommandError(f"No template with id={opts['template_id']}.")

        try:
            spec = json.loads(opts["spec"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"--spec is not valid JSON: {exc}") from exc
        if not isinstance(spec, list) or not spec:
            raise CommandError("--spec must be a non-empty JSON array.")

        owner = self._resolve_owner(opts["owner_email"], template)
        types = {t.name.strip().lower(): t for t in CustomRecapFieldType.objects.all()}

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TEMPLATE: [{template.id}] {template.name!r}\n"
            f"TENANT  : [{template.tenant_id}] {template.tenant.name!r}\n"
            f"MODE    : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)

        # Resolve everything up front so a bad spec fails before any writes.
        resolved = []
        for i, item in enumerate(spec, start=1):
            for key in ("section", "type", "name"):
                if not item.get(key):
                    raise CommandError(f"spec[{i}] is missing {key!r}.")

            ftype = types.get(str(item["type"]).strip().lower())
            if ftype is None:
                raise CommandError(
                    f"spec[{i}] type {item['type']!r} is not a known field type. "
                    f"Known: {sorted(t.name for t in types.values())}. "
                    "Run --list-types."
                )

            section = RecapSection.objects.filter(
                tenant_id=template.tenant_id, name__iexact=item["section"].strip()
            ).first()
            if section is None and not opts["create_sections"]:
                have = list(
                    RecapSection.objects.filter(tenant_id=template.tenant_id)
                    .order_by("order", "id")
                    .values_list("name", flat=True)
                )
                raise CommandError(
                    f"spec[{i}] section {item['section']!r} does not exist on this "
                    f"tenant. It has: {have}. Fix the name, or pass "
                    "--create-sections if a new section is genuinely wanted."
                )

            options = item.get("options") or []
            if options and not isinstance(options, list):
                raise CommandError(f"spec[{i}] options must be a JSON array.")

            existing = (
                CustomField.objects.filter(
                    custom_recap_template_id=template.id,
                    name=item["name"],
                ).first()
                if section
                else None
            )
            resolved.append(
                {
                    "name": item["name"],
                    "type": ftype,
                    "section_name": item["section"].strip(),
                    "section": section,
                    "required": bool(item.get("required", False)),
                    "order": int(item.get("order", 0)),
                    "options": options,
                    "existing": existing,
                }
            )

        self.stdout.write("")
        for r in resolved:
            state = "EXISTS" if r["existing"] else "new"
            sect = r["section_name"] + ("" if r["section"] else "  (WOULD CREATE)")
            self.stdout.write(
                f"  [{state}] {r['name']}\n"
                f"        section={sect}  type={r['type'].name}  "
                f"required={r['required']}  order={r['order']}"
            )
            if r["options"]:
                self.stdout.write(f"        options={r['options']}")

        if not apply:
            n_new = sum(1 for r in resolved if not r["existing"])
            self.stdout.write(
                f"\nDRY-RUN — would add {n_new} field(s) and leave "
                f"{len(resolved) - n_new} already present. Re-run with --apply."
            )
            return

        made = updated = kept = 0
        with transaction.atomic():
            for r in resolved:
                section = r["section"]
                if section is None:
                    section, _ = RecapSection.objects.get_or_create(
                        tenant_id=template.tenant_id,
                        name=r["section_name"],
                        defaults={"order": 90, "created_by": owner},
                    )
                    self.stdout.write(f"  + Section id={section.id} {section.name!r}")

                field = r["existing"]
                if field is None:
                    field = CustomField.objects.create(
                        custom_recap_template_id=template.id,
                        recap_section_id=section.id,
                        custom_field_type=r["type"],
                        name=r["name"],
                        required=r["required"],
                        order=r["order"],
                        options=r["options"],
                        created_by=owner,
                    )
                    made += 1
                    self.stdout.write(f"  + CustomField id={field.id} {r['name']!r}")
                elif r["options"] and field.options != r["options"]:
                    field.options = r["options"]
                    field.updated_by = owner
                    field.save(update_fields=["options", "updated_by", "updated_at"])
                    updated += 1
                    self.stdout.write(
                        f"  ~ CustomField id={field.id} options refreshed "
                        f"({len(r['options'])} choice(s))"
                    )
                else:
                    kept += 1
                    self.stdout.write(f"  = CustomField id={field.id} (unchanged)")

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"{made} field(s) added, {updated} refreshed, {kept} unchanged. "
                f"Template [{template.id}] now has "
                f"{CustomField.objects.filter(custom_recap_template_id=template.id).count()} "
                "field(s)."
            )
        )
        self.stdout.write("=" * 72)

    # ------------------------------------------------------------------

    def _list_types(self) -> None:
        rows = CustomRecapFieldType.objects.order_by("name")
        self.stdout.write("=" * 72)
        self.stdout.write(f"CustomRecapFieldType rows ({rows.count()}) — global:")
        self.stdout.write("=" * 72)
        for t in rows:
            n = CustomField.objects.filter(custom_field_type_id=t.id).count()
            self.stdout.write(f"  id={t.id:<4} {t.name!r}  used by {n} field(s)")
        self.stdout.write(
            "\nUse one of these names verbatim as the spec's \"type\". Read-only."
        )

    def _resolve_owner(self, owner_email: str | None, template):
        if owner_email:
            owner = User.objects.filter(email__iexact=owner_email).first()
            if owner is None:
                raise CommandError(f"No user with email {owner_email!r}.")
            return owner
        return template.created_by
