"""Turn on the STANDING check-in link for any tenant.

This is the generic form of what `setup_ld_retail_checkin`,
`setup_total_wireless_checkin` and `setup_feel_free_checkin` each do for one
brand. Those three stay: they carry brand-specific payloads (LD's SKU list, Feel
Free's market list) and their own live cron endpoints. What was NOT reusable was
the wiring every brand needs identically — mint a code, pin the program, back the
photo dropzones with real category rows — so a fourth brand meant a fourth copy
of the same 200 lines, and the fifth copy is where they start to drift.

WHAT A STANDING LINK IS
    `/checkin/<code>` normally carries an EVENT's walkup_code, so it works for
    exactly one pre-created activation. `Tenant.checkin_code` is the tenant-wide
    twin: one durable URL an admin shares with every BA. The BA supplies the
    store and date, and Spark finds-or-creates the event — keyed on (tenant,
    normalized address, date), which is what lets several BAs at one store on
    one day land on ONE event with their own bookings, hours and recaps.

THE PIN IS NOT OPTIONAL
    Without `checkin_event_type` the walk-in path falls back to the tenant's
    LOWEST-ID event type. That is arbitrary, and it decides WHICH RECAP FORM the
    BA is handed — `resolve_template_for_event` picks the template by
    `event_type_id`. A brand with more than one event type can hand a retail BA
    the wrong form and nobody notices, because the recap still submits fine. So
    this command refuses to write a link it can't pin.

ONE LINK, NOT ONE PER PROGRAM
    `checkin_code` is a single column. Minting a second link for a second
    program silently REPOINTS the first, and every BA holding the old URL lands
    somewhere else. A brand running two programs passes --event-type twice
    instead: the program becomes a question on the one link, the answer is
    stamped on the event, and the form follows from that.

PHOTO BUCKETS
    Two writes that have to agree: a `FileRecapCategory` per bucket (the rows
    the recap PDF groups by) and `Tenant.checkin_photo_buckets` (what the page
    renders). Bucket names are matched against existing categories
    case/punctuation-insensitively before anything is created, so a re-run never
    leaves a tenant with both "Table setup" and "Table Set Up" — two
    near-identical buckets in the PDF is exactly the failure this avoids.

    Categories that back a positional upload sentinel are matched and then left
    alone; see SENTINEL_CATEGORY_NAMES.

Dry-run by default; --apply writes. Idempotent throughout — re-running with the
same arguments writes nothing and says so.

Usage::

    python manage.py setup_tenant_checkin --tenant-id 18
    python manage.py setup_tenant_checkin --tenant-id 18 \
        --code-prefix RB --event-type "Retail Sampling" \
        --photo-buckets '[{"name": "Table Set Up"},
                          {"name": "Consumer Sampling Pictures",
                           "helper": "please try to upload 8+", "min": 8},
                          {"name": "Product Receipt"}]' --apply
"""

from __future__ import annotations

import json
import re
import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from tenants.models import Tenant

# No 0/O/1/I/L — the code gets read aloud and retyped off a text message.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_BODY_LENGTH = 6

# A bucket must never absorb one of these by renaming it: the positional upload
# sentinels in recaps.mutations resolve by NAME, so renaming "Sampling photos"
# makes the fallback path create a fresh one beside it and splits the brand's
# photos across two rows.
SENTINEL_CATEGORY_NAMES = ("Sampling photos", "Receipts")

# Used when --photo-buckets is omitted and the tenant has no categories to
# derive from. Deliberately the retail shot list, because that is what every
# brand onboarded through this command has run so far; a brand doing something
# else passes its own list.
DEFAULT_BUCKETS: list[dict] = [
    {"name": "Table Set Up"},
    {"name": "Product Display"},
    {
        "name": "Consumer Sampling Pictures",
        "helper": "please try to upload 8+",
        "min": 8,
    },
    {"name": "Product Receipt"},
]


