"""Set up Brew Dr. Kombucha check-in photo buckets.

Brew Dr's recap template is seeded by ``seed_brew_dr_recap_template``. This
command wires the labelled photo dropzones on the standing check-in recap
step — ``Tenant.checkin_photo_buckets`` plus matching ``FileRecapCategory``
rows — so BAs see Kyle's retail sampling shot list instead of one generic
grid.

Each required bucket carries ``min: 1`` as a BA-facing nudge (the page shows
"0 of 1 suggested"; submit never blocks). "Displays (if applicable)" has no
minimum because the store may have none.

DRY-RUN by default. Run via ``/internal/cron/setup-brew-dr-checkin`` (or the
"Setup Brew Dr check-in" GitHub Action) so it executes against prod.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

CODE_PREFIX = "BD-"
PROGRAM_NAME = "retail sampling"

# Kyle's retail sampling shot list, in render order. Names become both the
# walk-up dropzone labels and the FileRecapCategory rows the recap PDF groups by.
PHOTO_BUCKETS: list[dict] = [
    {"name": "Set Before", "min": 1},
    {"name": "Set After", "min": 1},
    {"name": "Demo Table Before Demo (Far Back)", "min": 1},
    {"name": "Demo Table (Close Up)", "min": 1},
    {"name": "Demo Table Area", "min": 1},
    {"name": "Displays (if applicable)"},
]


class Command(BaseCommand):
    help = (
        "Set up Brew Dr. Kombucha check-in photo buckets on the standing link "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="brew",
            help="tenant name/slug substring (case-insensitive). Default: 'brew'.",
        )
        parser.add_argument(
            "--event-type",
            dest="event_type",
            default=None,
            help=(
                "event type name substring. Default: prefer 'retail', else the "
                "tenant's recap template event type."
            ),
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'BD' -> "
                f"BD-XXXXXX. Blank keeps {CODE_PREFIX!r}."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        creator = self._resolve_creator()
        tenant = self._resolve_tenant(opts["tenant"])
        event_type = self._resolve_event_type(tenant, opts.get("event_type"))

        self.stdout.write("=" * 68)
        self.stdout.write(
            f"Tenant     : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
        )
        self.stdout.write(
            f"Event type : {getattr(event_type, 'name', None)!r} "
            f"(id {getattr(event_type, 'id', None)})"
        )
        existing_code = (getattr(tenant, "checkin_code", "") or "").strip()
        if existing_code:
            self.stdout.write(f"Check-in   : {existing_code!r} (will be left as-is)")
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        if event_type is None:
            raise CommandError(
                f"Tenant {tenant.slug!r} has no event types — run set_tenant_event_types "
                f"or seed_brew_dr_recap_template first."
            )

        self.stdout.write("\nPhoto buckets:")
        for bucket in PHOTO_BUCKETS:
            min_note = f", min={bucket['min']}" if bucket.get("min") else ""
            self.stdout.write(f"    - {bucket['name']!r}{min_note}")

        self._photo_buckets(tenant, creator, apply)
        self._pin_event_type(tenant, event_type, apply)
        self._checkin_code(tenant, apply, opts.get("prefix"))

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

    def _resolve_tenant(self, needle: str):
        from tenants.models import Tenant

        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if len(matches) == 1:
            return matches[0]
        self.stdout.write(self.style.WARNING("Tenants in this database:"))
        for t in Tenant.objects.order_by("id"):
            self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
        if not matches:
            raise CommandError(
                f"No tenant matches {needle!r}. If Brew Dr. isn't in the list above "
                f"it needs onboarding first."
            )
        raise CommandError(
            f"{needle!r} matched {len(matches)} tenants "
            f"({', '.join(repr(t.slug) for t in matches)}) — narrow --tenant."
        )

    def _resolve_event_type(self, tenant, hint: str | None):
        from events.models import EventType
        from recaps.models import CustomRecapTemplate

        qs = EventType.objects.filter(tenant_id=tenant.id).order_by("id")
        if hint:
            match = qs.filter(name__icontains=hint).first()
            if not match:
                raise CommandError(
                    f"No event type on tenant {tenant.slug!r} matches {hint!r}."
                )
            return match
        existing = (
            CustomRecapTemplate.objects.filter(tenant_id=tenant.id)
            .select_related("event_type")
            .first()
        )
        return (
            qs.filter(name__icontains="retail").first()
            or (existing.event_type if existing else None)
            or qs.first()
        )

    def _photo_buckets(self, tenant, creator, apply: bool) -> None:
        from recaps.models import FileRecapCategory

        self.stdout.write("\nFileRecapCategory rows:")
        for spec in PHOTO_BUCKETS:
            name = spec["name"]
            existing = FileRecapCategory.objects.filter(
                tenant_id=tenant.id, name__iexact=name
            ).first()
            if existing:
                self.stdout.write(f"    = {name!r} — [{existing.id}] already present")
            elif apply:
                cat = FileRecapCategory.objects.create(
                    name=name, tenant_id=tenant.id, created_by=creator
                )
                self.stdout.write(f"    + {name!r} — created [{cat.id}]")
            else:
                self.stdout.write(f"    + {name!r} — would be created")

        current = getattr(tenant, "checkin_photo_buckets", None)
        if current == PHOTO_BUCKETS:
            self.stdout.write("  checkin_photo_buckets already set — left as-is.")
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING("  DRY-RUN — would set checkin_photo_buckets")
            )
            return
        tenant.checkin_photo_buckets = PHOTO_BUCKETS
        tenant.save(update_fields=["checkin_photo_buckets"])
        self.stdout.write(self.style.SUCCESS("  checkin_photo_buckets set."))

    def _pin_event_type(self, tenant, event_type, apply: bool) -> None:
        current = getattr(tenant, "checkin_event_type_id", None)
        already = current == event_type.id
        if already:
            self.stdout.write(
                f"\ncheckin_event_type already pinned to {event_type.name!r}."
            )
        elif not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDRY-RUN — would pin checkin_event_type="
                    f"{event_type.name!r} (id {event_type.id})"
                )
            )
        else:
            tenant.checkin_event_type = event_type
            tenant.save(update_fields=["checkin_event_type"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nPinned checkin_event_type={event_type.name!r} "
                    f"(id {event_type.id})"
                )
            )
        if apply:
            tenant.checkin_event_types.set([event_type])

    def _checkin_code(self, tenant, apply: bool, prefix: str = "") -> None:
        from tenants.models import Tenant

        raw = (prefix or "").strip().upper().rstrip("-")
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if raw and not 1 <= len(cleaned) <= 4:
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. BD.")
        code_prefix = f"{cleaned}-" if cleaned else CODE_PREFIX

        ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "")
            or "https://client.igniteproductions.co"
        ).rstrip("/")

        self.stdout.write("\n" + "=" * 68)
        existing = (getattr(tenant, "checkin_code", "") or "").strip()
        if existing:
            self.stdout.write(
                f"Check-in code already set: {existing}\n"
                f"  Link: {base}/checkin/{existing}\n"
                "  (left as-is — rotating it would break every copy already shared)"
            )
            return

        code = None
        for _ in range(12):
            candidate = code_prefix + "".join(
                secrets.choice(ALPHABET) for _ in range(6)
            )
            if not Tenant.objects.filter(checkin_code__iexact=candidate).exists():
                code = candidate
                break
        if code is None:
            raise CommandError("Couldn't mint a unique check-in code — try again.")

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would set checkin_code={code}\n"
                    f"  Link would be: {base}/checkin/{code}"
                )
            )
            return

        tenant.checkin_code = code
        tenant.save(update_fields=["checkin_code"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Check-in code set: {code}\n  Link: {base}/checkin/{code}"
            )
        )
