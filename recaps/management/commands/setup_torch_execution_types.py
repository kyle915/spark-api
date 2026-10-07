"""Torch THC: four execution types on the BA walk-up.

``/checkin/TH-2HRV3D`` (never reminted) opens with "What's your execution
type?" and four options, in this order:

    Event     -> Event Activation    -> Torch THC-Event Activation
    Guerilla  -> Guerilla Activation -> Torch THC · Guerilla Recap
    Seeding   -> Product Seeding     -> Torch THC · Product Seeding Recap
    Retail    -> Retail Sampling     -> Torch THC-Retail Sampling

The labels are display copy (``Tenant.checkin_program_picker``); the event
type names stay descriptive because they drive the recap template, photo
buckets, the Recaps activation filter and the conversion gate. Guerilla
buckets as Event, Seeding as Product Seeding — both stay out of conversion,
which remains Retail Sampling only.

* Guerilla mirrors Event Activation: sampling only (no sales fields),
  typed / GPS location, per-SKU cans sampled from the Product catalog.
* Seeding mirrors Liquid Death Product Seeding: a Drop-off Locations
  repeater (place / GPS or typed address, per-location SKU + cases) and
  Total mileage; the walk-up skips "Where are you working".
* Retail, Event and Guerilla ask "Sample format" (Full can / 4oz pour,
  multi-select, required) and carry an optional Email Data Collection
  section (Email addresses collected / Collection method / notes). Retail
  adds "No samples (dry demo)" so a dry demo can still answer honestly.

The agency twin ``TH-AGENCY`` keeps no picker (Retail Sampling only); it
shares the Retail Sampling template, so it gets the same two additions.
Recaps still land ``approved=False`` (Needs review).

Idempotent. DRY-RUN by default; ``--apply`` writes. Run on prod via
``/internal/cron/setup-torch-execution-types`` (or the "Setup Torch
Execution Types" GitHub Action).
"""

from __future__ import annotations

import re

from django.core.management.base import CommandError
from django.db import transaction

from recaps.management.commands.setup_torch_event_activation import (
    ACTIVATION_BUCKETS,
    COMPETITOR_FIELD,
    EMAIL_FIELDS,
    EMAIL_SECTION,
    EVENT_LABEL,
    PRODUCTS_SAMPLED,
    SAMPLE_FORMAT,
    SAMPLE_FORMAT_FIELD,
    SAMPLE_FORMAT_OPTS,
    SAMPLE_QTY_LAYOUT,
    SERVED_FIELD,
    SERVED_OPTS,
    SPEC_FIELDS,
    TRAFFIC_OPTS,
    Field,
)
from recaps.management.commands.setup_torch_event_activation import (
    Command as EventActivationCommand,
)

GUERILLA_LABEL = "Guerilla Activation"
GUERILLA_TEMPLATE = "Torch THC · Guerilla Recap"
SEEDING_LABEL = "Product Seeding"
SEEDING_TEMPLATE = "Torch THC · Product Seeding Recap"
RETAIL_TEMPLATE = "Torch THC-Retail Sampling"

PICKER_TITLE = "What's your execution type?"
# (event type name, BA label, helper line). Retail's event type name is
# resolved from the pinned program at run time.
PICKER_OPTIONS: list[tuple[str, str, str]] = [
    (
        EVENT_LABEL,
        "Event",
        "Festivals, concerts, sporting and community events — one venue.",
    ),
    (
        GUERILLA_LABEL,
        "Guerilla",
        "Street-team sampling — sidewalks, parks, campuses, outside bars.",
    ),
    (
        SEEDING_LABEL,
        "Seeding",
        "Dropping product off at accounts — not a sampling shift.",
    ),
    (
        "Retail Sampling",
        "Retail",
        "In-store sampling demos at liquor, grocery, and convenience stores.",
    ),
]

