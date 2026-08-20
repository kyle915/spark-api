"""Set up Sipli: tenant + Event/Retail recap templates + standing check-in.

The Sipli twin of ``setup_g7_entertainment_checkin`` — same createTenant-style
seed plus a standing ``SIP-`` check-in code — with Liquid Death's two-program
picker because BAs run **Event Activation** and **Retail Sampling** off one
crew and one URL.

1. The **Event Sampling Recap** custom template, field-for-field off the
   client's own "Sipli // Event Sampling Recap" PDF (Maria Vorheier, #13,
   08/17/2026). Date of Sampling and Sampling Location are NOT template
   fields: clock-in on the standing link already captures where/when the BA
   started. Event / Sampling Location Name stays — the activation name is
   not the GPS address.

2. A **Retail Sampling** twin of that template that ALSO asks Store Number,
   Total Inventory Before Demo, and Total Inventory After Demo. Those three
   are required on Retail and absent on Event, so Event recaps never require
   or submit stale inventory values.

3. The tenant's **standing check-in code** — one durable ``/checkin/<code>``
   link. Start your shift asks **Retail Sampling vs Event Activation**
   (required, same picker as Liquid Death), then clock in → file the matching
   recap → clock out. Several BAs at one address on one day land on the SAME
   event *per program*.

Photos are labelled **buckets** (Consumer Sampling Pictures, Activation Set
Up, Expense Receipts) rather than template image fields. Two image fields on
one template would share a single photo grid on the walk-up page; buckets
keep those dropzones apart, each with library + camera multi-upload.

Recaps stay human-reviewed (not Feel Free auto-approve). Location mode is
Event-style address/GPS find-or-create, same as G7/KKC — the PDF is an Event
Sampling Recap, and retail still happens at a typed or GPS address, not a
Feel Free market day.

DRY-RUN by default. Run via ``/internal/cron/setup-sipli-checkin`` (or the
"Setup Sipli check-in" GitHub Action) so it executes against prod.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

from recaps.management.commands.setup_total_wireless_checkin import (
    _match_field_type,
)

TENANT_NAME = "Sipli"
TENANT_SLUG = "sipli"
CODE_PREFIX = "SIP-"
EVENT_PROGRAM = "Event Activation"
RETAIL_PROGRAM = "Retail Sampling"
EVENT_TEMPLATE_NAME = "Sipli · Event Sampling Recap"
RETAIL_TEMPLATE_NAME = "Sipli · Retail Sampling Recap"

PRODUCT_OPTIONS = [
    "100% Apple Juice",
    "100% Grape Juice",
    "100% Cranberry Juice",
]

# Taken field-for-field from the Event Sampling Recap PDF. Date of Sampling
# and Sampling Location belong to the event the check-in resolves. Photo
# slots are buckets, not template image fields — see PHOTO_BUCKETS.
EVENT_SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    (
        "Event Details",
        [
            ("Event / Sampling Location Name", "text", True, []),
        ],
    ),
    (
        "Products",
        [
            (
                "Which products were sampled?",
                "multiselect",
                True,
                list(PRODUCT_OPTIONS),
            ),
            ("How many Apple bottles were used for sampling?", "number", True, []),
            (
                "How many Cranberry bottles were used for sampling?",
                "number",
                True,
                [],
            ),
            ("How many Grape bottles were used for sampling?", "number", True, []),
        ],
    ),
    (
        "Sampling Counts",
        [
            (
                "Number of coupons given out (BOGO coupons and free bottle coupons)",
                "number",
                True,
                [],
            ),
            ("Number of unique people served", "number", True, []),
            (
                "Number of samples served even if someone receives two or more",
                "number",
                True,
                [],
            ),
        ],
    ),
    (
        "Brand Awareness",
        [
            (
                "How many consumers that were engaged with knew about Sipli product/brand?",
                "number",
                True,
                [],
            ),
            (
                "How many consumers would be willing to purchase the product after tasting it?",
                "number",
                True,
                [],
            ),
            (
                "How many consumers have tried Sipli flavors before?",
                "number",
                True,
                [],
            ),
        ],
    ),
    (
        "Feedback",
        [
            ("Demographics", "longtext", True, []),
            (
                "What were the top 5 frequently asked questions you received from consumers?",
                "longtext",
                True,
                [],
            ),
            ("Helpful feedback", "longtext", False, []),
        ],
    ),
    (
        "Expenses",
        [
            (
                "Any expenses / bill-backs outside of product. (E.g. Tolls, Parking, etc).",
                "longtext",
                False,
                [],
            ),
        ],
    ),
]

# Retail-only. Required on the Retail template, omitted from Event so they
# are never asked, required, or submitted on an Event Activation recap.
RETAIL_EXTRA: tuple[str, list[tuple[str, str, bool, list[str]]]] = (
    "Retail Inventory",
    [
        ("Store Number", "text", True, []),
        ("Total Inventory Before Demo", "number", True, []),
        ("Total Inventory After Demo", "number", True, []),
    ],
)

RETAIL_SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    RETAIL_EXTRA,
    *EVENT_SPEC,
]

# Display order is tenant-wide (RecapSection is shared). Retail Inventory
# sits first so a retail BA sees Store Number / inventory before the PDF
# questions. Event recaps skip that section because those fields are not
# on the Event template.
SECTION_ORDER = {
    "Retail Inventory": 0,
    "Event Details": 1,
    "Products": 2,
    "Sampling Counts": 3,
    "Brand Awareness": 4,
    "Feedback": 5,
    "Expenses": 6,
}

# Labelled dropzones on the walk-up recap. Same shots for both programs —
# the PDF is one form; retail extras are the three inventory fields, not
# extra photos. Keyed by event type so serialize_photo_buckets stays on
# the per-program path.
PHOTO_BUCKETS: list[dict] = [
    {"name": "Consumer Sampling Pictures"},
    {"name": "Activation Set Up"},
    {"name": "Expense Receipts"},
]

PHOTO_BUCKETS_BY_PROGRAM: dict[str, list[dict]] = {
    EVENT_PROGRAM: list(PHOTO_BUCKETS),
    RETAIL_PROGRAM: list(PHOTO_BUCKETS),
}

KEEP_PROGRAMS = {EVENT_PROGRAM.lower(), RETAIL_PROGRAM.lower()}


class Command(BaseCommand):
    help = (
        "Set up Sipli: tenant (if missing), Event + Retail recap templates, "
        "and standing check-in link (dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="sipli",
            help=(
                "tenant name/slug substring (case-insensitive). "
                "Default: 'sipli'."
            ),
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'SIP' -> "
                f"SIP-XXXXXX. Blank keeps {CODE_PREFIX!r}."
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
        tenant = self._resolve_or_create_tenant(opts["tenant"], creator, apply)

        self.stdout.write("=" * 68)
        if tenant is None:
            self.stdout.write(
                f"Tenant     : (would create {TENANT_NAME!r} / {TENANT_SLUG!r})"
            )
        else:
            self.stdout.write(
                f"Tenant     : [{tenant.id}] {tenant.name!r} "
                f"(slug {tenant.slug!r})"
            )
        self.stdout.write(f"Event tpl  : {EVENT_TEMPLATE_NAME!r}")
        self.stdout.write(f"Retail tpl : {RETAIL_TEMPLATE_NAME!r}")
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        if tenant is None:
            self._print_spec()
            self.stdout.write("\n" + "=" * 68)
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would create tenant {TENANT_NAME!r}, "
                    f"Event + Retail templates, photo buckets, and a "
                    f"{CODE_PREFIX} standing link. Re-run with --apply to write."
                )
            )
            return

        event_type = self._ensure_program(
            tenant, EVENT_PROGRAM, "event-activation", creator, apply
        )
        retail_type = self._ensure_program(
            tenant, RETAIL_PROGRAM, "retail-sampling", creator, apply
        )
        self.stdout.write(
            f"Event type : {getattr(event_type, 'name', None)!r} "
            f"(id {getattr(event_type, 'id', None)})"
        )
        self.stdout.write(
            f"Retail type: {getattr(retail_type, 'name', None)!r} "
            f"(id {getattr(retail_type, 'id', None)})"
        )
        if event_type is None or retail_type is None:
            if not apply:
                self._print_spec()
                return
            raise CommandError(
                f"Tenant {tenant.slug!r} is missing Event Activation or "
                "Retail Sampling — seed failed."
            )

        self._report_existing_templates(tenant)

        ft_cache: dict = {}
        for spec in (EVENT_SPEC, RETAIL_SPEC):
            for _, fields in spec:
                for _, kind, _, _ in fields:
                    self._resolve_field_type(kind, creator, apply, ft_cache)

        if apply:
            with transaction.atomic():
                self._upsert_template(
                    tenant,
                    EVENT_TEMPLATE_NAME,
                    event_type,
                    EVENT_SPEC,
                    creator,
                    apply,
                    ft_cache,
                )
                self._upsert_template(
                    tenant,
                    RETAIL_TEMPLATE_NAME,
                    retail_type,
                    RETAIL_SPEC,
                    creator,
                    apply,
                    ft_cache,
                )
        else:
            self._upsert_template(
                tenant,
                EVENT_TEMPLATE_NAME,
                event_type,
                EVENT_SPEC,
                creator,
                apply,
                ft_cache,
            )
            self._upsert_template(
                tenant,
                RETAIL_TEMPLATE_NAME,
                retail_type,
                RETAIL_SPEC,
                creator,
                apply,
                ft_cache,
            )

        self._photo_buckets(tenant, creator, apply)
        self._pin_programs(tenant, event_type, retail_type, apply)
        self._retire_other_programs(tenant, event_type, retail_type, apply)
        # Retiring "Event" can repoint a leftover template onto Event
        # Activation. resolve_template_for_event picks the lowest id, so
        # that leftover would win over the PDF form until we fold it.
        self._fold_extra_templates(
            tenant, event_type, retail_type, creator, apply, ft_cache
        )
        self._location_mode(tenant, apply)
        self._checkin_code(tenant, apply, opts.get("prefix"))

    def _print_spec(self) -> None:
        self.stdout.write("\nEVENT ACTIVATION (PDF fields):")
        for s_idx, (section_name, fields) in enumerate(EVENT_SPEC):
            self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
            for fname, kind, required, _ in fields:
                req = "REQUIRED" if required else "optional"
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}")
        self.stdout.write("\nRETAIL SAMPLING adds:")
        for fname, kind, required, _ in RETAIL_EXTRA[1]:
            req = "REQUIRED" if required else "optional"
            self.stdout.write(f"    - {fname!r}  [{kind}] {req}")
        self.stdout.write("\nPhoto buckets:")
        for bucket in PHOTO_BUCKETS:
            self.stdout.write(f"    - {bucket['name']!r}")

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

    def _resolve_or_create_tenant(self, needle: str, creator, apply: bool):
        from tenants.models import Tenant

        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            for t in matches:
                self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
            raise CommandError(
                f"{needle!r} matched {len(matches)} tenants — narrow --tenant."
            )

        self.stdout.write(
            self.style.WARNING(
                f"No tenant matches {needle!r} — "
                f"{'creating' if apply else 'would create'} "
                f"{TENANT_NAME!r} (slug {TENANT_SLUG!r})."
            )
        )
        if not apply:
            return None
        return self._create_tenant(creator)

    def _create_tenant(self, creator):
        """Create the tenant the same way ``createTenant`` seeds a new brand.

        Girl Beer's side-path onboard skipped FileRecapCategory rows and
        leaked receipts into another tenant. This copies the mutation's
        default lists so the standing link has statuses, event types, and
        photo categories of its own.
        """
        from ambassadors.models import AttendanceStatus, Skill
        from events.models import EventStatus, EventType, RequestStatus, RequestType
        from jobs.models import RateType
        from jobs.models import Status as JobStatus
        from recaps.models import FileRecapCategory, TypeOfGood
        from tenants.models import Tenant
        from tenants.mutations import (
            DEFAULT_ATTENDANCE_STATUS_TEMPLATES,
            DEFAULT_EVENT_TYPES,
            DEFAULT_FILE_RECAP_CATEGORIES,
            DEFAULT_JOB_STATUS_TEMPLATES,
            DEFAULT_RATE_TYPES,
            DEFAULT_REQUEST_TYPES,
            DEFAULT_SKILLS,
            DEFAULT_STATUS_TEMPLATES,
            DEFAULT_TYPES_OF_GOOD,
        )

        tenant = Tenant.objects.create(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            request_url_name=TENANT_SLUG,
            created_by=creator,
        )

        def _statuses(model_cls, templates, include_default: bool):
            for status in templates:
                payload = {
                    "name": status["name"],
                    "slug": status.get("slug") or slugify(status["name"]),
                    "tenant": tenant,
                    "created_by": creator,
                }
                if include_default:
                    payload["is_default"] = status["is_default"]
                model_cls.objects.create(**payload)

        _statuses(RequestStatus, DEFAULT_STATUS_TEMPLATES, True)
        _statuses(EventStatus, DEFAULT_STATUS_TEMPLATES, True)
        _statuses(JobStatus, DEFAULT_JOB_STATUS_TEMPLATES, False)
        _statuses(AttendanceStatus, DEFAULT_ATTENDANCE_STATUS_TEMPLATES, False)

        for event_type in DEFAULT_EVENT_TYPES:
            EventType.objects.create(
                name=event_type["name"],
                slug=event_type.get("slug") or slugify(event_type["name"]),
                tenant=tenant,
                created_by=creator,
                is_default=event_type["is_default"],
            )
        for request_type in DEFAULT_REQUEST_TYPES:
            RequestType.objects.create(
                name=request_type, tenant=tenant, created_by=creator
            )
        for rate_type in DEFAULT_RATE_TYPES:
            RateType.objects.create(
                name=rate_type, tenant=tenant, created_by=creator
            )
        for recap_category in DEFAULT_FILE_RECAP_CATEGORIES:
            FileRecapCategory.objects.create(
                name=recap_category, tenant_id=tenant.id, created_by=creator
            )
        for type_of_good in DEFAULT_TYPES_OF_GOOD:
            TypeOfGood.objects.create(
                name=type_of_good, tenant=tenant, created_by=creator
            )
        for skill in DEFAULT_SKILLS:
            if not Skill.objects.filter(name__iexact=skill).exists():
                Skill.objects.create(name=skill, created_by=creator)

        self.stdout.write(
            self.style.SUCCESS(
                f"  Created tenant [{tenant.id}] {tenant.name!r} "
                f"with createTenant-style seeds."
            )
        )
        return tenant

    def _ensure_program(self, tenant, name: str, slug: str, creator, apply: bool):
        from events.models import EventType
        from tenants.mutations import DEFAULT_EVENT_TYPES

        qs = EventType.objects.filter(tenant_id=tenant.id).order_by("id")
        if not qs.exists() and apply:
            for event_type in DEFAULT_EVENT_TYPES:
                EventType.objects.create(
                    name=event_type["name"],
                    slug=event_type.get("slug") or slugify(event_type["name"]),
                    tenant=tenant,
                    created_by=creator,
                    is_default=event_type["is_default"],
                )
            qs = EventType.objects.filter(tenant_id=tenant.id).order_by("id")

        match = qs.filter(name__iexact=name).first() or qs.filter(
            name__icontains=name
        ).first()
        if match:
            return match
        if not apply:
            self.stdout.write(
                self.style.WARNING(f"  DRY-RUN — would create event type {name!r}")
            )
            return None
        created = EventType.objects.create(
            name=name,
            slug=slug,
            tenant=tenant,
            created_by=creator,
            is_default=False,
        )
        self.stdout.write(
            self.style.SUCCESS(f"  Created event type {name!r} (id {created.id})")
        )
        return created

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

    def _report_existing_templates(self, tenant) -> None:
        from recaps.models import CustomField, CustomRecapTemplate

        rows = list(
            CustomRecapTemplate.objects.filter(tenant_id=tenant.id).order_by("id")
        )
        self.stdout.write("\nExisting templates on this tenant:")
        if not rows:
            self.stdout.write("  (none — these will be the first)")
            return
        wanted = {EVENT_TEMPLATE_NAME, RETAIL_TEMPLATE_NAME}
        for t in rows:
            n_fields = CustomField.objects.filter(custom_recap_template=t).count()
            same = " <-- SAME NAME, will be reused" if t.name in wanted else ""
            self.stdout.write(
                f"  [{t.id}] {t.name!r} — {n_fields} field(s), "
                f"event_type={getattr(t.event_type, 'name', None)!r}{same}"
            )

    def _upsert_template(
        self,
        tenant,
        template_name: str,
        event_type,
        spec,
        creator,
        apply: bool,
        ft_cache: dict,
    ) -> None:
        from recaps.models import CustomField, CustomRecapTemplate, RecapSection

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
            if template.event_type_id != event_type.id:
                template.event_type = event_type
                template.save(update_fields=["event_type"])
                self.stdout.write(
                    f"  Re-pointed {template_name!r} event_type → "
                    f"{event_type.name!r}"
                )
            self.stdout.write(
                f"\nTemplate {'CREATED' if made else 'exists'} "
                f"{template_name!r} (id {template.id}, uuid {template.uuid}, "
                f"event_type={event_type.name!r})"
            )
        else:
            self.stdout.write(f"\nTemplate {template_name!r} ({event_type.name})")

        for section_name, fields in spec:
            s_idx = SECTION_ORDER[section_name]
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
                extra = f" options={options!r}" if options else ""
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}{extra}")
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
                    continue
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
                if list(field.options or []) != list(options):
                    field.options = list(options)
                    changed.append("options")
                if changed:
                    changed.append("updated_at")
                    field.save(update_fields=changed)
                    updated["fields"] += 1

        self.stdout.write("\n" + "=" * 68)
        if apply:
            self.stdout.write(
                self.style.SUCCESS(
                    f"APPLIED — {template_name!r} {template.uuid} · "
                    f"sections +{created['sections']}/~{updated['sections']} · "
                    f"fields +{created['fields']}/~{updated['fields']}."
                )
            )
        else:
            total_fields = sum(len(f) for _, f in spec)
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would create/reconcile {len(spec)} sections + "
                    f"{total_fields} fields on {template_name!r}."
                )
            )

    def _photo_buckets(self, tenant, creator, apply: bool) -> None:
        from recaps.models import FileRecapCategory

        self.stdout.write("\nPhoto buckets:")
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

        wanted = PHOTO_BUCKETS_BY_PROGRAM
        current = getattr(tenant, "checkin_photo_buckets", None)
        if current == wanted:
            self.stdout.write("  checkin_photo_buckets already set — left as-is.")
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING("  DRY-RUN — would set checkin_photo_buckets")
            )
            return
        tenant.checkin_photo_buckets = wanted
        tenant.save(update_fields=["checkin_photo_buckets"])
        self.stdout.write(self.style.SUCCESS("  checkin_photo_buckets set."))

    def _pin_programs(self, tenant, event_type, retail_type, apply: bool) -> None:
        """Default Event Activation; BA picks Retail vs Event on the standing link."""
        from events.models import EventType

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
        selectable = [event_type, retail_type]
        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    "  DRY-RUN — would make selectable: "
                    + ", ".join(t.name for t in selectable)
                )
            )
            return
        tenant.checkin_event_types.set(selectable)
        if not event_type.is_default:
            EventType.objects.filter(tenant_id=tenant.id, is_default=True).exclude(
                pk=event_type.pk
            ).update(is_default=False)
            event_type.is_default = True
            event_type.save(update_fields=["is_default"])
        self.stdout.write(
            "  selectable = "
            + ", ".join(f"[{t.id}] {t.name}" for t in selectable)
        )

    def _retire_other_programs(
        self, tenant, event_type, retail_type, apply: bool
    ) -> None:
        """Keep Event Activation + Retail Sampling. Retire On-Premise / Event.

        createTenant seeds Retail Sampling / On-Premise Sampling / Event.
        Event Activation is created by this command. The extras stay in the
        catalog until we repoint events onto Event Activation and delete
        them. RESTRICT FKs block a plain delete.
        """
        from events.models import Event, EventType
        from recaps.models import CustomRecapTemplate

        keep_ids = {event_type.id, retail_type.id}
        others = list(
            EventType.objects.filter(tenant_id=tenant.id)
            .exclude(pk__in=keep_ids)
            .order_by("id")
        )
        if not others:
            self.stdout.write(
                "\nNo other event types — Event Activation and Retail "
                "Sampling are the only programs."
            )
            return
        self.stdout.write("\nOther event types (will not be selectable):")
        for extra in others:
            n_events = Event.objects.filter(event_type=extra).count()
            n_tpl = CustomRecapTemplate.objects.filter(event_type=extra).count()
            refs = []
            if n_events:
                refs.append(f"{n_events} event(s)→{event_type.name}")
            if n_tpl:
                refs.append(f"{n_tpl} template(s)→{event_type.name}")
            ref_txt = f"  [{', '.join(refs)}]" if refs else "  [unused]"
            if extra.name.lower() in KEEP_PROGRAMS:
                self.stdout.write(f"    keeping {extra.name!r}{ref_txt}")
                continue
            if not apply:
                self.stdout.write(
                    self.style.WARNING(f"    would retire {extra.name!r}{ref_txt}")
                )
                continue
            Event.objects.filter(event_type=extra).update(event_type=event_type)
            CustomRecapTemplate.objects.filter(event_type=extra).update(
                event_type=event_type
            )
            extra.delete()
            self.stdout.write(
                self.style.SUCCESS(f"    retired {extra.name!r}{ref_txt}")
            )

    def _delete_template(self, template) -> None:
        from recaps.models import CustomField

        CustomField.objects.filter(custom_recap_template=template).delete()
        template.delete()

    def _fold_extra_templates(
        self, tenant, event_type, retail_type, creator, apply: bool, ft_cache: dict
    ) -> None:
        """Leave one named template per program so walk-up hits the PDF form.

        ``resolve_template_for_event`` does
        ``filter(event_type_id=...).order_by("id").first()``. A leftover
        like ``Sipli - Event Activation Recap`` that got repointed off
        retired Event wins over ``Sipli · Event Sampling Recap`` because
        it has the lower id. Unused leftovers are deleted. Leftovers with
        recaps keep their rows: drop the empty named clone, rename the
        leftover, then upsert the PDF fields onto it.
        """
        from recaps.models import CustomRecap, CustomRecapTemplate

        pairs = [
            (event_type, EVENT_TEMPLATE_NAME, EVENT_SPEC),
            (retail_type, RETAIL_TEMPLATE_NAME, RETAIL_SPEC),
        ]
        reupsert: list = []
        for et, keep_name, spec in pairs:
            tpls = list(
                CustomRecapTemplate.objects.filter(
                    tenant_id=tenant.id, event_type=et
                ).order_by("id")
            )
            keepers = [t for t in tpls if t.name == keep_name]
            extras = [t for t in tpls if t.name != keep_name]
            if not extras:
                continue
            self.stdout.write(f"\nExtra templates on {et.name!r}:")
            for extra in extras:
                n = CustomRecap.objects.filter(
                    custom_recap_template=extra
                ).count()
                keeper = keepers[0] if keepers else None
                n_keep = (
                    CustomRecap.objects.filter(
                        custom_recap_template=keeper
                    ).count()
                    if keeper
                    else 0
                )
                if n == 0:
                    msg = (
                        f"    leftover {extra.name!r} (id {extra.id}, "
                        f"0 recaps)"
                    )
                    if not apply:
                        self.stdout.write(
                            self.style.WARNING(f"{msg} — would delete")
                        )
                        continue
                    self._delete_template(extra)
                    self.stdout.write(self.style.SUCCESS(f"{msg} — deleted"))
                    continue
                if keeper and n_keep == 0:
                    msg = (
                        f"    leftover {extra.name!r} (id {extra.id}, "
                        f"{n} recap(s)); empty {keep_name!r} would lose "
                        f"walk-up (resolve_template order_by id)"
                    )
                    if not apply:
                        self.stdout.write(
                            self.style.WARNING(
                                f"{msg} — would drop empty {keep_name!r} "
                                "and rename leftover, then upsert PDF fields"
                            )
                        )
                        continue
                    self._delete_template(keeper)
                    extra.name = keep_name
                    extra.save(update_fields=["name"])
                    keepers = [extra]
                    reupsert.append((keep_name, et, spec))
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"{msg} — renamed leftover to {keep_name!r}"
                        )
                    )
                    continue
                self.stdout.write(
                    self.style.WARNING(
                        f"    leftover {extra.name!r} (id {extra.id}, "
                        f"{n} recap(s)) AND {keep_name!r} also has recaps "
                        "— left both"
                    )
                )
        if apply:
            for keep_name, et, spec in reupsert:
                self._upsert_template(
                    tenant, keep_name, et, spec, creator, apply, ft_cache
                )

    def _location_mode(self, tenant, apply: bool) -> None:
        from tenants.models import Tenant

        wanted = Tenant.CHECKIN_LOCATION_ADDRESS
        current = tenant.checkin_location_mode
        self.stdout.write(f"\nLocation mode: {current!r}")
        if current == wanted:
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING(f"DRY-RUN — would set location mode to {wanted!r}")
            )
            return
        tenant.checkin_location_mode = wanted
        tenant.save(update_fields=["checkin_location_mode"])
        self.stdout.write(self.style.SUCCESS(f"Location mode set to {wanted!r}"))

    def _checkin_code(self, tenant, apply: bool, prefix: str = "") -> None:
        from tenants.models import Tenant

        raw = (prefix or "").strip().upper().rstrip("-")
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if raw and not 1 <= len(cleaned) <= 4:
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. SIP.")
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
