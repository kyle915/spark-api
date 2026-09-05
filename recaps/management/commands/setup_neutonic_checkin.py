"""Wire Neutonic's EXISTING standing check-in to two walk-up programs.

Keeps Neutonic's existing ``checkin_code`` and Retail Sampling photo buckets.
Ensures the Event Activation event type + LD-style activation photo buckets,
and sets the walk-up picker to **Retail Sampling** + **Event Activation** only.

Unused Event / On-Premise Sampling ``EventType`` rows (and their templates)
may remain in the DB — they are simply dropped from ``checkin_event_types``
so they do not appear on ``NEU-N85ZE5``.

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
RETAIL_LABEL = "Retail Sampling"
EVENT_LABEL = "Event Activation"

# Exact set shown on the Neutonic walk-up program picker (order = display).
WALKUP_PROGRAMS = (RETAIL_LABEL, EVENT_LABEL)

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

# Prefer these as the pinned default when Neutonic has no (or an invalid) pin.
PIN_PREFERENCE = WALKUP_PROGRAMS


def _norm(name: str | None) -> str:
    """Fold a category label for comparison — case and punctuation dropped."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


class Command(BaseCommand):
    help = (
        "Set Neutonic's standing check-in to Retail Sampling + Event "
        "Activation only (keeps existing code; dry-run default)."
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
            key = pin.name if pin is not None else RETAIL_LABEL
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

    def _resolve_selectable(
        self, tenant, retail, activation, apply: bool
    ) -> list:
        """Walk-up picker = Retail Sampling + Event Activation only.

        Does not delete unused Event / On-Premise Sampling EventType rows —
        only clears them from the link's selectable M2M.
        """
        from events.models import EventType

        previously = list(
            tenant.checkin_event_types.filter(tenant_id=tenant.id).order_by("id")
        )
        wanted: list = []
        for label, etype in (
            (RETAIL_LABEL, retail),
            (EVENT_LABEL, activation),
        ):
            if etype is not None and getattr(etype, "id", None):
                wanted.append(etype)
            else:
                # Dry-run stand-in so logs still show the intended picker.
                wanted.append(EventType(name=label, tenant=tenant, id=0))

        wanted_ids = {et.id for et in wanted if et.id}
        dropped = [et for et in previously if et.id not in wanted_ids]

        self.stdout.write("\nSelectable on the link (Retail + Event Activation only):")
        for et in wanted:
            tag = "  ← walk-up" if et.name in WALKUP_PROGRAMS else ""
            self.stdout.write(f"  [{et.id}] {et.name!r}{tag}")
        if dropped:
            self.stdout.write("Dropped from walk-up picker (rows kept in DB):")
            for et in dropped:
                self.stdout.write(f"  - [{et.id}] {et.name!r}")

        if apply:
            real = [et for et in wanted if et.id]
            tenant.checkin_event_types.set(real)
            return real
        return wanted

    def _ensure_pin(self, tenant, selectable: list, apply: bool) -> None:
        """Keep pin if it is still selectable; otherwise prefer Retail Sampling."""
        pin = getattr(tenant, "checkin_event_type", None)
        selectable_ids = {et.id for et in selectable if et.id}
        if pin is not None and pin.id in selectable_ids:
            self.stdout.write(f"\nPinned default : [{pin.id}] {pin.name!r} (unchanged)")
            return

        by_name = {et.name: et for et in selectable if et.id}
        pick = None
        for name in PIN_PREFERENCE:
            if name in by_name:
                pick = by_name[name]
                break
        if pick is None and selectable:
            pick = next((et for et in selectable if et.id), None)
        if pick is None:
            self.stdout.write(
                self.style.WARNING("\nNo event type available to pin as default.")
            )
            return

        reason = (
            f"(was {pin.name!r} — not on walk-up set)"
            if pin is not None
            else "(none)"
        )
        self.stdout.write(
            f"\nPinned default : {reason} → would pin [{pick.id}] {pick.name!r}"
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
                retail = self._ensure_event_type(
                    tenant, RETAIL_LABEL, creator, apply=True
                )
                activation = self._ensure_event_type(
                    tenant, EVENT_LABEL, creator, apply=True
                )
                self._ensure_activation_categories(tenant, creator, apply=True)
                self._merge_photo_buckets(tenant, apply=True)
                selectable = self._resolve_selectable(
                    tenant, retail, activation, apply=True
                )
                self._ensure_pin(tenant, selectable, apply=True)

                if forced_code and not existing_code:
                    tenant.checkin_code = forced_code
                    tenant.save(update_fields=["checkin_code"])
                elif not existing_code:
                    tenant.checkin_code = self._mint_code(opts.get("prefix") or "")
                    tenant.save(update_fields=["checkin_code"])
        else:
            retail = self._ensure_event_type(
                tenant, RETAIL_LABEL, creator, apply=False
            )
            activation = self._ensure_event_type(
                tenant, EVENT_LABEL, creator, apply=False
            )
            if retail is None:
                from events.models import EventType

                retail = EventType(name=RETAIL_LABEL, tenant=tenant, id=0)
            if activation is None:
                from events.models import EventType

                activation = EventType(name=EVENT_LABEL, tenant=tenant, id=0)
            self._ensure_activation_categories(tenant, creator, apply=False)
            self._merge_photo_buckets(tenant, apply=False)
            selectable = self._resolve_selectable(
                tenant, retail, activation, apply=False
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
                    "APPLIED — Neutonic walk-up offers Retail Sampling + "
                    "Event Activation only (code unchanged)."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "DRY-RUN — would set walk-up programs to Retail Sampling + "
                    "Event Activation only (Event / On-Premise Sampling dropped "
                    "from picker, not deleted). Re-run with --apply to write."
                )
            )
            if existing_code:
                self.stdout.write(f"Existing link : {base}/checkin/{existing_code}")