RETAIL_DRY_DEMO_OPT = "No samples (dry demo)"
RETAIL_SAMPLE_FORMAT_OPTS = [*SAMPLE_FORMAT_OPTS, RETAIL_DRY_DEMO_OPT]
RETAIL_ANCHORS = ("People engaged", "Total number of consumers sampled")

GUERILLA_SPOT_OPTS = [
    "Sidewalk / Street Corner",
    "Park / Beach",
    "Outside a Bar / Venue",
    "College / Campus",
    "Parking Lot / Tailgate",
    "Other",
]

_EA_ENGAGEMENT = dict(SPEC_FIELDS)["Consumer Engagement"]
_EA_FEEDBACK = {f[0]: f for f in dict(SPEC_FIELDS)["Feedback & Account Notes"]}
GUERILLA_FEEDBACK: list[Field] = [
    _EA_FEEDBACK["Demographics (general age, sex, ethnicities of consumers)"],
    _EA_FEEDBACK["Consumer Feedback"],
    _EA_FEEDBACK["Quotes from Consumers"],
    _EA_FEEDBACK[
        "What were the top 5 frequently asked questions you received from consumers?"
    ],
    _EA_FEEDBACK[COMPETITOR_FIELD],
    ("Positive stories from the day", "longtext", False, [], ""),
    _EA_FEEDBACK["Anything you'd change or do differently?"],
]

GUERILLA_SPEC_FIELDS: list[tuple[str, list[Field]]] = [
    (
        "Guerilla Details",
        [
            ("Where did you set up?", "select", True, list(GUERILLA_SPOT_OPTS), ""),
            ("Describe the foot traffic", "select", True, list(TRAFFIC_OPTS), ""),
            (SERVED_FIELD, "multiselect", True, list(SERVED_OPTS), ""),
            SAMPLE_FORMAT,
        ],
    ),
    ("Consumer Engagement", list(_EA_ENGAGEMENT)),
    (EMAIL_SECTION, list(EMAIL_FIELDS)),
    ("Feedback & Account Notes", list(GUERILLA_FEEDBACK)),
]

GUERILLA_SECTION_ORDER = {
    "Guerilla Details": 0,
    "Consumer Engagement": 1,
    EMAIL_SECTION: 2,
    "Feedback & Account Notes": 3,
    PRODUCTS_SAMPLED: 4,
}

GUERILLA_BUCKETS = list(ACTIVATION_BUCKETS)

DROP_OFF_LOCATIONS_FIELD = "Drop-off Locations"
SEEDING_SPEC: list[tuple[str, list[Field]]] = [
    (
        "Drop-off Details",
        [
            (
                DROP_OFF_LOCATIONS_FIELD,
                "longtext",
                True,
                [],
                "Add every stop: place, SKUs, and cases dropped",
            ),
            (
                "Account feedback",
                "longtext",
                False,
                [],
                "What buyers or managers said when you dropped product off",
            ),
        ],
    ),
    (
        "Mileage",
        [("Total mileage", "number", True, [], "Total miles driven for today's drop-offs")],
    ),
]
SEEDING_SECTION_ORDER = {"Drop-off Details": 0, "Mileage": 1}
SEEDING_BUCKETS: list[dict] = [
    {
        "name": "Drop-off Placement",
        "helper": "Product at each drop-off — shelf, cooler, or back room",
    },
]


def build_guerilla_spec(product_opts: list[str]) -> list[tuple[str, list[Field]]]:
    sections = [(name, list(fields)) for name, fields in GUERILLA_SPEC_FIELDS]
    sections.append(
        (
            PRODUCTS_SAMPLED,
            [
                (
                    PRODUCTS_SAMPLED,
                    "multiselect",
                    True,
                    list(product_opts),
                    "Pick every SKU you poured, then enter cans sampled for each",
                )
            ],
        )
    )
    return sections


