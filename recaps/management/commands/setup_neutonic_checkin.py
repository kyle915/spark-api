"""Add Event Activation to Neutonic's EXISTING standing check-in link.

Additive only: keeps Neutonic's existing ``checkin_code``, pinned program,
selectable event types, and photo-bucket keys. Creates the Event Activation
event type + LD-style activation photo buckets, and adds that program to the
walk-up picker alongside whatever is already there (Retail Sampling, Event,
On-Premise Sampling, …).

Recap form is seeded by ``seed_neutonic_recap_template`` — this command
creates NEITHER the template nor a new code when one already exists.

DRY-RUN by default. Run via ``/internal/cron/setup-neutonic-checkin`` (or the
"Setup Neutonic check-in" GitHub Action) so it executes against prod.
"""

from __future__ import annotations

import re
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

CODE_PREFIX = "NEU-"
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TENANT_SLUG = "neutonic"
EVENT_LABEL = "Event Activation"

CONSUMER_SAMPLING: dict = {
    "name": "Consumer Sampling Pictures",
    "helper": "please try to upload 8+",
    "min": 8,
}

# LD / MAB Event Activation dropzones.
ACTIVATION_BUCKETS: list[dict] = [
    {"name": "Activation Set Up"},
    CONSUMER_SAMPLING,
    {"name": "Expense Receipts (Parking)"},
]

SENTINEL_CATEGORY_NAMES = ("Sampling photos", "Receipts")

# Prefer these as the pinned default when Neutonic has no pin yet.
PIN_PREFERENCE = (
    "Retail Sampling",
    "Event",
    "On-Premise Sampling",
    EVENT_LABEL,
)


