"""Make Brew Dr. Kombucha's ONE standing check-in link serve BOTH programs.

Mirrors ``setup_ld_retail_checkin``: Retail Sampling + Event Activation on the
same ``BD-`` URL. Recap forms are seeded by ``seed_brew_dr_recap_template`` —
this command creates NEITHER. What's missing is only:

1. a standing ``BD-`` check-in code on the tenant (keep BD-AQRACD if set),
2. both event types made SELECTABLE on that one link, with Retail Sampling
   pinned as the fallback when a request names no program,
3. labelled PHOTO BUCKETS per program (``Tenant.checkin_photo_buckets`` keyed
   by event type name + matching ``FileRecapCategory`` rows).

Retail buckets stay Kyle's Brew Dr shot list (already live). Event Activation
buckets mirror Liquid Death's activation dropzones.

DRY-RUN by default. Run via ``/internal/cron/setup-brew-dr-checkin`` (or the
"Setup Brew Dr check-in" GitHub Action) so it executes against prod.
"""

from __future__ import annotations

import re
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

CODE_PREFIX = "BD-"
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Kyle's retail sampling shot list (already live on BD-AQRACD).
RETAIL_BUCKETS: list[dict] = [
    {"name": "Set Before", "min": 1},
    {"name": "Set After", "min": 1},
    {"name": "Demo Table Before Demo (Far Back)", "min": 1},
    {"name": "Demo Table (Close Up)", "min": 1},
    {"name": "Demo Table Area", "min": 1},
    {"name": "Displays (if applicable)"},
]

# LD Event Activation dropzones, brand-agnostic shot names.
CONSUMER_SAMPLING: dict = {
    "name": "Consumer Sampling Pictures",
    "helper": "please try to upload 8+",
    "min": 8,
}

ACTIVATION_BUCKETS: list[dict] = [
    {"name": "Activation Set Up"},
    CONSUMER_SAMPLING,
    {"name": "Expense Receipts (Parking)"},
]

# First entry = pinned default when the BA / request names no program.
PROGRAMS: list[dict] = [
    {
        "event_type": "retail sampling",
        "label": "Retail Sampling",
        "photos": RETAIL_BUCKETS,
    },
    {
        "event_type": "event activation",
        "label": "Event Activation",
        "photos": ACTIVATION_BUCKETS,
    },
]

# Flat list kept for older tests / imports that expect PHOTO_BUCKETS.
PHOTO_BUCKETS = RETAIL_BUCKETS

SENTINEL_CATEGORY_NAMES = ("Sampling photos", "Receipts")


