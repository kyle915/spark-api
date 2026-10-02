"""Torch THC: add "Dry demo?" and "People engaged" to the retail recap.

A dry demo is a shift where no product was tasted (no non-dosed cans on
hand, store won't allow sampling). Those recaps stay visible but are left
out of conversion, and "People engaged" keeps the talked-with count without
overloading "Total number of consumers sampled".

Adds both fields to Consumer Engagement on ``Torch THC-Retail Sampling``,
right around the consumers-sampled question:

    Dry demo? (no product tasted)      select No/Yes, required
    Total number of consumers sampled  (existing, stays required)
    People engaged                     integer, optional

Recap fields have no conditional-required rules, so consumers sampled stays
required; on a dry demo the BA enters 0 there (its placeholder says so when
it has none) and the talked-with count goes in People engaged.

Walk-up ``/checkin/TH-2HRV3D`` and agency ``/checkin/TH-AGENCY`` both use
this template — neither code is touched.

Idempotent. DRY-RUN by default; ``--apply`` writes.

    python manage.py add_torch_dry_demo_fields
    python manage.py add_torch_dry_demo_fields --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DEFAULT_TENANT = "torch-thc"
TEMPLATE_NAME = "Torch THC-Retail Sampling"
SECTION_NAME = "Consumer Engagement"
SAMPLED_FIELD_NAME = "Total number of consumers sampled"
SAMPLED_PLACEHOLDER = "Enter 0 on a dry demo (no product tasted)"

DRY_DEMO = {
    "name": "Dry demo? (no product tasted)",
    "type": "select",
    "required": True,
    "options": ["No", "Yes"],
    "placeholder": "",
}
PEOPLE_ENGAGED = {
    "name": "People engaged",
    "type": "FormInputInteger",
    "required": False,
    "options": [],
    "placeholder": "People you talked with about Torch, tasted or not",
}


class Command(BaseCommand):
    help = (
        "Torch THC: ensure 'Dry demo?' and 'People engaged' exist on the "
        "retail recap template. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--tenant",
            default=DEFAULT_TENANT,
            help="Tenant slug (exact), else request_url_name.",
        )

    @staticmethod
    def _resolve_tenant(ident: str):
        from tenants.models import Tenant

        for lookup in ("slug", "request_url_name"):
            matches = list(Tenant.objects.filter(**{lookup: ident}).order_by("id"))
            if len(matches) > 1:
                ids = ", ".join(str(t.id) for t in matches)
                raise CommandError(f"{len(matches)} tenants share {lookup}={ident!r} ({ids}).")
            if matches:
                return matches[0]
        raise CommandError(f"No tenant with slug or request_url_name {ident!r}.")

    def handle(self, *args, **opts):
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )

        apply = bool(opts["apply"])
        tenant = self._resolve_tenant((opts["tenant"] or DEFAULT_TENANT).strip())

        self.stdout.write("=" * 68)
        self.stdout.write(f"Tenant   : [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")
        self.stdout.write(
            f"Codes    : {tenant.checkin_code or '(none)'} — not reminted"
        )
        self.stdout.write(f"Mode     : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")

        templates = list(
            CustomRecapTemplate.objects.filter(tenant_id=tenant.id, name=TEMPLATE_NAME)
            .select_related("created_by")
            .order_by("id")
        )
        if len(templates) != 1:
            raise CommandError(
                f"Expected exactly one template named {TEMPLATE_NAME!r} on tenant "
                f"[{tenant.id}], found {len(templates)}."
            )
        tpl = templates[0]
        self.stdout.write(f"Template : [{tpl.id}] {tpl.name!r}")

        section = RecapSection.objects.filter(
            tenant_id=tenant.id, name__iexact=SECTION_NAME
        ).first()
        if section is None:
            raise CommandError(f"Section {SECTION_NAME!r} missing on tenant [{tenant.id}].")

        types = {}
        for spec in (DRY_DEMO, PEOPLE_ENGAGED):
            ftype = CustomRecapFieldType.objects.filter(name__iexact=spec["type"]).first()
            if ftype is None:
                raise CommandError(
                    f"No CustomRecapFieldType named {spec['type']!r}. "
                    "Run add_recap_template_fields --list-types."
                )
            types[spec["name"]] = ftype

        def _section_fields():
            return list(
                CustomField.objects.filter(
                    custom_recap_template_id=tpl.id, recap_section_id=section.id
                )
                .select_related("custom_field_type")
                .order_by("order", "id")
            )

        before = _section_fields()
        sampled = next((f for f in before if f.name == SAMPLED_FIELD_NAME), None)
        if sampled is None:
            raise CommandError(
                f"{SAMPLED_FIELD_NAME!r} not found in {SECTION_NAME!r} on template [{tpl.id}]."
            )

        self.stdout.write("=" * 68)
        self.stdout.write(f"{SECTION_NAME} — before:")
        for f in before:
            self._print_field(f)

        with transaction.atomic():
            made = []
            for spec in (DRY_DEMO, PEOPLE_ENGAGED):
                existing = CustomField.objects.filter(
                    custom_recap_template_id=tpl.id, name=spec["name"]
                ).first()
                if existing:
                    self.stdout.write(f"  = {spec['name']!r} already present [{existing.id}]")
                    continue
                self.stdout.write(
                    f"  + would add {spec['name']!r} ({spec['type']}, "
                    f"required={spec['required']}, options={spec['options']})"
                )
                if apply:
                    made.append(
                        CustomField.objects.create(
                            custom_recap_template_id=tpl.id,
                            recap_section_id=section.id,
                            custom_field_type=types[spec["name"]],
                            name=spec["name"],
                            required=spec["required"],
                            order=sampled.order,
                            options=spec["options"],
                            placeholder=spec["placeholder"],
                            created_by=tpl.created_by,
                        )
                    )

            if not (sampled.placeholder or "").strip():
                self.stdout.write(
                    f"  ~ {SAMPLED_FIELD_NAME!r} placeholder: '' -> {SAMPLED_PLACEHOLDER!r} "
                    "(still required)"
                )
                if apply:
                    sampled.placeholder = SAMPLED_PLACEHOLDER
                    sampled.save(update_fields=["placeholder"])

            # Dry demo? directly before consumers sampled, People engaged
            # directly after; everything else keeps its relative order.
            current = _section_fields() if apply else before
            by_name = {f.name: f for f in current}
            rest = [
                f
                for f in current
                if f.name not in (DRY_DEMO["name"], PEOPLE_ENGAGED["name"])
            ]
            desired = []
            for f in rest:
                if f.id == sampled.id:
                    if DRY_DEMO["name"] in by_name:
                        desired.append(by_name[DRY_DEMO["name"]])
                    desired.append(f)
                    if PEOPLE_ENGAGED["name"] in by_name:
                        desired.append(by_name[PEOPLE_ENGAGED["name"]])
                else:
                    desired.append(f)
            reorders = 0
            for idx, f in enumerate(desired, start=1):
                new_order = idx * 10
                if f.order != new_order:
                    reorders += 1
                    if apply:
                        f.order = new_order
                        f.save(update_fields=["order"])
            if reorders:
                self.stdout.write(f"  ~ renumber display order on {reorders} field(s) in section")

        self.stdout.write("=" * 68)
        if apply:
            self.stdout.write(f"{SECTION_NAME} — after:")
            for f in _section_fields():
                self._print_field(f)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. added={len(made)} "
                    f"({', '.join(f'[{f.id}] {f.name!r}' for f in made) or 'none'})"
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING("DRY-RUN complete — re-run with --apply to write.")
            )
        self._walkup_check(tenant)

    def _walkup_check(self, tenant) -> None:
        """Print the recap form exactly as the walk-up page receives it
        (``serialize_template``) for the tenant's latest walk-up event.
        Read-only."""
        from ambassadors.checkin_web import serialize_template
        from events.models import Event

        event = (
            Event.objects.filter(tenant_id=tenant.id, request__isnull=True)
            .order_by("-id")
            .first()
        )
        self.stdout.write("=" * 68)
        if event is None:
            self.stdout.write("Walk-up form check: no walk-up event on this tenant yet.")
            return
        payload = serialize_template(event)
        if payload is None:
            self.stdout.write(f"Walk-up form check: event [{event.id}] resolves no template.")
            return
        self.stdout.write(
            f"Walk-up form check — event [{event.id}] -> template [{payload['id']}] "
            f"{payload['name']!r}:"
        )
        for sec in payload["sections"]:
            if sec["name"].lower() != SECTION_NAME.lower():
                continue
            for f in sec["fields"]:
                extra = f" options={f['options']}" if f["options"] else ""
                hint = f" placeholder={f['placeholder']!r}" if f["placeholder"] else ""
                self.stdout.write(
                    f"    [{f['id']}] {f['type']:<16} req={f['required']!s:<5} "
                    f"{f['name']!r}{extra}{hint}"
                )

    def _print_field(self, f) -> None:
        ftype = getattr(f.custom_field_type, "name", "?")
        self.stdout.write(
            f"    [{f.id}] order={f.order} {ftype:<16} req={bool(f.required)!s:<5} "
            f"{f.name!r}" + (f" options={f.options}" if f.options else "")
        )