class GuerillaCommand(EventActivationCommand):
    event_label = GUERILLA_LABEL
    template_name = GUERILLA_TEMPLATE
    activation_buckets = GUERILLA_BUCKETS
    sample_qty_layout = SAMPLE_QTY_LAYOUT
    section_order = GUERILLA_SECTION_ORDER

    def build_spec(self, product_opts: list[str]) -> list[tuple[str, list[Field]]]:
        return build_guerilla_spec(product_opts)


class SeedingCommand(EventActivationCommand):
    event_label = SEEDING_LABEL
    template_name = SEEDING_TEMPLATE
    activation_buckets = SEEDING_BUCKETS
    sample_qty_layout: dict = {}
    section_order = SEEDING_SECTION_ORDER

    def build_spec(self, product_opts: list[str]) -> list[tuple[str, list[Field]]]:
        return [(name, list(fields)) for name, fields in SEEDING_SPEC]


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


class Command(EventActivationCommand):
    help = (
        "Torch THC: Event / Guerilla / Seeding / Retail execution types on the "
        "TH-2HRV3D walk-up, plus Sample format and Email Data Collection "
        "(dry-run by default; --apply to write)."
    )

    # ── picker copy ───────────────────────────────────────────────────────

    def _retail_program_name(self, tenant) -> str:
        pin = getattr(tenant, "checkin_event_type", None)
        if pin is not None and re.search(r"retail", pin.name or "", re.I):
            return pin.name
        from events.models import EventType

        hit = (
            EventType.objects.filter(tenant_id=tenant.id, name__icontains="retail")
            .order_by("id")
            .first()
        )
        return hit.name if hit else "Retail Sampling"

    def _set_picker(self, tenant, apply: bool) -> dict:
        retail = self._retail_program_name(tenant)
        options = []
        for name, label, description in PICKER_OPTIONS:
            event_type = retail if label == "Retail" else name
            options.append(
                {"eventType": event_type, "label": label, "description": description}
            )
        config = {"title": PICKER_TITLE, "options": options}
        self.stdout.write("\nPicker copy (BA sees, in order):")
        for opt in options:
            self.stdout.write(f"  {opt['label']:<9} -> {opt['eventType']!r}")
        if tenant.checkin_program_picker == config:
            self.stdout.write("  (unchanged)")
        elif apply:
            tenant.checkin_program_picker = config
            tenant.save(update_fields=["checkin_program_picker"])
        return config

    # ── Retail Sampling additions ─────────────────────────────────────────

    def _upsert_field(self, template, section, spec: Field, order: int, creator, apply, cache):
        from recaps.models import CustomField

        name, kind, required, options, placeholder = spec
        req = "REQUIRED" if required else "optional"
        self.stdout.write(f"    - {name}  [{kind}] {req}")
        if not apply:
            self._preview_field_change(template, name, kind, required, options, placeholder)
            return
        ft = self._field_type(kind, creator, apply, cache)
        field = CustomField.objects.filter(custom_recap_template=template, name=name).first()
        want = {
            "recap_section_id": section.id,
            "custom_field_type_id": ft.id,
            "required": required,
            "options": list(options),
            "placeholder": placeholder,
            "order": order,
        }
        if field is None:
            CustomField.objects.create(
                custom_recap_template=template,
                recap_section=section,
                name=name,
                custom_field_type=ft,
                required=required,
                options=list(options),
                placeholder=placeholder,
                order=order,
                created_by=creator,
            )
            return
        changed = [k for k, v in want.items() if getattr(field, k) != v]
        if changed:
            for k in changed:
                setattr(field, k, want[k])
            field.save(update_fields=[*changed, "updated_at"])

    def _augment_retail(self, tenant, creator, apply: bool) -> None:
        from recaps.models import CustomField, CustomRecapTemplate, RecapSection

        templates = list(
            CustomRecapTemplate.objects.filter(tenant_id=tenant.id, name=RETAIL_TEMPLATE)
        )
        if len(templates) != 1:
            raise CommandError(
                f"Expected one {RETAIL_TEMPLATE!r} template, found {len(templates)}."
            )
        tpl = templates[0]
        self.stdout.write(
            f"\nRetail     : [{tpl.id}] {tpl.name!r} (TH-2HRV3D Retail + TH-AGENCY)"
        )
        fields = {
            f.name: f
            for f in CustomField.objects.filter(custom_recap_template=tpl).select_related(
                "recap_section"
            )
        }
        anchor = next((fields[n] for n in RETAIL_ANCHORS if n in fields), None)
        if anchor is None or anchor.recap_section is None:
            raise CommandError(
                f"No consumers-sampled field on {RETAIL_TEMPLATE!r} to anchor Sample format."
            )
        engagement = anchor.recap_section
        cache: dict = {}

        self.stdout.write(f"  [{engagement.order}] {engagement.name} (after {anchor.name!r})")
        existing = fields.get(SAMPLE_FORMAT_FIELD)
        order = existing.order if existing is not None else (anchor.order or 0) + 1
        name, kind, required, _opts, placeholder = SAMPLE_FORMAT
        retail_format: Field = (name, kind, required, list(RETAIL_SAMPLE_FORMAT_OPTS), placeholder)
        self._upsert_field(tpl, engagement, retail_format, order, creator, apply, cache)

        # Own section row, ordered with Consumer Engagement so it renders right
        # after it (sections sort by order, then id).
        email_section = next(
            (
                f.recap_section
                for f in fields.values()
                if f.recap_section is not None and f.recap_section.name == EMAIL_SECTION
            ),
            None,
        )
        if email_section is None and apply:
            email_section = RecapSection.objects.create(
                tenant_id=tenant.id,
                name=EMAIL_SECTION,
                order=engagement.order,
                created_by=creator,
            )
        self.stdout.write(f"  [{engagement.order}] {EMAIL_SECTION}")
        for idx, spec in enumerate(EMAIL_FIELDS):
            self._upsert_field(tpl, email_section, spec, idx, creator, apply, cache)

    # ── entry ─────────────────────────────────────────────────────────────

    def _subcommand(self, cls):
        cmd = cls()
        cmd.stdout = self.stdout
        cmd.style = self.style
        return cmd

    def _run_all(self, tenant, creator, apply: bool) -> None:
        for cls in (EventActivationCommand, GuerillaCommand, SeedingCommand):
            self.stdout.write("\n" + "-" * 68)
            self.stdout.write(f"{cls.event_label} -> {cls.template_name!r}")
            self.stdout.write("-" * 68)
            self._subcommand(cls).run_steps(tenant, creator, apply)
        self.stdout.write("\n" + "-" * 68)
        self._augment_retail(tenant, creator, apply)
        self._set_picker(tenant, apply)

    def handle(self, *args, **opts):
        from django.conf import settings

        apply = bool(opts["apply"])
        tenant = self._resolve_tenant(opts["tenant"])
        creator = self._resolve_creator()
        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "")
            or "https://client.igniteproductions.co"
        ).rstrip("/")
        code = (tenant.checkin_code or "").strip()

        self.stdout.write("=" * 68)
        self.stdout.write(f"Tenant     : [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")
        self.stdout.write(f"Walk-up    : {base}/checkin/{code or '(none)'} (never reminted)")
        if tenant.checkin_recap_code:
            self.stdout.write(
                f"Agency     : {base}/checkin/{tenant.checkin_recap_code} "
                "(no picker — Retail Sampling only)"
            )
        self.stdout.write(f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")
        self.stdout.write("=" * 68)

        if apply:
            with transaction.atomic():
                self._run_all(tenant, creator, True)
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nAPPLIED — {base}/checkin/{code} offers Event / Guerilla / "
                    "Seeding / Retail."
                )
            )
        else:
            self._run_all(tenant, creator, False)
            self.stdout.write(self.style.WARNING("\nDRY-RUN — nothing written. Re-run with --apply."))