def _norm(name: str | None) -> str:
    """Fold a label for comparison — case and punctuation dropped, so
    "Table Set Up", "Table setup" and "table-setup" are recognised as one."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _derive_prefix(tenant: Tenant) -> str:
    """Initials of the tenant name, e.g. "Resort Beverage" -> "RB".

    A readable prefix is the whole point of a code a BA reads off a text
    message; a single-word brand keeps its first two letters rather than one.
    """
    words = re.findall(r"[A-Za-z0-9]+", tenant.name or "")
    if not words:
        return "SP"
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(w[0] for w in words[:3]).upper()


class Command(BaseCommand):
    help = (
        "Turn on the standing check-in link for a tenant: mint the code, pin "
        "the program(s), and back the photo dropzones with real category rows "
        "(dry-run default)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            dest="tenant_id",
            type=int,
            default=None,
            help="Tenant to configure. Preferred over --tenant; ids are exact.",
        )
        parser.add_argument(
            "--tenant",
            default="",
            help="Name/slug needle, if you don't have the id. Must match one.",
        )
        parser.add_argument(
            "--code",
            default="",
            help="Force a specific code (to keep an already-shared link).",
        )
        parser.add_argument(
            "--code-prefix",
            dest="code_prefix",
            default="",
            help="Prefix for a newly minted code. Default: tenant initials.",
        )
        parser.add_argument(
            "--event-type",
            dest="event_types",
            action="append",
            default=[],
            help=(
                "Program name (loose match) the link opens. Repeat for a brand "
                "running several; the FIRST is the pinned default. Omitted, "
                "the tenant's only event type is used."
            ),
        )
        parser.add_argument(
            "--photo-buckets",
            dest="photo_buckets",
            default="",
            help=(
                'JSON array of {name, helper, min} dropzones, or a map of '
                '{"<event type>": [...]} for a multi-program brand. Omitted, '
                "the tenant's existing categories are used."
            ),
        )
        parser.add_argument(
            "--location-mode",
            dest="location_mode",
            choices=[Tenant.CHECKIN_LOCATION_ADDRESS, Tenant.CHECKIN_LOCATION_MARKET],
            default=None,
            help="How the link asks 'where are you working?'. Default: leave as-is.",
        )
        parser.add_argument(
            "--training-url",
            dest="training_url",
            default="",
            help="BA reference link on the check-in page. Blank = leave as-is.",
        )
        parser.add_argument(
            "--recap-only",
            dest="recap_only",
            action="store_true",
            help=(
                "Mint/keep Tenant.checkin_recap_code — a second URL that skips "
                "the time clock (3rd-party / agency recaps). Does NOT touch "
                "checkin_code, the BA clock link."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this the command only reports.",
        )

    # -- resolve -----------------------------------------------------------

    def _resolve_tenant(self, tenant_id: int | None, needle: str) -> Tenant:
        if tenant_id:
            tenant = Tenant.objects.filter(id=tenant_id).first()
            if tenant is None:
                raise CommandError(f"No tenant with id={tenant_id}.")
            return tenant
        needle = (needle or "").strip()
        if not needle:
            raise CommandError("Pass --tenant-id (preferred) or --tenant.")
        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if not matches:
            raise CommandError(f"No tenant matches {needle!r}.")
        if len(matches) > 1:
            for t in matches:
                self.stdout.write(f"  [{t.id}] {t.name!r} / {t.slug!r}")
            raise CommandError(
                f"{len(matches)} tenants match {needle!r} — pass --tenant-id."
            )
        return matches[0]

    def _mint_code(self, prefix: str) -> str:
        for _ in range(50):
            body = "".join(secrets.choice(ALPHABET) for _ in range(CODE_BODY_LENGTH))
            candidate = f"{prefix}-{body}"
            taken = Tenant.objects.filter(checkin_code=candidate).exists() or (
                Tenant.objects.filter(checkin_recap_code=candidate).exists()
            )
            if not taken:
                return candidate
        raise CommandError("Could not mint an unused check-in code.")

    def _resolve_event_types(self, tenant, needles: list[str]) -> list:
        """Match each --event-type against the tenant's own types, in order.

        Refuses to guess. A wrong match here hands BAs the wrong recap form,
        which is invisible at submit time — so an ambiguous needle is an error,
        never a best guess.
        """
        from events.models import EventType

        available = list(EventType.objects.filter(tenant=tenant).order_by("id"))
        if not available:
            raise CommandError(
                f"Tenant {tenant.slug!r} has no event types — nothing to pin, so "
                "the link would hand BAs an arbitrary form. Create one first."
            )

        if not needles:
            if len(available) == 1:
                return available
            raise CommandError(
                f"Tenant has {len(available)} event types and no --event-type "
                "given. Name the program(s) explicitly: "
                + ", ".join(repr(e.name) for e in available)
            )

        chosen: list = []
        for needle in needles:
            key = needle.strip().lower()
            hits = [e for e in available if key in (e.name or "").lower()]
            if not hits:
                raise CommandError(
                    f"No event type on {tenant.slug!r} matches {needle!r}. "
                    "Has: " + ", ".join(repr(e.name) for e in available)
                )
            if len(hits) > 1:
                raise CommandError(
                    f"{needle!r} matches {len(hits)} event types "
                    f"({', '.join(repr(e.name) for e in hits)}) — be specific."
                )
            if hits[0].id not in {e.id for e in chosen}:
                chosen.append(hits[0])
        return chosen

    def _parse_buckets(self, raw: str, programs: list) -> dict:
        """Return {event type name: [bucket spec, ...]}.

        A flat list applies to every program. A map is keyed by program name,
        matched the same fuzzy way bucket names match categories.
        """
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"--photo-buckets is not valid JSON: {exc}") from exc

        def _clean(entries, where: str) -> list[dict]:
            if not isinstance(entries, list):
                raise CommandError(f"--photo-buckets {where} must be a JSON array.")
            out = []
            for i, e in enumerate(entries, start=1):
                if not isinstance(e, dict) or not (e.get("name") or "").strip():
                    raise CommandError(
                        f"--photo-buckets {where}[{i}] needs a non-empty 'name'."
                    )
                item = {"name": e["name"].strip()}
                if e.get("helper"):
                    item["helper"] = str(e["helper"])
                if e.get("min"):
                    item["min"] = int(e["min"])
                out.append(item)
            return out

        if isinstance(parsed, list):
            flat = _clean(parsed, "")
            return {p.name: list(flat) for p in programs}

        if isinstance(parsed, dict):
            by_norm = {_norm(p.name): p for p in programs}
            out: dict = {}
            for key, entries in parsed.items():
                if _norm(key) == _norm("default"):
                    out["default"] = _clean(entries, f"[{key!r}]")
                    continue
                program = by_norm.get(_norm(key))
                if program is None:
                    raise CommandError(
                        f"--photo-buckets key {key!r} is not one of the programs "
                        "on this link: "
                        + ", ".join(repr(p.name) for p in programs)
                    )
                out[program.name] = _clean(entries, f"[{key!r}]")
            return out

        raise CommandError("--photo-buckets must be a JSON array or object.")

    # -- main --------------------------------------------------------------

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        tenant = self._resolve_tenant(opts["tenant_id"], opts["tenant"])
        if opts.get("recap_only"):
            return self._handle_recap_only(tenant, opts)
        forced_code = (opts["code"] or "").strip().upper()
        training_url = (opts["training_url"] or "").strip()
        location_mode = opts["location_mode"]

        programs = self._resolve_event_types(tenant, opts["event_types"])
        prefix = (opts["code_prefix"] or _derive_prefix(tenant)).strip().upper().rstrip("-")

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TENANT : [{tenant.id}] {tenant.name!r} / {tenant.slug!r}\n"
            f"CODE   : {tenant.checkin_code or '(none yet)'}\n"
            f"MODE   : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)

        if tenant.checkin_code and not forced_code:
            self.stdout.write(
                "\n  Tenant already has a code — KEEPING it. Every BA holding "
                "the current URL stays pointed at the right place."
            )
        new_code = (
            forced_code
            or tenant.checkin_code
            or (self._mint_code(prefix) if apply else f"{prefix}-XXXXXX")
        )

        # -- programs ------------------------------------------------------
        from events.models import EventType

        all_types = list(EventType.objects.filter(tenant=tenant).order_by("id"))
        self.stdout.write("\n  Programs on this link:")
        for i, etype in enumerate(programs):
            tag = "   ← pinned default" if i == 0 else ""
            self.stdout.write(f"    [{etype.id}] {etype.name!r}{tag}")
        if len(programs) == 1:
            self.stdout.write(
                "    (one program — the page asks nothing and just uses it)"
            )
        if all_types and all_types[0].id != programs[0].id:
            self.stdout.write(
                self.style.WARNING(
                    f"    Without the pin the fallback would be "
                    f"[{all_types[0].id}] {all_types[0].name!r} — the wrong form."
                )
            )

        # Which recap form each program resolves to. Reported, never created:
        # seeding a duplicate template splits a brand's recaps across two forms
        # and halves every dashboard number.
        from recaps.models import CustomRecapTemplate

        self.stdout.write("\n  Recap form each program opens:")
        for etype in programs:
            tpl = CustomRecapTemplate.objects.filter(
                tenant=tenant, event_type_id=etype.id
            ).first()
            if tpl is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"    {etype.name!r} → NO TEMPLATE. BAs get the generic "
                        "form. Build one before sharing this link."
                    )
                )
            else:
                from recaps.models import CustomField

                n = CustomField.objects.filter(custom_recap_template=tpl).count()
                self.stdout.write(
                    f"    {etype.name!r} → [{tpl.id}] {tpl.name!r} ({n} fields)"
                )

        # -- photo buckets -------------------------------------------------
        wanted = self._parse_buckets(opts["photo_buckets"], programs)
        bucket_plan = self._plan_buckets(tenant, wanted, programs)

        # -- location / training -------------------------------------------
        self.stdout.write("")
        self.stdout.write(
            f"  Location mode : {location_mode or tenant.checkin_location_mode}"
            + ("" if location_mode else "  (unchanged)")
        )
        if training_url:
            self.stdout.write(f"  Training link : {training_url}")

        if not apply:
            self.stdout.write("")
            self.stdout.write(
                f"DRY-RUN — would serve https://client.igniteproductions.co"
                f"/checkin/{new_code}\nRe-run with --apply to write."
            )
            return

        with transaction.atomic():
            tenant.checkin_code = new_code
            tenant.checkin_event_type = programs[0]
            if location_mode:
                tenant.checkin_location_mode = location_mode
            if training_url:
                tenant.checkin_training_url = training_url
            if wanted or bucket_plan:
                tenant.checkin_photo_buckets = self._ensure_buckets(
                    tenant, wanted, bucket_plan, programs
                )
            tenant.save(
                update_fields=[
                    "checkin_code",
                    "checkin_event_type",
                    "checkin_location_mode",
                    "checkin_training_url",
                    "checkin_photo_buckets",
                ]
            )
            # `set`, not `add` — this list is the whole truth, so dropping a
            # program from the arguments also takes it off the link.
            tenant.checkin_event_types.set(programs)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(self.style.SUCCESS(f"CHECKIN_CODE: {tenant.checkin_code}"))
        self.stdout.write(
            f"CHECKIN_URL: https://client.igniteproductions.co"
            f"/checkin/{tenant.checkin_code}"
        )
        self.stdout.write("=" * 72)

    def _handle_recap_only(self, tenant, opts) -> None:
        """Mint the recap-only URL without touching the BA clock code."""
        apply = bool(opts["apply"])
        forced = (opts["code"] or "").strip().upper()
        prefix = (opts["code_prefix"] or _derive_prefix(tenant)).strip().upper().rstrip("-")
        existing = (tenant.checkin_recap_code or "").strip()
        if forced:
            new_code = forced
        elif existing:
            new_code = existing
        else:
            new_code = self._mint_code(prefix) if apply else f"{prefix}-XXXXXX"

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TENANT     : [{tenant.id}] {tenant.name!r} / {tenant.slug!r}\n"
            f"CLOCK CODE : {tenant.checkin_code or '(none)'}  (untouched)\n"
            f"RECAP CODE : {existing or '(none yet)'}\n"
            f"MODE       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)
        if tenant.checkin_event_type_id is None:
            self.stdout.write(
                self.style.WARNING(
                    "  No pinned checkin_event_type — recap-only still needs "
                    "the BA link's program pin so filers get the right form."
                )
            )
        if not apply:
            self.stdout.write("")
            self.stdout.write(
                f"DRY-RUN — would serve https://client.igniteproductions.co"
                f"/checkin/{new_code}  (recap-only, no time clock)\n"
                "Re-run with --apply to write."
            )
            return
        if existing and existing.upper() == new_code:
            self.stdout.write(f"\nKEEPING recap code {existing}.")
        tenant.checkin_recap_code = new_code
        tenant.save(update_fields=["checkin_recap_code"])
        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(self.style.SUCCESS(f"CHECKIN_RECAP_CODE: {tenant.checkin_recap_code}"))
        self.stdout.write(
            f"CHECKIN_RECAP_URL: https://client.igniteproductions.co"
            f"/checkin/{tenant.checkin_recap_code}"
        )
        self.stdout.write("NO TIME CLOCK. Name + date + store + recap questions only.")
        self.stdout.write("=" * 72)

    # -- buckets -----------------------------------------------------------

    def _plan_buckets(self, tenant, wanted: dict, programs: list) -> dict:
        """Report the tenant's CURRENT categories, then decide per bucket name:
        reuse the row already playing this role, relabel it, or create one.

        Deduped by normalized name across all programs, so a bucket two
        programs share is created once and both lists point at the same row.
        Reporting first is the point — the failure mode is silent (a second
        near-identical bucket in the recap PDF), so the dry run has to show
        what exists before anything is written.
        """
        from recaps.models import FileRecapCategory

        existing = list(
            FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id")
        )
        self.stdout.write(
            f"\n  Photo categories on this tenant today: {len(existing)}"
        )
        for cat in existing:
            protected = (
                "  [sentinel target — never renamed]"
                if any(_norm(cat.name) == _norm(n) for n in SENTINEL_CATEGORY_NAMES)
                else ""
            )
            self.stdout.write(f"    [{cat.id}] {cat.name!r}{protected}")

        if not wanted:
            self.stdout.write(
                "\n  No --photo-buckets given — leaving the generic photo grid "
                "in place and not touching categories."
            )
            return {}

        # Lowest id wins a tie, so a duplicate pair resolves to the older row —
        # the one history is already filed against — rather than flip-flopping.
        by_norm: dict[str, object] = {}
        for cat in existing:
            by_norm.setdefault(_norm(cat.name), cat)

        plan: dict = {}
        for key_name, entries in wanted.items():
            self.stdout.write(f"\n  Dropzones — {key_name}:")
            for spec in entries:
                name = spec["name"]
                key = _norm(name)
                hint = (
                    f"  (min {spec['min']}, {spec.get('helper', '')!r})"
                    if spec.get("min")
                    else ""
                )
                if key in plan:
                    self.stdout.write(
                        f"    ⇄ {name!r} — shared with an earlier program, one row"
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
                elif any(
                    _norm(match.name) == _norm(n) for n in SENTINEL_CATEGORY_NAMES
                ):
                    # Never rename a sentinel out from under the upload path.
                    plan[key]["category"] = None
                    self.stdout.write(
                        self.style.WARNING(
                            f"    + {name!r} — [{match.id}] {match.name!r} is a "
                            "sentinel target; creating a separate row instead"
                        )
                    )
                else:
                    self.stdout.write(
                        f"    ~ {name!r} — reusing [{match.id}] {match.name!r}, "
                        f"relabelling in place{hint}"
                    )
        return plan

    def _ensure_buckets(
        self, tenant, wanted: dict, plan: dict, programs: list
    ) -> dict:
        """Create/relabel each bucket's category once, then return the config
        for ``Tenant.checkin_photo_buckets``, keyed by event type name.

        Relabelling in place (rather than creating a second row) keeps photos
        already filed under the old label inside the bucket they belong to; a
        fresh row would strand that history in an orphan category that still
        shows up in the recap PDF.
        """
        from recaps.models import FileRecapCategory

        creator = getattr(tenant, "created_by", None)
        for entry in plan.values():
            name = entry["name"]
            cat = entry["category"]
            if cat is None:
                cat = FileRecapCategory.objects.create(
                    name=name, tenant_id=tenant.id, created_by=creator
                )
                entry["category"] = cat
                self.stdout.write(f"  created    [{cat.id}] {name!r}")
            elif cat.name != name:
                old = cat.name
                cat.name = name
                cat.save(update_fields=["name", "updated_at"])
                self.stdout.write(f"  relabelled [{cat.id}] {old!r} → {name!r}")
            else:
                self.stdout.write(f"  kept       [{cat.id}] {name!r}")

        # A single-program brand stores a flat list; the per-program map only
        # earns its keys when there is more than one program to tell apart.
        if len(programs) == 1 and len(wanted) == 1:
            return next(iter(wanted.values()))
        return wanted