def _norm(name: str | None) -> str:
    """Fold a category label for comparison — case and punctuation dropped."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


class Command(BaseCommand):
    help = (
        "Make Brew Dr. Kombucha's standing check-in link serve Retail Sampling "
        "and Event Activation (photo buckets + selectable types; dry-run default)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="brew",
            help="tenant name/slug substring (case-insensitive). Default: 'brew'.",
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
            "--code",
            default="",
            help="force a specific check-in code (to keep an already-shared link).",
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
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. BD.")
        code_prefix = f"{cleaned}-" if cleaned else CODE_PREFIX
        for _ in range(50):
            candidate = code_prefix + "".join(
                secrets.choice(ALPHABET) for _ in range(6)
            )
            if not Tenant.objects.filter(checkin_code=candidate).exists():
                return candidate
        raise CommandError("Could not mint an unused check-in code.")

    def _plan_photo_buckets(self, tenant, programs: list[dict]) -> dict:
        from recaps.models import FileRecapCategory

        existing = list(
            FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id")
        )
        self.stdout.write("")
        self.stdout.write(
            f"Categories : {len(existing)} on this tenant today"
            + ("" if existing else "  (none — every bucket will be created)")
        )
        for cat in existing:
            protected = (
                " [sentinel target — never renamed]"
                if any(_norm(cat.name) == _norm(n) for n in SENTINEL_CATEGORY_NAMES)
                else ""
            )
            self.stdout.write(f"    [{cat.id}] {cat.name!r}{protected}")

        by_norm: dict[str, object] = {}
        for cat in existing:
            key = _norm(cat.name)
            if key not in by_norm:
                by_norm[key] = cat

        self.stdout.write("")
        plan: dict = {}
        for program in programs:
            self.stdout.write(
                f"Buckets    : {program['type'].name} — {len(program['photos'])} "
                "dropzone(s)"
            )
            for spec in program["photos"]:
                name = spec["name"]
                key = _norm(name)
                hint = (
                    f" (min {spec['min']}, {spec.get('helper', '')!r})"
                    if spec.get("min")
                    else ""
                )
                if key in plan:
                    self.stdout.write(
                        f"    ⇄ {name!r} — shared with an earlier program, "
                        f"one row{hint}"
                    )
                    continue
                match = by_norm.get(key)
                plan[key] = {**spec, "category": match}
                if match is None:
                    self.stdout.write(f"    + {name!r} — will be CREATED{hint}")
                elif match.name == name:
                    self.stdout.write(
                        f"    = {name!r} — [{match.id}] already correct{hint}"
                    )
                else:
                    self.stdout.write(
                        f"    ~ {name!r} — reusing [{match.id}] {match.name!r}, "
                        f"relabelling in place{hint}"
                    )
        return plan

    def _ensure_photo_buckets(
        self, tenant, programs: list[dict], bucket_plan: dict, creator, apply: bool
    ) -> dict:
        """Create/relabel categories, return the keyed checkin_photo_buckets dict."""
        from recaps.models import FileRecapCategory

        for key, spec in bucket_plan.items():
            cat = spec.get("category")
            name = spec["name"]
            if cat is None:
                if apply:
                    cat = FileRecapCategory.objects.create(
                        name=name, tenant_id=tenant.id, created_by=creator
                    )
                    self.stdout.write(f"    + {name!r} — created [{cat.id}]")
                    spec["category"] = cat
            elif cat.name != name and apply:
                if any(_norm(cat.name) == _norm(n) for n in SENTINEL_CATEGORY_NAMES):
                    self.stdout.write(
                        self.style.WARNING(
                            f"    ! refusing to rename sentinel {cat.name!r}"
                        )
                    )
                else:
                    old = cat.name
                    cat.name = name
                    cat.save(update_fields=["name"])
                    self.stdout.write(
                        f"    ~ relabelled [{cat.id}] {old!r} → {name!r}"
                    )

        config: dict[str, list] = {}
        for program in programs:
            etype = program["type"]
            entries = []
            for spec in program["photos"]:
                entry = {"name": spec["name"]}
                if spec.get("min"):
                    entry["min"] = spec["min"]
                if spec.get("helper"):
                    entry["helper"] = spec["helper"]
                entries.append(entry)
            config[etype.name] = entries
        return config

    def handle(self, *args, **opts):
        apply = opts["apply"]
        creator = self._resolve_creator()
        tenant = self._resolve_tenant(opts["tenant"])
        forced_code = (opts.get("code") or "").strip().upper()

        self.stdout.write("=" * 68)
        self.stdout.write(
            f"Tenant     : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
        )
        existing_code = (getattr(tenant, "checkin_code", "") or "").strip()
        if existing_code:
            self.stdout.write(f"Check-in   : {existing_code!r} (will be left as-is)")
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        plan: list[dict] = []
        for spec in PROGRAMS:
            etype = self._ensure_event_type(
                tenant, spec["label"], creator, apply
            )
            if etype is None and apply:
                raise CommandError(
                    f"Could not ensure event type {spec['label']!r}."
                )
            # Dry-run may leave etype None — synthesize a stand-in for reporting.
            if etype is None:
                from events.models import EventType

                etype = EventType(name=spec["label"], tenant=tenant, id=0)
            plan.append({"type": etype, "photos": spec["photos"]})

        self.stdout.write("\nSelectable on the link (in this order):")
        for i, entry in enumerate(plan):
            etype = entry["type"]
            pin = "   ← pinned default" if i == 0 else ""
            self.stdout.write(f"  [{etype.id}] {etype.name!r}{pin}")

        bucket_plan = self._plan_photo_buckets(tenant, plan)
        config = self._ensure_photo_buckets(
            tenant, plan, bucket_plan, creator, apply=False
        )

        if not apply:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "DRY-RUN — would set checkin_event_types + keyed "
                    "checkin_photo_buckets. Re-run with --apply to write."
                )
            )
            return

        with transaction.atomic():
            config = self._ensure_photo_buckets(
                tenant, plan, bucket_plan, creator, apply=True
            )
            real_types = [e["type"] for e in plan if e["type"].id]
            tenant.checkin_event_type = real_types[0]
            tenant.checkin_photo_buckets = config
            update_fields = ["checkin_event_type", "checkin_photo_buckets"]
            if forced_code:
                tenant.checkin_code = forced_code
                update_fields.append("checkin_code")
            elif not (tenant.checkin_code or "").strip():
                tenant.checkin_code = self._mint_code(opts.get("prefix") or "")
                update_fields.append("checkin_code")
            tenant.save(update_fields=update_fields)
            tenant.checkin_event_types.set(real_types)

        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "")
            or "https://client.igniteproductions.co"
        ).rstrip("/")
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Check-in code : {tenant.checkin_code}")
        )
        self.stdout.write(f"Link          : {base}/checkin/{tenant.checkin_code}")
        self.stdout.write(
            "  selectable = "
            + ", ".join(f"[{t.id}] {t.name}" for t in real_types)
        )
        for key, entries in config.items():
            self.stdout.write(
                f"Photo buckets : {key} — "
                + " | ".join(b["name"] for b in entries)
            )
