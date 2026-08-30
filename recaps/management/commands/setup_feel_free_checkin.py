"""Set up Feel Free field capture: recap template + standing check-in link.

The Feel Free twin of ``setup_total_wireless_checkin`` — same two things in one
idempotent command, same reasoning (see that module's docstring):

1. The **Sampling Recap** custom template, field-for-field off the client's own
   "Feel Free Sampling Recap" PDF. Todays Date and Event Location are NOT
   template fields; they belong to the event the check-in resolves, and the
   recap PDF already renders them from there.

2. The tenant's **standing check-in code** — one durable ``/checkin/<code>``
   link the whole Feel Free field team uses all season. Clock in, work, file
   this recap, clock out. Several BAs at one location on one day land on the
   SAME event.

DRY-RUN by default. Because Feel Free is an ESTABLISHED tenant that may already
have a template (unlike Total Wireless, which was new), the dry run also dumps
every existing template on the tenant — seeding a near-duplicate would split
the brand's recaps across two forms and quietly halve every dashboard number.
**Read that list before passing --apply.**

Run via ``/internal/cron/setup-feel-free-checkin`` (or the "Setup Feel Free
check-in" GitHub Action) so it executes against prod.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# Reuse the TW command's field-type fuzzy matcher rather than forking it — the
# rules mirror the FE's `customFieldKind` and the two must not drift.
from recaps.management.commands.setup_total_wireless_checkin import (
    _match_field_type,
)

TEMPLATE_NAME = "Feel Free · Sampling Recap"

# Taken field-for-field from "Feel Free Sampling Recap" (Heather Kinzer,
# 08/02/2026). Two deliberate departures from the PDF, both flagged to Kyle:
#
#  - "tasing" is corrected to "tasting". It's a typo in the client's form and
#    it would be baked into every recap and export from here on.
#  - The trailing "::" on "Demographics::" / "Helpful feedback::" is dropped;
#    that's an artifact of how the old form rendered labels, not a name.
#
# Kava Matte and Classic Tonic are explicit number fields rather than the
# structured per-SKU product picker. The PDF asks for exactly these two SKUs by
# name, and hard-coding them guarantees the client's report matches the form
# they already know. If Feel Free's SKU list grows, switch this section to
# product_samples=True and seed Product rows instead.
SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    (
        "Products Sampled",
        [
            ("Quantity Distributed of Kava Matte", "number", True, []),
            ("Quantity Distributed of Classic Tonic", "number", True, []),
        ],
    ),
    (
        "Consumer Engagement",
        [
            ("How many TOTAL consumers did you sample?", "number", True, []),
            (
                "How many consumers would be willing to purchase the product "
                "after tasting it?",
                "number",
                True,
                [],
            ),
            (
                "How many consumers that were engaged with knew about Feel "
                "Free product/brand?",
                "number",
                True,
                [],
            ),
            (
                "How many consumers had tried a Feel Free flavor before?",
                "number",
                True,
                [],
            ),
            ("Demographics", "longtext", True, []),
            (
                "What were the top 5 frequently asked questions you received "
                "from consumers?",
                "longtext",
                True,
                [],
            ),
        ],
    ),
    (
        "Photos",
        [
            ("Sampling Pictures", "image", True, []),
        ],
    ),
    (
        # Written summary of the roaming day. Distinct from mid-shift "Log
        # this stop" GPS pins — admins reading the filed recap need the BA's
        # own short list of spots and when they worked, not a stop trail.
        "Sampling Details",
        [
            (
                "Where did you sample? (name a few locations)",
                "longtext",
                True,
                [],
            ),
            ("Sampling Timeframe?", "text", True, []),
            # Filled automatically from payable-mileage itinerary on check-in
            # submit (storage→stops or stops-only). Not typed by the BA.
            ("Mileage", "number", True, []),
        ],
    ),
    (
        "Wrap Up",
        [
            ("Helpful feedback", "longtext", False, []),
        ],
    ),
]

CODE_PREFIX = "FF-"

# Appended to the LIVE Feel Free form ("Feel Free - Field Sampling") without
# re-seeding the whole SPEC — that template name predates the seeder default
# and applying SPEC under the default title would mint a second form.
SAMPLING_DETAIL_FIELDS: list[tuple[str, str, bool]] = [
    ("Where did you sample? (name a few locations)", "longtext", True),
    ("Sampling Timeframe?", "text", True),
    # Auto-filled from the storage→stops itinerary on walk-up submit.
    # BA never types this — check-in writes the computed payable miles.
    ("Mileage", "number", True),
]
SAMPLING_DETAIL_SECTION = "Sampling Details"


class Command(BaseCommand):
    help = (
        "Set up Feel Free: sampling recap template + standing check-in link "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="feel free",
            help="tenant name/slug substring (case-insensitive). Default: 'feel free'.",
        )
        parser.add_argument(
            "--template-name",
            dest="template_name",
            default=TEMPLATE_NAME,
            help=f"template name. Default: {TEMPLATE_NAME!r}.",
        )
        parser.add_argument(
            "--event-type",
            dest="event_type",
            default=None,
            help="event type name substring. Default: prefer 'sampling', else the tenant's first.",
        )
        parser.add_argument(
            "--add-photo-field",
            dest="add_photo_field",
            default=None,
            help=(
                "add an IMAGE field with this name to the brand's existing "
                "template if absent. For a brand whose only image field is "
                "something like 'corporate card receipts', a BA has nowhere "
                "labelled to put their sampling photos."
            ),
        )
        parser.add_argument(
            "--photo-section",
            dest="photo_section",
            default="Photos",
            help="section for --add-photo-field (default: 'Photos').",
        )
        parser.add_argument(
            "--add-sampling-details",
            dest="add_sampling_details",
            action="store_true",
            help=(
                "add 'Where did you sample?' + 'Sampling Timeframe?' to the "
                "tenant's EXISTING sole template (Feel Free - Field Sampling). "
                "Skips SPEC seeding so we do not mint a second form."
            ),
        )
        parser.add_argument(
            "--location-mode",
            dest="location_mode",
            choices=["address", "market"],
            default=None,
            help=(
                "how the link asks 'where are you working?'. 'market' for a "
                "ROAMING crew (event keyed on the market, spots logged as "
                "stops); 'address' for a static store activation. Omit to "
                "leave the tenant's current setting alone."
            ),
        )
        parser.add_argument(
            "--code-only",
            dest="code_only",
            action="store_true",
            help=(
                "mint the standing check-in code and DO NOTHING to templates. "
                "Use when the brand already has its recap form in Spark — the "
                "link resolves it on its own."
            ),
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'BD' -> BD-XXXXXX. "
                f"Blank keeps this command's default ({CODE_PREFIX!r}). Set it "
                "whenever you point this at a brand that isn't Feel Free — the "
                "code is permanent, and an FF- link on another brand misleads "
                "every BA who reads it."
            ),
        )
        parser.add_argument(
            "--pin-event-type",
            dest="pin_event_type",
            action="store_true",
            help=(
                "also pin Tenant.checkin_event_type to the resolved event type. "
                "Without it the walk-in path stamps the tenant's LOWEST-ID "
                "event type, which is arbitrary — harmless while a brand has "
                "one template (the sole-template fallback still finds the right "
                "form) but it mislabels the event for reporting."
            ),
        )
        parser.add_argument(
            "--seed-storage-units",
            dest="seed_storage_units",
            action="store_true",
            help=(
                "write Feel Free market storage-unit addresses onto "
                "Tenant.checkin_storage_units (enables payable-mileage capture "
                "on the standing check-in)."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing).",
        )

    # ---- resolvers -------------------------------------------------------

    def _resolve_tenant(self, needle: str):
        from django.db.models import Q

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
                f"No tenant matches {needle!r}. If it isn't in the list above "
                f"it needs onboarding first (tenant + event types)."
            )
        raise CommandError(
            f"{needle!r} matched {len(matches)} tenants "
            f"({', '.join(repr(t.slug) for t in matches)}) — narrow --tenant."
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
            qs.filter(name__icontains="sampling").first()
            or (existing.event_type if existing else None)
            or qs.first()
        )

    def _resolve_field_type(self, kind: str, creator, apply: bool, cache: dict):
        if kind in cache:
            return cache[kind]
        from recaps.models import CustomRecapFieldType

        existing = None
        for ft in CustomRecapFieldType.objects.all():
            if _match_field_type(kind, (ft.name or "").lower()):
                existing = ft
                break
        if existing is None:
            if apply:
                existing = CustomRecapFieldType.objects.create(
                    name=kind, created_by=creator
                )
                self.stdout.write(f"    (created field type {kind!r})")
            else:
                existing = f"<would-create '{kind}'>"
        cache[kind] = existing
        return existing

    def _report_existing_templates(self, tenant, template_name: str) -> None:
        """Feel Free is an established tenant — surface what it already has.

        Seeding a second near-identical template is the failure that splits a
        brand's recaps across two forms, so this is printed loudly rather than
        left for the operator to go looking for.
        """
        from recaps.models import CustomField, CustomRecapTemplate

        rows = list(
            CustomRecapTemplate.objects.filter(tenant_id=tenant.id).order_by("id")
        )
        self.stdout.write("\nExisting templates on this tenant:")
        if not rows:
            self.stdout.write("  (none — this will be the first)")
            return
        for t in rows:
            n_fields = CustomField.objects.filter(custom_recap_template=t).count()
            same = " <-- SAME NAME, will be reused" if t.name == template_name else ""
            self.stdout.write(
                f"  [{t.id}] {t.name!r} — {n_fields} field(s), "
                f"event_type={getattr(t.event_type, 'name', None)!r}{same}"
            )
            for cf in (
                CustomField.objects.filter(custom_recap_template=t)
                .select_related("custom_field_type", "recap_section")
                .order_by("recap_section__order", "order", "id")
            ):
                kind = (getattr(cf.custom_field_type, "name", "") or "?").lower()
                sec = getattr(cf.recap_section, "name", None)
                self.stdout.write(
                    f"        · {cf.name!r} [{kind}]"
                    f"{' REQUIRED' if cf.required else ''}  (section {sec!r})"
                )
        if not any(t.name == template_name for t in rows):
            self.stdout.write(
                self.style.WARNING(
                    "  NOTE: none match the name being seeded. If one of the "
                    "above is already Feel Free's live sampling form, EDIT it "
                    "instead of applying this — two templates split the brand's "
                    "recaps and halve every dashboard number."
                )
            )

    # ---- handle ----------------------------------------------------------

    def handle(self, *args, **opts):
        apply = opts["apply"]
        tenant = self._resolve_tenant(opts["tenant"])
        creator = self._resolve_creator()
        event_type = self._resolve_event_type(tenant, opts["event_type"])
        template_name = opts["template_name"]

        self.stdout.write("=" * 68)
        self.stdout.write(
            f"Tenant     : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
        )
        self.stdout.write(f"Template   : {template_name!r}")
        self.stdout.write(
            f"Event type : {getattr(event_type, 'name', None)!r} "
            f"(id {getattr(event_type, 'id', None)})"
        )
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        if event_type is None:
            raise CommandError(
                f"Tenant {tenant.slug!r} has no event types — run "
                f"set_tenant_event_types first, then re-run this."
            )

        self._report_existing_templates(tenant, template_name)
        self._location_mode(tenant, opts.get("location_mode"), apply)
        self._seed_storage_units(
            tenant,
            apply=apply,
            force=bool(opts.get("seed_storage_units")),
        )

        if opts.get("add_photo_field"):
            self._add_photo_field(
                tenant, creator, opts["add_photo_field"], opts["photo_section"], apply
            )

        if opts.get("add_sampling_details"):
            self._add_sampling_details(tenant, creator, apply)
            # Live Feel Free form is "Feel Free - Field Sampling" — never mint
            # a second template from SPEC when this flag is the reason we ran.
            self._checkin_code(tenant, apply, opts.get("prefix"))
            return

        if opts.get("pin_event_type"):
            self._pin_event_type(tenant, event_type, apply)

        if opts.get("code_only"):
            self.stdout.write(
                self.style.WARNING(
                    "\n--code-only: leaving templates untouched. The standing "
                    "link resolves the brand's existing form by itself."
                )
            )
            self._checkin_code(tenant, apply, opts.get("prefix"))
            return

        ft_cache: dict = {}
        for _, fields in SPEC:
            for _, kind, _, _ in fields:
                self._resolve_field_type(kind, creator, apply, ft_cache)

        from recaps.models import CustomField, CustomRecapTemplate, RecapSection

        def _run():
            created = {"sections": 0, "fields": 0}
            updated = {"sections": 0, "fields": 0}

            template = None
            if apply:
                template, made = CustomRecapTemplate.objects.get_or_create(
                    tenant_id=tenant.id,
                    name=template_name,
                    defaults={
                        "event_type": event_type,
                        "product_samples": False,
                        "sales_performance": False,
                        "layout": {},
                        "created_by": creator,
                    },
                )
                self.stdout.write(
                    f"\nTemplate {'CREATED' if made else 'exists'} "
                    f"(id {template.id}, uuid {template.uuid})"
                )

            for s_idx, (section_name, fields) in enumerate(SPEC):
                self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
                section = None
                if apply:
                    section, made = RecapSection.objects.get_or_create(
                        tenant_id=tenant.id,
                        name=section_name,
                        defaults={"order": s_idx, "created_by": creator},
                    )
                    if made:
                        created["sections"] += 1
                    elif section.order != s_idx:
                        section.order = s_idx
                        section.save(update_fields=["order", "updated_at"])
                        updated["sections"] += 1

                for f_idx, (fname, kind, required, options) in enumerate(fields):
                    ft = ft_cache[kind]
                    req = "REQUIRED" if required else "optional"
                    self.stdout.write(f"    - {fname!r}  [{kind}] {req}")
                    if not apply:
                        continue
                    field, made = CustomField.objects.get_or_create(
                        custom_recap_template=template,
                        recap_section=section,
                        name=fname,
                        defaults={
                            "custom_field_type": ft,
                            "required": required,
                            "options": list(options),
                            "order": f_idx,
                            "created_by": creator,
                        },
                    )
                    if made:
                        created["fields"] += 1
                    else:
                        changed = []
                        if field.custom_field_type_id != ft.id:
                            field.custom_field_type = ft
                            changed.append("custom_field_type")
                        if field.required != required:
                            field.required = required
                            changed.append("required")
                        if field.order != f_idx:
                            field.order = f_idx
                            changed.append("order")
                        if changed:
                            changed.append("updated_at")
                            field.save(update_fields=changed)
                            updated["fields"] += 1

            self.stdout.write("\n" + "=" * 68)
            if apply:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"APPLIED — template {template.uuid} · "
                        f"sections +{created['sections']}/~{updated['sections']} · "
                        f"fields +{created['fields']}/~{updated['fields']}."
                    )
                )
            else:
                total_fields = sum(len(f) for _, f in SPEC)
                self.stdout.write(
                    self.style.WARNING(
                        f"DRY-RUN — would create/reconcile {len(SPEC)} sections + "
                        f"{total_fields} fields on tenant {tenant.slug!r}. "
                        f"Re-run with --apply to write."
                    )
                )

        if apply:
            with transaction.atomic():
                _run()
        else:
            _run()

        self._checkin_code(tenant, apply, opts.get("prefix"))

    def _add_photo_field(
        self, tenant, creator, field_name: str, section_name: str, apply: bool
    ) -> None:
        """Append one image field to the tenant's EXISTING template.

        Photos on the check-in page are a single shared pool — the template's
        image fields are prompts, not separate buckets — so adding one is a
        labelling change, not a data-model one. It is what gives a BA a place
        that actually says "sampling pictures".

        Refuses to guess: exactly one template, or nothing happens.
        """
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )

        templates = list(CustomRecapTemplate.objects.filter(tenant_id=tenant.id))
        if len(templates) != 1:
            raise CommandError(
                f"--add-photo-field needs exactly one template on the tenant; "
                f"found {len(templates)}. Use the admin template editor instead."
            )
        tpl = templates[0]

        existing = CustomField.objects.filter(
            custom_recap_template=tpl, name__iexact=field_name.strip()
        ).first()
        self.stdout.write(f"\nPhoto field {field_name!r} on {tpl.name!r}:")
        if existing:
            self.stdout.write("  already present — nothing to do")
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"  DRY-RUN — would add an image field in section "
                    f"{section_name!r}"
                )
            )
            return

        ftype = None
        for ft in CustomRecapFieldType.objects.all():
            if _match_field_type("image", (ft.name or "").lower()):
                ftype = ft
                break
        if ftype is None:
            ftype = CustomRecapFieldType.objects.create(
                name="image", created_by=creator
            )

        section, _ = RecapSection.objects.get_or_create(
            tenant_id=tenant.id,
            name=section_name,
            defaults={"order": 90, "created_by": creator},
        )
        last = (
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
        )
        CustomField.objects.create(
            custom_recap_template=tpl,
            recap_section=section,
            name=field_name.strip(),
            custom_field_type=ftype,
            required=False,
            options=[],
            order=(last or 0) + 1,
            created_by=creator,
        )
        self.stdout.write(
            self.style.SUCCESS(f"  ADDED image field in section {section_name!r}")
        )

    def _add_sampling_details(self, tenant, creator, apply: bool) -> None:
        """Append where-sampled + timeframe to the tenant's sole live template.

        Feel Free's production form is ``Feel Free - Field Sampling`` (not
        the seeder default title). This is the safe path to grow that form
        without splitting recaps across two templates.
        """
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )

        templates = list(CustomRecapTemplate.objects.filter(tenant_id=tenant.id))
        if len(templates) != 1:
            raise CommandError(
                f"--add-sampling-details needs exactly one template on the "
                f"tenant; found {len(templates)}. Pass the live form through "
                f"the admin editor if there is more than one."
            )
        tpl = templates[0]
        self.stdout.write(f"\nSampling detail fields on {tpl.name!r}:")

        ft_cache: dict = {}
        for _, kind, _ in SAMPLING_DETAIL_FIELDS:
            self._resolve_field_type(kind, creator, apply, ft_cache)

        if not apply:
            for fname, kind, required in SAMPLING_DETAIL_FIELDS:
                exists = CustomField.objects.filter(
                    custom_recap_template=tpl, name__iexact=fname
                ).exists()
                state = "already present" if exists else f"would add [{kind}]"
                req = " REQUIRED" if required else ""
                self.stdout.write(
                    self.style.WARNING(
                        f"  DRY-RUN — {fname!r}{req}: {state} "
                        f"(section {SAMPLING_DETAIL_SECTION!r})"
                    )
                )
            return

        section, _ = RecapSection.objects.get_or_create(
            tenant_id=tenant.id,
            name=SAMPLING_DETAIL_SECTION,
            defaults={"order": 85, "created_by": creator},
        )
        last = (
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
        )
        order = last or 0
        for fname, kind, required in SAMPLING_DETAIL_FIELDS:
            existing = CustomField.objects.filter(
                custom_recap_template=tpl, name__iexact=fname
            ).first()
            if existing:
                self.stdout.write(f"  {fname!r} already present — skip")
                continue
            order += 1
            ft = ft_cache[kind]
            CustomField.objects.create(
                custom_recap_template=tpl,
                recap_section=section,
                name=fname,
                custom_field_type=ft,
                required=required,
                options=[],
                order=order,
                created_by=creator,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  ADDED {fname!r} [{kind}] in {SAMPLING_DETAIL_SECTION!r}"
                )
            )

    def _location_mode(self, tenant, mode: str | None, apply: bool) -> None:
        """Set (or just report) how the link asks for location."""
        from ambassadors import checkin_web

        current = checkin_web.tenant_location_mode(tenant)
        markets = checkin_web.tenant_markets(tenant)
        self.stdout.write(
            f"\nLocation mode: {current!r}"
            + (f"  markets={markets}" if markets else "  (no market list found)")
        )
        if mode is None or mode == current:
            return
        if mode == "market" and not markets:
            raise CommandError(
                "Can't switch to market mode: no market list. Add options to "
                "the brand's 'Event Location'-style recap field, or set "
                "Tenant.checkin_markets."
            )
        if not apply:
            self.stdout.write(
                self.style.WARNING(f"DRY-RUN — would set location mode to {mode!r}")
            )
            return
        tenant.checkin_location_mode = mode
        tenant.save(update_fields=["checkin_location_mode"])
        self.stdout.write(self.style.SUCCESS(f"Location mode set to {mode!r}"))

    def _seed_storage_units(self, tenant, *, apply: bool, force: bool = False) -> None:
        """Write Feel Free market → storage unit addresses for payable mileage."""
        from ambassadors.payable_mileage import FEEL_FREE_STORAGE_UNITS

        current = getattr(tenant, "checkin_storage_units", None) or []
        self.stdout.write(
            f"\nStorage units: {len(current)} configured"
            + (
                f" ({', '.join(u.get('market', '?') for u in current if isinstance(u, dict))})"
                if current
                else ""
            )
        )
        if current and not force:
            self.stdout.write("  already seeded — left as-is (pass --seed-storage-units to rewrite)")
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would set checkin_storage_units to "
                    f"{len(FEEL_FREE_STORAGE_UNITS)} markets"
                )
            )
            for u in FEEL_FREE_STORAGE_UNITS:
                self.stdout.write(f"    · {u['market']}: {u['address']}")
            return
        tenant.checkin_storage_units = list(FEEL_FREE_STORAGE_UNITS)
        tenant.save(update_fields=["checkin_storage_units"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(FEEL_FREE_STORAGE_UNITS)} storage units "
                "(payable mileage capture ON)"
            )
        )

    # ---- standing check-in link -----------------------------------------

    def _pin_event_type(self, tenant, event_type, apply: bool) -> None:
        """Pin Tenant.checkin_event_type so the walk-in path stops guessing.

        `_default_event_type` otherwise falls back to the tenant's lowest-id
        EventType, which is arbitrary. With a single template the right form
        still resolves (the sole-template fallback), but the event carries a
        wrong type into every report that groups by it.
        """
        if event_type is None:
            return
        current = getattr(tenant, "checkin_event_type_id", None)
        if current == event_type.id:
            self.stdout.write(
                f"\ncheckin_event_type already pinned to {event_type.name!r} — "
                "left as-is."
            )
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDRY-RUN — would pin checkin_event_type="
                    f"{event_type.name!r} (id {event_type.id})"
                )
            )
            return
        tenant.checkin_event_type = event_type
        tenant.save(update_fields=["checkin_event_type"])
        self.stdout.write(
            self.style.SUCCESS(
                f"\nPinned checkin_event_type={event_type.name!r} "
                f"(id {event_type.id})"
            )
        )

    def _checkin_code(self, tenant, apply: bool, prefix: str = "") -> None:
        """Mint (or report) the tenant's standing check-in code.

        Alphabet drops the characters BAs mistype off a text (0/O, 1/I/L).
        NEVER regenerated once set — the link gets pinned and shared, so
        rotating it silently breaks every copy already in circulation.

        `prefix` overrides this command's Feel Free default so the same tool
        can mint a correctly-branded code for any tenant.
        """
        import secrets

        from tenants.models import Tenant

        raw = (prefix or "").strip().upper().rstrip("-")
        cleaned = "".join(ch for ch in raw if ch.isalpha())
        if raw and not 1 <= len(cleaned) <= 4:
            raise CommandError("--prefix should be 1-4 letters, e.g. BD.")
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
