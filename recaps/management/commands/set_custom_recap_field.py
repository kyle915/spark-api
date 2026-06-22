"""Correct ONE custom-recap field value — a targeted, reversible data fix.

Built for the kind of single-field data-entry error that can dominate a
whole rollup (e.g. Stone House Bread recap 95's "Consumers Sampled" entered
as 1960 instead of ~30). Rather than hand-edit prod, this command makes the
change auditable and safe:

  * Targets exactly ONE recap + ONE field (matched by name substring). If the
    substring matches zero or more-than-one field on the recap, it refuses to
    write — no guessing which field.
  * DRY-RUN by default. Prints the recap, the field, the current value, and
    the would-be new value. Pass --apply to actually write.
  * --expect-current GUARD: when given, the write only happens if the field's
    current value still equals it. Makes the change idempotent (a re-run is a
    no-op) and prevents clobbering a value that changed since we looked.
  * REVERSIBLE: the output prints the old value + the CustomFieldValue id, so
    the exact same command with --value <old> restores it.

Run via the ``/internal/cron/set-custom-recap-field`` endpoint (or the
``Set custom recap field`` GitHub Action) so it executes against prod.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from recaps.models import CustomFieldValue, CustomRecap


def _resolve_recap(ident: str) -> CustomRecap:
    """Find a CustomRecap by numeric id or uuid."""
    if ident.isdigit():
        r = CustomRecap.objects.filter(id=int(ident)).first()
        if r:
            return r
    # A malformed uuid string makes the ORM raise ValidationError/ValueError.
    try:
        r = CustomRecap.objects.filter(uuid=ident).first()
    except (ValueError, ValidationError):
        r = None
    if r:
        return r
    raise CommandError(f"No custom recap matches {ident!r} (tried id and uuid).")


class Command(BaseCommand):
    help = "Correct ONE custom-recap field value (dry-run by default; --apply to write)."

    def add_arguments(self, parser):
        parser.add_argument("--recap", required=True, help="CustomRecap id or uuid")
        parser.add_argument(
            "--field-contains",
            required=True,
            dest="field_contains",
            help="case-insensitive substring of the field NAME to target",
        )
        parser.add_argument("--value", required=True, help="new value to set")
        parser.add_argument(
            "--expect-current",
            dest="expect_current",
            default=None,
            help="only write if the field's current value equals this (safety guard)",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing)",
        )

    def handle(self, *args, **opts):
        recap = _resolve_recap(opts["recap"])
        needle = opts["field_contains"]
        new_value = opts["value"]
        expect = opts["expect_current"]
        apply = opts["apply"]

        matches = list(
            CustomFieldValue.objects.filter(
                custom_recap_id=recap.id,
                custom_field__name__icontains=needle,
            ).select_related("custom_field")
        )

        tenant_name = getattr(getattr(recap, "tenant", None), "name", "?")
        self.stdout.write(
            f"Recap {recap.id} (uuid {recap.uuid}) · {recap.name or '—'} · tenant {tenant_name}"
        )
        self.stdout.write("=" * 64)

        if not matches:
            raise CommandError(
                f"No field on recap {recap.id} has a name containing {needle!r}."
            )
        if len(matches) > 1:
            names = "; ".join(f"[{m.id}] {m.custom_field.name!r}={m.value!r}" for m in matches)
            raise CommandError(
                f"{needle!r} matches {len(matches)} fields on recap {recap.id} — "
                f"refusing to guess. Matches: {names}. Narrow --field-contains."
            )

        cfv = matches[0]
        field_name = cfv.custom_field.name
        current = cfv.value
        self.stdout.write(f"Field        : {field_name!r}  (CustomFieldValue id {cfv.id})")
        self.stdout.write(f"Current value: {current!r}")
        self.stdout.write(f"New value    : {new_value!r}")

        if expect is not None and str(current) != str(expect):
            raise CommandError(
                f"Current value {current!r} != --expect-current {expect!r}; "
                f"refusing to write (value changed or already corrected)."
            )

        if str(current) == str(new_value):
            self.stdout.write(self.style.SUCCESS("Already set to the target value — no-op."))
            return

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would set {field_name!r} {current!r} -> {new_value!r}. "
                    f"Re-run with --apply to write. To revert later: --value {current!r}."
                )
            )
            return

        with transaction.atomic():
            cfv.value = new_value
            cfv.save(update_fields=["value", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"APPLIED — recap {recap.id} field {field_name!r} (CFV {cfv.id}): "
                f"{current!r} -> {new_value!r}. To revert: --value {current!r} "
                f"--expect-current {new_value!r}."
            )
        )
