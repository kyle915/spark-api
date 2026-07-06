"""Set the allowed options on a custom-recap CHOICE field (select/multiselect).

The recap template builder only lets an admin set a choice field's options at
CREATE time — there's no UI to edit them later. So a multiselect field created
without options renders "No options configured for this field yet" on the app
and can't be answered (Feel Free's "Which products were sampled?" field).

This sets `CustomField.options` on the matched field. Template-level, so it
fixes the field for EVERY recap that uses the template, and preserves the field
+ any existing answers (unlike deleting and re-adding the field in the builder).

Match: the tenant's CHOICE fields (custom_field_type.name in select/multiselect)
whose NAME contains ``--field-contains``. Refuses to write unless EXACTLY ONE
field matches (no guessing).

Options source: ``--options "A,B,C"`` if given, else the tenant's Product names
(``--from-products``, the default) — e.g. Feel Free -> "Classic Tonic, Kava Mate".

DRY-RUN by default; ``--apply`` to write. ``--overwrite`` to replace options
that are already non-empty (default: only fill EMPTY options).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Set a custom-recap choice field's options (dry-run by default; "
        "--apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", required=True)
        parser.add_argument(
            "--field-contains",
            required=True,
            help="case-insensitive substring of the choice field NAME to target",
        )
        parser.add_argument(
            "--options",
            default="",
            help="comma-separated options; omit to use the tenant's Product names",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write; without it, only report the planned change",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="replace options even if already non-empty (default: fill only empty)",
        )

    def handle(self, *args, **opts):
        from events.models import Product
        from recaps.models import CustomField
        from tenants.models import Tenant

        slug = opts["tenant_slug"]
        needle = opts["field_contains"]
        apply = bool(opts["apply"])
        overwrite = bool(opts["overwrite"])
        w = self.stdout.write

        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f"no tenant with slug {slug!r}")

        # Resolve the option list: explicit --options, else the tenant's products.
        if opts["options"].strip():
            new_options = [o.strip() for o in opts["options"].split(",") if o.strip()]
            source = "--options"
        else:
            new_options = list(
                Product.objects.filter(tenant=tenant)
                .order_by("id")
                .values_list("name", flat=True)
            )
            source = "tenant products"
        if not new_options:
            raise CommandError(
                "no options to set (empty --options and tenant has no products)"
            )

        # Every choice field on this tenant's templates (name + current options),
        # so a bad --field-contains shows the real names to retry with.
        choice_fields = (
            CustomField.objects.filter(
                custom_recap_template__tenant=tenant,
                custom_field_type__name__in=["select", "multiselect"],
            )
            .select_related("custom_field_type", "custom_recap_template")
            .order_by("custom_recap_template_id", "id")
        )
        w(f"choice fields for tenant '{tenant.name}':")
        for f in choice_fields:
            w(
                f"  [{f.id}] {(f.name or '')!r} type={f.custom_field_type.name} "
                f"tmpl={f.custom_recap_template_id} options={f.options!r}"
            )

        matches = [f for f in choice_fields if needle.lower() in (f.name or "").lower()]
        if not matches:
            raise CommandError(
                f"no choice field name contains {needle!r} (see list above)."
            )
        if len(matches) > 1:
            names = "; ".join(f"[{m.id}] {m.name!r}" for m in matches)
            raise CommandError(
                f"{needle!r} matches {len(matches)} choice fields — refusing to "
                f"guess. Narrow --field-contains. Matches: {names}"
            )

        field = matches[0]
        current = list(field.options or [])
        w(
            f"\ntarget field [{field.id}] {field.name!r} "
            f"(type={field.custom_field_type.name})"
        )
        w(f"  current options: {current!r}")
        w(f"  new options    : {new_options!r}  (source: {source})")

        if current and not overwrite:
            w(
                "  SKIP — field already has options; pass --overwrite to replace. "
                "Nothing written."
            )
            return

        if not apply:
            w("\nDRY-RUN — pass --apply to write. Nothing changed.")
            return

        field.options = new_options
        field.save(update_fields=["options"])
        w(f"\nAPPLIED — field [{field.id}] options set to {new_options!r}.")