def _norm(name: str | None) -> str:
    """Fold a category label for comparison — case and punctuation dropped."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


class Command(BaseCommand):
    help = (
        "Add Event Activation to Neutonic's standing check-in link "
        "(keeps existing code + programs; dry-run default)."
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
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code only, e.g. 'NEU' -> "
                f"NEU-XXXXXX. Blank keeps {CODE_PREFIX!r}. Never rotates an "
                "existing code."
            ),
        )
        parser.add_argument(
            "--code",
            default="",
            help="force a specific check-in code (only when tenant has none).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing).",
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

    def _resolve_tenant(self, needle: str):
        from tenants.models import Tenant

        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle)
                | Q(slug__icontains=needle)
                | Q(slug__iexact=TENANT_SLUG)
            )
            .distinct()
            .order_by("id")
        )
        if len(matches) > 1:
            exact = [
                t for t in matches if (t.slug or "").lower() == TENANT_SLUG
            ]
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

    def _ensure_event_type(self, tenant, label: str, creator, apply: bool):
        from events.models import EventType

        existing = EventType.objects.filter(
            tenant_id=tenant.id, name__iexact=label
        ).first()
        if existing:
            return existing
        if not apply:
            self.stdout.write(f"  would create event type {label!r}")
            return None
        et = EventType.objects.create(name=label, tenant=tenant, created_by=creator)
        self.stdout.write(f"  + event type {label!r} [{et.id}]")
        return et

    def _mint_code(self, prefix: str) -> str:
        from tenants.models import Tenant

        raw = (prefix or "").strip().upper().rstrip("-")
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if raw and not 1 <= len(cleaned) <= 4:
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. NEU.")
        code_prefix = f"{cleaned}-" if cleaned else CODE_PREFIX
        for _ in range(50):
            candidate = code_prefix + "".join(
                secrets.choice(ALPHABET) for _ in range(6)
            )
            if not Tenant.objects.filter(checkin_code=candidate).exists():
                return candidate
        raise CommandError("Could not mint an unused check-in code.")

    def _ensure_activation_categories(self, tenant, creator, apply: bool) -> None:
        from recaps.models import FileRecapCategory

        existing = list(
            FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id")
        )
        by_norm: dict[str, object] = {}
        for cat in existing:
            key = _norm(cat.name)
            if key not in by_norm:
                by_norm[key] = cat

        self.stdout.write("")
        self.stdout.write(
            f"Categories : {len(existing)} on this tenant today"
            + ("" if existing else "  (none — activation buckets will be created)")
        )
        for cat in existing:
            protected = (
                " [sentinel — never renamed]"
                if any(_norm(cat.name) == _norm(n) for n in SENTINEL_CATEGORY_NAMES)
                else ""
            )
            self.stdout.write(f"    [{cat.id}] {cat.name!r}{protected}")

        self.stdout.write(f"\nEvent Activation buckets ({len(ACTIVATION_BUCKETS)}):")
        for spec in ACTIVATION_BUCKETS:
            name = spec["name"]
            key = _norm(name)
            match = by_norm.get(key)
            hint = (
                f" (min {spec['min']}, {spec.get('helper', '')!r})"
                if spec.get("min")
                else ""
            )
            if match is None:
                self.stdout.write(f"    + {name!r} — will be CREATED{hint}")
                if apply:
                    cat = FileRecapCategory.objects.create(
                        name=name, tenant_id=tenant.id, created_by=creator
                    )
                    by_norm[key] = cat
                    self.stdout.write(f"      created [{cat.id}]")
            elif match.name == name:
                self.stdout.write(
                    f"    = {name!r} — [{match.id}] already correct{hint}"
                )
            else:
                self.stdout.write(
                    f"    ~ {name!r} — reusing [{match.id}] {match.name!r}"
                    f"{hint}"
                )
                if apply and not any(
                    _norm(match.name) == _norm(n) for n in SENTINEL_CATEGORY_NAMES
                ):
                    old = match.name
                    match.name = name
                    match.save(update_fields=["name"])
                    self.stdout.write(f"      relabelled {old!r} → {name!r}")

    def _merge_photo_buckets(self, tenant, apply: bool) -> dict:
        """Preserve existing keyed buckets; set/refresh Event Activation only."""
        current = getattr(tenant, "checkin_photo_buckets", None)
        merged: dict = {}
        if isinstance(current, dict):
            merged = {k: list(v) for k, v in current.items()}
        elif isinstance(current, list) and current:
            # Legacy flat list — keep under a generic key if any pin exists.
            pin = getattr(tenant, "checkin_event_type", None)
            key = pin.name if pin is not None else "Retail Sampling"
            merged[key] = list(current)

        entries = []
        for spec in ACTIVATION_BUCKETS:
            entry = {"name": spec["name"]}
            if spec.get("min"):
                entry["min"] = spec["min"]
            if spec.get("helper"):
                entry["helper"] = spec["helper"]
            entries.append(entry)
        merged[EVENT_LABEL] = entries

        self.stdout.write("\nPhoto bucket keys after merge:")
        for key, buckets in merged.items():
            marker = "  ← Event Activation" if key == EVENT_LABEL else ""
            self.stdout.write(
                f"  {key!r}: "
                + " | ".join(b.get("name", "?") for b in buckets)
                + marker
            )

        if not apply:
            return merged
        tenant.checkin_photo_buckets = merged
        tenant.save(update_fields=["checkin_photo_buckets"])
        return merged

    def _resolve_selectable(self, tenant, activation, apply: bool) -> list:
        """Existing selectable programs + Event Activation (never drop any)."""
        from events.models import EventType

        current = list(
            tenant.checkin_event_types.filter(tenant_id=tenant.id).order_by("id")
        )
        if not current:
            # Nothing pinned on the link yet — offer Neutonic's existing
            # program types so Retail / Event / On-Premise stay available.
            named = list(
                EventType.objects.filter(tenant_id=tenant.id)
                .filter(
                    name__in=[
                        "Retail Sampling",
                        "Event",
                        "On-Premise Sampling",
                        EVENT_LABEL,
                    ]
                )
                .order_by("id")
            )
            current = named

        by_id = {et.id: et for et in current}
        if activation is not None and activation.id and activation.id not in by_id:
            by_id[activation.id] = activation
        selectable = list(by_id.values())

        self.stdout.write("\nSelectable on the link:")
        for et in selectable:
            tag = "  ← NEW" if et.name == EVENT_LABEL else ""
            self.stdout.write(f"  [{et.id}] {et.name!r}{tag}")

        if apply:
            tenant.checkin_event_types.set(selectable)
        return selectable

    def _ensure_pin(self, tenant, selectable: list, apply: bool) -> None:
        """Keep existing pin; if unset, prefer Retail Sampling among selectable."""
        pin = getattr(tenant, "checkin_event_type", None)
        if pin is not None:
            self.stdout.write(f"\nPinned default : [{pin.id}] {pin.name!r} (unchanged)")
            return

        by_name = {et.name: et for et in selectable}
        pick = None
        for name in PIN_PREFERENCE:
            if name in by_name:
                pick = by_name[name]
                break
        if pick is None and selectable:
            pick = selectable[0]
        if pick is None:
            self.stdout.write(
                self.style.WARNING("\nNo event type available to pin as default.")
            )
            return

        self.stdout.write(
            f"\nPinned default : (none) → would pin [{pick.id}] {pick.name!r}"
            if not apply
            else f"\nPinned default : [{pick.id}] {pick.name!r}"
        )
        if apply:
            tenant.checkin_event_type = pick
            tenant.save(update_fields=["checkin_event_type"])

    def handle(self, *args, **opts):
        apply = opts["apply"]
        creator = self._resolve_creator()
        tenant = self._resolve_tenant(opts["tenant"])
        forced_code = (opts.get("code") or "").strip().upper()

        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "")
            or "https://client.igniteproductions.co"
        ).rstrip("/")

        self.stdout.write("=" * 68)
        self.stdout.write(
            f"Tenant     : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
        )
        existing_code = (getattr(tenant, "checkin_code", "") or "").strip()
        if existing_code:
            self.stdout.write(
                f"Check-in   : {existing_code!r} (will be left as-is)"
            )
            self.stdout.write(f"Link       : {base}/checkin/{existing_code}")
        else:
            self.stdout.write("Check-in   : (none yet — will mint only under --apply)")
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        if apply:
            with transaction.atomic():
                activation = self._ensure_event_type(
                    tenant, EVENT_LABEL, creator, apply=True
                )
                self._ensure_activation_categories(tenant, creator, apply=True)
                self._merge_photo_buckets(tenant, apply=True)
                selectable = self._resolve_selectable(
                    tenant, activation, apply=True
                )
                self._ensure_pin(tenant, selectable, apply=True)

                if forced_code and not existing_code:
                    tenant.checkin_code = forced_code
                    tenant.save(update_fields=["checkin_code"])
                elif not existing_code:
                    tenant.checkin_code = self._mint_code(opts.get("prefix") or "")
                    tenant.save(update_fields=["checkin_code"])
        else:
            activation = self._ensure_event_type(
                tenant, EVENT_LABEL, creator, apply=False
            )
            if activation is None:
                from events.models import EventType

                activation = EventType(name=EVENT_LABEL, tenant=tenant, id=0)
            self._ensure_activation_categories(tenant, creator, apply=False)
            self._merge_photo_buckets(tenant, apply=False)
            selectable = self._resolve_selectable(
                tenant, activation if activation.id else None, apply=False
            )
            self._ensure_pin(tenant, selectable, apply=False)

        tenant.refresh_from_db()
        code = (tenant.checkin_code or "").strip() or "(none — dry-run)"
        self.stdout.write("")
        if apply:
            self.stdout.write(self.style.SUCCESS(f"Check-in code : {code}"))
            self.stdout.write(f"Link          : {base}/checkin/{code}")
            self.stdout.write(
                self.style.SUCCESS(
                    "APPLIED — Event Activation added to Neutonic walk-up "
                    "(existing programs preserved)."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "DRY-RUN — would add Event Activation + activation photo "
                    "buckets without removing existing programs or rotating "
                    "the check-in code. Re-run with --apply to write."
                )
            )
            if existing_code:
                self.stdout.write(f"Existing link : {base}/checkin/{existing_code}")
