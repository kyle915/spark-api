"""Torch THC: add the competitor-products feedback question.

Appends a longtext field to Feedback & Account Notes on every live Torch
CustomRecapTemplate (today: ``Torch THC-Retail Sampling``). Walk-up
``/checkin/TH-2HRV3D`` and Spark ops share that template — no remint.

Idempotent. DRY-RUN by default; ``--apply`` writes.

    python manage.py add_torch_competitor_feedback
    python manage.py add_torch_competitor_feedback --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

TENANT_SLUG = "torch-thc"
CHECKIN_CODE = "TH-2HRV3D"

SECTION_NAME = "Feedback & Account Notes"
FIELD_NAME = (
    "Ask consumers what competitor products they drink. "
    "And if so, which one. Why do you like it?"
)
FIELD_TYPE = "longtext"
FIELD_REQUIRED = False
# After the existing "which other beverage brands…" field (order 10).
FIELD_ORDER = 15


class Command(BaseCommand):
    help = (
        "Torch THC: ensure the competitor-products feedback question exists "
        "on live recap templates (Feedback & Account Notes)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--tenant", default=TENANT_SLUG)

    def handle(self, *args, **opts):
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )
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

        ftype = CustomRecapFieldType.objects.filter(name__iexact=FIELD_TYPE).first()
        if ftype is None:
            raise CommandError(
                f"No CustomRecapFieldType named {FIELD_TYPE!r}. "
                "Run add_recap_template_fields --list-types."
            )

        section = RecapSection.objects.filter(
            tenant_id=tenant.id, name__iexact=SECTION_NAME
        ).first()
        if section is None:
            have = list(
                RecapSection.objects.filter(tenant_id=tenant.id)
                .order_by("order", "id")
                .values_list("name", flat=True)
            )
            raise CommandError(
                f"Section {SECTION_NAME!r} missing on tenant. Has: {have}."
            )

        templates = list(
            CustomRecapTemplate.objects.filter(tenant_id=tenant.id).order_by("id")
        )
        if not templates:
            raise CommandError(f"No CustomRecapTemplate on tenant {slug!r}.")

        made = kept = 0
        for tpl in templates:
            existing = CustomField.objects.filter(
                custom_recap_template_id=tpl.id, name=FIELD_NAME
            ).first()
            if existing:
                kept += 1
                self.stdout.write(
                    f"  = [{tpl.id}] {tpl.name!r}: field already present "
                    f"[{existing.id}]"
                )
                continue

            self.stdout.write(
                f"  + [{tpl.id}] {tpl.name!r}: would add {FIELD_NAME!r} "
                f"({FIELD_TYPE}, required={FIELD_REQUIRED}, order={FIELD_ORDER})"
            )
            if not apply:
                continue

            with transaction.atomic():
                field = CustomField.objects.create(
                    custom_recap_template_id=tpl.id,
                    recap_section_id=section.id,
                    custom_field_type=ftype,
                    name=FIELD_NAME,
                    required=FIELD_REQUIRED,
                    order=FIELD_ORDER,
                    options=[],
                    created_by=tpl.created_by,
                )
            made += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"  + CustomField id={field.id} on template [{tpl.id}]"
                )
            )

        self.stdout.write("-" * 68)
        if apply:
            self.stdout.write(
                self.style.SUCCESS(f"Done. added={made} already_present={kept}")
            )
        else:
            pending = sum(
                1
                for tpl in templates
                if not CustomField.objects.filter(
                    custom_recap_template_id=tpl.id, name=FIELD_NAME
                ).exists()
            )
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN complete — would add {pending} field(s), "
                    f"{kept} already present. Re-run with --apply to write."
                )
            )
