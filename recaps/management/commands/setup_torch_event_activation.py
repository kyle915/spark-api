"""Torch THC: add an Event Activation program to the BA walk-up.

Adds **Event Activation** next to Retail Sampling on the existing standing
check-in ``/checkin/TH-2HRV3D`` (code never reminted) and builds its recap
form, ``Torch THC-Event Activation``. Structure mirrors the Liquid Death /
Mark Anthony Brands Event Activation recaps, trimmed to sampling only: Torch
does not sell at events, so there are no purchase, units-sold, spend, or
can-purchase fields. Products Sampled resolves from the live Torch Product
catalog (per-SKU sample counts on the walk-up), never a hardcoded SKU list.

Photo dropzones are the LD/MAB activation buckets (Activation Set Up,
Consumer Sampling Pictures, Expense Receipts (Parking)), keyed to the Event
Activation program so the Retail Sampling buckets are untouched.

Leaves alone: the Retail Sampling template, the pinned default program, the
3rd-party agency twin ``TH-AGENCY`` (stays store demos — the agency link
never offers the program picker). Template sections are dedicated rows of
this template, so reordering them never moves a Retail Sampling section.

Event Activation is excluded from Retail + On-Premise conversion by its
program name; recaps still land as ``approved=False`` (Needs review).

Idempotent. DRY-RUN by default; ``--apply`` writes. Run on prod via
``/internal/cron/setup-torch-event-activation`` (or the "Setup Torch Event
Activation" GitHub Action).
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

TENANT_SLUG = "torch-thc"
TENANT_FORM_SLUG = "keee-torch-thc"
CHECKIN_CODE = "TH-2HRV3D"

EVENT_LABEL = "Event Activation"
TEMPLATE_NAME = "Torch THC-Event Activation"

CONSUMER_SAMPLING: dict = {
    "name": "Consumer Sampling Pictures",
    "helper": "please try to upload 8+",
    "min": 8,
}

# LD / MAB Event Activation dropzones.
ACTIVATION_BUCKETS: list[dict] = [
    {
        "name": "Activation Set Up",
        "helper": "Booth / table, signage, and the full footprint",
    },
    CONSUMER_SAMPLING,
    {"name": "Expense Receipts (Parking)"},
]

SENTINEL_CATEGORY_NAMES = ("Sampling photos", "Receipts")

EVENT_KIND_OPTS = [
    "Music Festival / Concert",
    "Sporting Event",
    "Street Fair / Community Event",
    "Brewery / Taproom Pop-Up",
    "Private / Corporate Event",
    "Other",
]
TRAFFIC_OPTS = ["High", "Medium", "Low"]
SERVED_OPTS = ["Chilled cans", "Poured sample cups", "Both"]

COMPETITOR_FIELD = (
    "Ask consumers what competitor products they drink. "
    "And if so, which one. Why do you like it?"
)

PRODUCTS_SAMPLED = "Products Sampled"

# (name, kind, required, options, placeholder)
Field = tuple[str, str, bool, list[str], str]

SPEC_FIELDS: list[tuple[str, list[Field]]] = [
    (
        "Event Details",
        [
            ("What kind of event was it?", "select", True, list(EVENT_KIND_OPTS), ""),
            (
                "Total Estimated Attendance",
                "number",
                True,
                [],
                "Best guess for the whole event, e.g. 2500",
            ),
            (
                "Describe the traffic at your booth",
                "select",
                True,
                list(TRAFFIC_OPTS),
                "",
            ),
            ("How were samples served?", "select", True, list(SERVED_OPTS), ""),
        ],
    ),
    (
        "Consumer Engagement",
        [
            (
                "How many TOTAL consumers did you sample?",
                "number",
                True,
                [],
                "People who actually tasted Torch (21+ only)",
            ),
            (
                "People engaged",
                "number",
                True,
                [],
                "People you talked with about Torch, tasted or not",
            ),
            (
                "How many consumers were trying Torch for the first time?",
                "number",
                True,
                [],
                "",
            ),
            (
                "How many consumers that were engaged with knew about "
                "Torch THC product/brand?",
                "number",
                True,
                [],
                "",
            ),
            (
                "How many consumers would be willing to purchase Torch at a "
                "store after tasting it?",
                "number",
                True,
                [],
                "",
            ),
        ],
    ),
    (
        "Feedback & Account Notes",
        [
            (
                "Demographics (general age, sex, ethnicities of consumers)",
                "longtext",
                True,
                [],
                "e.g. Mostly 25-40, even male/female split, college crowd after 6pm",
            ),
            (
                "Consumer Feedback",
                "longtext",
                True,
                [],
                "Which flavors and doses landed, and what people said about taste and feel",
            ),
            (
                "Quotes from Consumers",
                "longtext",
                True,
                [],
                '"This tastes like a real seltzer." - female, ~30',
            ),
            (
                "What were the top 5 frequently asked questions you received "
                "from consumers?",
                "longtext",
                True,
                [],
                "e.g. How strong is 5mg? Where can I buy it? Is it legal here?",
            ),
            (COMPETITOR_FIELD, "longtext", False, [], ""),
            ("Positive stories from the event", "longtext", False, [], ""),
            (
                "Event organizer / venue feedback",
                "longtext",
                False,
                [],
                "Anything the organizer or venue staff said about Torch or the booth",
            ),
            ("Anything you'd change or do differently?", "longtext", False, [], ""),
        ],
    ),
]

SECTION_ORDER = {
    "Event Details": 0,
    "Consumer Engagement": 1,
    "Feedback & Account Notes": 2,
    PRODUCTS_SAMPLED: 3,
}

# Purchase / sales vocabulary that must never appear on a sampling-only form.
SALES_FIELD_RE = re.compile(
    r"\b(sold|bought|spend|spent)\b|did consumers purchase|cans? (were )?purchased",
    re.I,
)


def build_spec(product_opts: list[str]) -> list[tuple[str, list[Field]]]:
    """SPEC with catalog-backed Products Sampled options."""
    sections = [(name, list(fields)) for name, fields in SPEC_FIELDS]
    sections.append(
        (
            PRODUCTS_SAMPLED,
            [
                (
                    PRODUCTS_SAMPLED,
                    "multiselect",
                    True,
                    list(product_opts),
                    "Pick every SKU you poured, then enter samples given for each",
                )
            ],
        )
    )
    return sections


SPEC = build_spec([])


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _match_field_type(kind: str, name_lower: str) -> bool:
    """Same fuzzy rules as the FE ``customFieldKind`` (see seed_neutonic)."""
    if kind == "multiselect":
        return name_lower == "multiselect" or "multi" in name_lower
    if kind == "select":
        return name_lower == "select" or "dropdown" in name_lower
    if kind == "number":
        return name_lower == "number" or "num" in name_lower or "integer" in name_lower
    if kind == "longtext":
        return "long" in name_lower or "textarea" in name_lower or "paragraph" in name_lower
    if kind == "text":
        return name_lower == "text"
    return False


class Command(BaseCommand):
    help = (
        "Torch THC: add Event Activation to the TH-2HRV3D walk-up and seed its "
        "sampling-only recap template (dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default=TENANT_SLUG,
            help="exact Tenant.slug, else exact request_url_name (default torch-thc).",
        )
        parser.add_argument("--apply", action="store_true")

    # ── resolution ────────────────────────────────────────────────────────

    def _resolve_tenant(self, needle: str):
        from tenants.models import Tenant

        needle = (needle or TENANT_SLUG).strip()
        for lookup in ("slug__iexact", "request_url_name__iexact"):
            matches = list(Tenant.objects.filter(**{lookup: needle}).order_by("id"))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                ids = ", ".join(str(t.id) for t in matches)
                raise CommandError(f"{len(matches)} tenants match {needle!r} ({ids}).")
        if needle == TENANT_SLUG:
            return self._resolve_tenant(TENANT_FORM_SLUG)
        raise CommandError(f"tenant-not-found: {needle}")

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

    # ── walk-up program + photo buckets ───────────────────────────────────

    def _ensure_event_type(self, tenant, creator, apply: bool):
        from events.models import EventType

        existing = EventType.objects.filter(
            tenant_id=tenant.id, name__iexact=EVENT_LABEL
        ).first()
        if existing:
            self.stdout.write(f"Event type : [{existing.id}] {existing.name!r} (exists)")
            return existing
        if not apply:
            self.stdout.write(f"Event type : would create {EVENT_LABEL!r}")
            return None
        et = EventType.objects.create(name=EVENT_LABEL, tenant=tenant, created_by=creator)
        self.stdout.write(f"Event type : + {EVENT_LABEL!r} [{et.id}]")
        return et

    def _ensure_categories(self, tenant, creator, apply: bool) -> None:
        from recaps.models import FileRecapCategory

        by_norm: dict[str, object] = {}
        for cat in FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id"):
            by_norm.setdefault(_norm(cat.name), cat)

        self.stdout.write("\nPhoto categories (Event Activation):")
        for spec in ACTIVATION_BUCKETS:
            name = spec["name"]
            match = by_norm.get(_norm(name))
            if match is not None:
                self.stdout.write(f"  = {name!r} [{match.id}]")
                continue
            self.stdout.write(f"  + {name!r} — {'created' if apply else 'would create'}")
            if apply:
                cat = FileRecapCategory.objects.create(
                    name=name, tenant_id=tenant.id, created_by=creator
                )
                by_norm[_norm(name)] = cat

    def _merge_photo_buckets(self, tenant, apply: bool) -> dict:
        """Key buckets by program; keep every existing list byte-for-byte."""
        current = getattr(tenant, "checkin_photo_buckets", None)
        merged: dict = {}
        if isinstance(current, dict):
            merged = {k: list(v) for k, v in current.items()}
        elif isinstance(current, list) and current:
            # A flat list served every program. Keep it as the fallback for
            # Retail Sampling (and anything else) via "default".
            merged["default"] = list(current)
            pin = getattr(tenant, "checkin_event_type", None)
            if pin is not None and not re.search(r"activation", pin.name or "", re.I):
                merged[pin.name] = list(current)

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
            names = " | ".join(str(b.get("name", "?")) for b in buckets if isinstance(b, dict))
            self.stdout.write(f"  {key!r}: {names}")
        if apply:
            tenant.checkin_photo_buckets = merged
            tenant.save(update_fields=["checkin_photo_buckets"])
        return merged

    def _add_to_picker(self, tenant, activation, apply: bool) -> None:
        offered = list(tenant.checkin_event_types.filter(tenant_id=tenant.id).order_by("id"))
        pin = getattr(tenant, "checkin_event_type", None)
        wanted = list(offered)
        # An empty picker means "just the pinned program"; keep that program
        # offered so adding Event Activation doesn't hide Retail Sampling.
        if not wanted and pin is not None:
            wanted.append(pin)
        if activation is not None and all(et.id != activation.id for et in wanted):
            wanted.append(activation)

        self.stdout.write("\nWalk-up picker:")
        for et in wanted:
            self.stdout.write(f"  [{et.id}] {et.name!r}")
        if activation is None:
            self.stdout.write(f"  [new] {EVENT_LABEL!r}")
        self.stdout.write(
            f"Pinned default : {pin.name!r} (unchanged)" if pin else "Pinned default : (none)"
        )
        if apply:
            tenant.checkin_event_types.set(wanted)

    # ── template ──────────────────────────────────────────────────────────

    def _product_options(self, tenant) -> list[str]:
        from recaps.products_sampled import products_sampled_options_for_tenant

        opts = products_sampled_options_for_tenant(tenant)
        self.stdout.write(
            f"\nProducts   : {len(opts)} SKUs from the Torch Product catalog"
            if opts
            else "\nProducts   : catalog empty — Products Sampled refreshes at render"
        )
        return opts

    def _field_type(self, kind: str, creator, apply: bool, cache: dict):
        if kind in cache:
            return cache[kind]
        from recaps.models import CustomRecapFieldType

        found = next(
            (
                ft
                for ft in CustomRecapFieldType.objects.order_by("id")
                if _match_field_type(kind, (ft.name or "").lower())
            ),
            None,
        )
        if found is None and apply:
            found = CustomRecapFieldType.objects.create(name=kind, created_by=creator)
        cache[kind] = found
        return found

    def _template_section(self, tenant, template, name: str, creator, apply: bool):
        """This template's own section row named ``name`` (created if missing).

        Never reuses a section another template's fields sit in, so ordering
        here can't reshuffle the Retail Sampling form.
        """
        from recaps.models import CustomField, RecapSection

        order = SECTION_ORDER.get(name, 99)
        if template is not None:
            sec_id = (
                CustomField.objects.filter(
                    custom_recap_template=template, recap_section__name=name
                )
                .values_list("recap_section_id", flat=True)
                .first()
            )
            if sec_id:
                section = RecapSection.objects.get(id=sec_id)
                if apply and section.order != order:
                    section.order = order
                    section.save(update_fields=["order", "updated_at"])
                return section
        if not apply:
            return None
        return RecapSection.objects.create(
            tenant_id=tenant.id, name=name, order=order, created_by=creator
        )

    def _seed_template(self, tenant, event_type, creator, apply: bool) -> None:
        from recaps.models import CustomField, CustomFieldValue, CustomRecapTemplate

        spec = build_spec(self._product_options(tenant))
        template = CustomRecapTemplate.objects.filter(
            tenant_id=tenant.id, name=TEMPLATE_NAME
        ).first()
        if event_type is not None:
            # The walk-up serves the lowest-id template on this event type.
            rivals = CustomRecapTemplate.objects.filter(
                tenant_id=tenant.id, event_type=event_type
            ).exclude(name=TEMPLATE_NAME)
            if template is not None:
                rivals = rivals.filter(id__lt=template.id)
            rivals = list(rivals.order_by("id"))
            for rival in rivals:
                self.stdout.write(
                    self.style.WARNING(
                        f"  ! [{rival.id}] {rival.name!r} already sits on {EVENT_LABEL!r} "
                        "and would win the walk-up"
                    )
                )
            if rivals and apply:
                raise CommandError(
                    f"{len(rivals)} other template(s) on {EVENT_LABEL!r} — "
                    "move them to another event type first."
                )
        if template is None and apply:
            template = CustomRecapTemplate.objects.create(
                tenant_id=tenant.id,
                name=TEMPLATE_NAME,
                event_type=event_type,
                product_samples=True,
                sales_performance=False,
                layout={},
                created_by=creator,
            )
            self.stdout.write(f"\nTemplate   : + {TEMPLATE_NAME!r} [{template.id}]")
        elif template is not None:
            self.stdout.write(f"\nTemplate   : {TEMPLATE_NAME!r} [{template.id}] (exists)")
            if apply:
                changed = []
                if template.event_type_id != event_type.id:
                    template.event_type = event_type
                    changed.append("event_type")
                if template.product_samples is not True:
                    template.product_samples = True
                    changed.append("product_samples")
                if template.sales_performance:
                    template.sales_performance = False
                    changed.append("sales_performance")
                if changed:
                    template.save(update_fields=[*changed, "updated_at"])
        else:
            self.stdout.write(f"\nTemplate   : would create {TEMPLATE_NAME!r}")

        ft_cache: dict = {}
        keep: set[str] = set()
        stats = {"created": 0, "updated": 0}
        for section_name, fields in spec:
            section = self._template_section(tenant, template, section_name, creator, apply)
            self.stdout.write(f"\n  [{SECTION_ORDER[section_name]}] {section_name}")
            for idx, (name, kind, required, options, placeholder) in enumerate(fields):
                keep.add(name)
                req = "REQUIRED" if required else "optional"
                extra = ""
                if kind in ("select", "multiselect"):
                    extra = f" ({len(options)} options)" if options else " (catalog at render)"
                self.stdout.write(f"    - {name}  [{kind}] {req}{extra}")
                if not apply:
                    continue
                ft = self._field_type(kind, creator, apply, ft_cache)
                field = CustomField.objects.filter(
                    custom_recap_template=template, name=name
                ).first()
                if field is None:
                    CustomField.objects.create(
                        custom_recap_template=template,
                        recap_section=section,
                        name=name,
                        custom_field_type=ft,
                        required=required,
                        options=list(options),
                        placeholder=placeholder,
                        order=idx,
                        created_by=creator,
                    )
                    stats["created"] += 1
                    continue
                want = {
                    "recap_section_id": section.id,
                    "custom_field_type_id": ft.id,
                    "required": required,
                    "options": list(options),
                    "placeholder": placeholder,
                    "order": idx,
                }
                changed = [k for k, v in want.items() if getattr(field, k) != v]
                if changed:
                    for k in changed:
                        setattr(field, k, want[k])
                    field.save(update_fields=[*changed, "updated_at"])
                    stats["updated"] += 1

        if template is not None:
            for field in CustomField.objects.filter(custom_recap_template=template):
                if field.name in keep:
                    continue
                if CustomFieldValue.objects.filter(custom_field=field).exists():
                    self.stdout.write(f"  keep obsolete {field.name!r} — has answers")
                    continue
                self.stdout.write(
                    f"  - {'pruned' if apply else 'would prune'} {field.name!r}"
                )
                if apply:
                    field.delete()
        self.stdout.write(
            f"\nFields     : +{stats['created']} ~{stats['updated']} "
            f"({sum(len(f) for _, f in spec)} in spec)"
        )

    # ── entry ─────────────────────────────────────────────────────────────

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
        self.stdout.write(
            f"Walk-up    : {base}/checkin/{code} (kept; never reminted)"
            if code
            else "Walk-up    : (no checkin_code — set one up before BAs can use this)"
        )
        if code and code != CHECKIN_CODE:
            self.stdout.write(
                self.style.WARNING(f"  ! expected {CHECKIN_CODE!r} — leaving {code!r} alone")
            )
        self.stdout.write(f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")
        self.stdout.write("=" * 68)

        if apply:
            with transaction.atomic():
                activation = self._ensure_event_type(tenant, creator, True)
                self._ensure_categories(tenant, creator, True)
                self._merge_photo_buckets(tenant, True)
                self._add_to_picker(tenant, activation, True)
                self._seed_template(tenant, activation, creator, True)
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nAPPLIED — {base}/checkin/{code} offers {EVENT_LABEL} "
                    f"with {TEMPLATE_NAME!r}."
                )
            )
        else:
            activation = self._ensure_event_type(tenant, creator, False)
            self._ensure_categories(tenant, creator, False)
            self._merge_photo_buckets(tenant, False)
            self._add_to_picker(tenant, activation, False)
            self._seed_template(tenant, activation, creator, False)
            self.stdout.write(
                self.style.WARNING("\nDRY-RUN — nothing written. Re-run with --apply.")
            )
