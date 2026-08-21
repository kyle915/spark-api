"""Set up Breakaway: tenant + Jimmy Johns / Hiyo recap templates + standing check-in.

The Breakaway twin of ``setup_sipli_checkin`` — same createTenant-style seed
plus a standing ``BRK-`` check-in code — with the two-program picker used for
the festival's two brands: BAs run **Jimmy Johns** (Silent DJ chip sampling)
and **Hiyo** (exit sampling) off one crew and one URL.

1. The **Jimmy Johns** custom template, field-for-field off the client's own
   "Jimmy Johns // Breakaway Music Festival" recap PDF (Tishawna Banks, #8,
   05/31/2026). Date is NOT a template field: clock-in on the standing link
   already captures when the BA started. Festival Location stays — an open
   city/state text ("Columbus, OH"), not a locked venue list.

2. The **Hiyo** custom template, field-for-field off the "Hiyo // Breakaway
   Music Festival" recap PDF (Samantha Redmond, #4, 06/27/2026). Same Date /
   Festival Location treatment.

3. The tenant's **standing check-in code** — one durable ``/checkin/<code>``
   link. Start your shift asks **Jimmy Johns vs Hiyo** (required, same picker
   as Sipli/Liquid Death), then clock in → file the matching recap → clock
   out. Several BAs at one festival on one day land on the SAME event *per
   brand* (find-or-create keys on tenant + address + date + event type).

Photos are labelled **buckets** (each PDF's own photo label) rather than
template image fields, with library + camera multi-upload on the walk-up page.

Recaps stay human-reviewed (not Feel Free auto-approve). Location mode is
Event-style address/GPS find-or-create, same as Sipli/G7/KKC.

Breakaway already exists as a tenant with historical recaps, so unlike Sipli
this command does NOT retire other event types — it only adds the two brand
programs, pins them as the standing link's selectable set, and leaves the
tenant's existing catalog alone.

DRY-RUN by default. Run via ``/internal/cron/setup-breakaway-checkin`` (or the
"Setup Breakaway check-in" GitHub Action) so it executes against prod.
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

TENANT_NAME = "Breakaway"
TENANT_SLUG = "breakaway"
CODE_PREFIX = "BRK-"
JJ_PROGRAM = "Jimmy Johns"
HIYO_PROGRAM = "Hiyo"
JJ_TEMPLATE_NAME = "Breakaway · Jimmy Johns Recap"
HIYO_TEMPLATE_NAME = "Breakaway · Hiyo Recap"

# Taken field-for-field from the "Jimmy Johns // Breakaway Music Festival"
# PDF. Date belongs to the event the check-in resolves. Festival Location is
# the open city/state text Kyle asked for ("Columbus, OH"). Photo slots are
# buckets, not template image fields — see PHOTO_BUCKETS_BY_PROGRAM.
JJ_SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    (
        "Event Details",
        [
            ("Festival Location", "text", True, []),
        ],
    ),
    (
        "Consumer Engagement",
        [
            (
                "Total bags of chips set out today (estimate is fine)",
                "number",
                True,
                [],
            ),
            (
                "Total bags remaining at end of shift (estimate)",
                "number",
                True,
                [],
            ),
            ("Total number of chips distributed", "number", True, []),
            (
                "How many people did you personally interact with about the brand / chips / disco?",
                "number",
                True,
                [],
            ),
            (
                "During your shift, when were chips moving fastest?",
                "text",
                True,
                [],
            ),
            ("When were chips moving slowest?", "text", True, []),
        ],
    ),
    (
        "Feedback & Account Notes",
        [
            ("What Worked Well?", "longtext", True, []),
            ("What Could Be Improved?", "longtext", True, []),
            (
                "Anything that the client MUST know in the recap (wins, concerns, important learning)",
                "longtext",
                True,
                [],
            ),
        ],
    ),
]

# Taken field-for-field from the "Hiyo // Breakaway Music Festival" PDF.
HIYO_SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    (
        "Event Details",
        [
            ("Festival Location", "text", True, []),
        ],
    ),
    (
        "Sampling Counts",
        [
            ("Total samples distributed", "number", True, []),
            ("Estimated foot traffic / impressions", "number", True, []),
        ],
    ),
    (
        "Feedback",
        [
            (
                "Key highlight (1–2 sentences) / What worked well & What didn't?",
                "longtext",
                True,
                [],
            ),
            (
                "What were some consumer comments that you heard?",
                "longtext",
                True,
                [],
            ),
            (
                "What percent of consumer had heard of or tried Hiyo before?",
                "text",
                True,
                [],
            ),
        ],
    ),
]

# Display order is tenant-wide (RecapSection is shared). Event Details first
# so both brands open on Festival Location; the brand-specific sections
# follow. Jimmy Johns reuses the legacy "Consumer Engagement" /
# "Feedback & Account Notes" section names from the existing Breakaway
# template so historical and walk-up recaps read the same.
SECTION_ORDER = {
    "Event Details": 0,
    "Consumer Engagement": 1,
    "Feedback & Account Notes": 2,
    "Sampling Counts": 3,
    "Feedback": 4,
}

# Labelled dropzones on the walk-up recap, one per brand, named after each
# PDF's own photo label. Keyed by event type so serialize_photo_buckets stays
# on the per-program path.
PHOTO_BUCKETS_BY_PROGRAM: dict[str, list[dict]] = {
    JJ_PROGRAM: [{"name": "Activation / Sampling / Recap Photos"}],
    HIYO_PROGRAM: [{"name": "Consumer Sampling Pictures"}],
}

ALL_BUCKETS: list[dict] = [
    bucket
    for buckets in PHOTO_BUCKETS_BY_PROGRAM.values()
    for bucket in buckets
]


class Command(BaseCommand):
    help = (
        "Set up Breakaway: tenant (if missing), Jimmy Johns + Hiyo recap "
        "templates, and standing check-in link (dry-run by default; "
        "--apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="breakaway",
            help=(
                "tenant name/slug substring (case-insensitive). "
                "Default: 'breakaway'."
            ),
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'BRK' -> "
                f"BRK-XXXXXX. Blank keeps {CODE_PREFIX!r}."
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
        self.stdout.write(f"JJ tpl     : {JJ_TEMPLATE_NAME!r}")
        self.stdout.write(f"Hiyo tpl   : {HIYO_TEMPLATE_NAME!r}")
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
                    f"Jimmy Johns + Hiyo templates, photo buckets, and a "
                    f"{CODE_PREFIX} standing link. Re-run with --apply to write."
                )
            )
            return

        jj_type = self._ensure_program(tenant, JJ_PROGRAM, "jimmy-johns", creator, apply)
        hiyo_type = self._ensure_program(tenant, HIYO_PROGRAM, "hiyo", creator, apply)
        self.stdout.write(
            f"JJ type    : {getattr(jj_type, 'name', None)!r} "
            f"(id {getattr(jj_type, 'id', None)})"
        )
        self.stdout.write(
            f"Hiyo type  : {getattr(hiyo_type, 'name', None)!r} "
            f"(id {getattr(hiyo_type, 'id', None)})"
        )
        if jj_type is None or hiyo_type is None:
            if not apply:
                self._print_spec()
                return
            raise CommandError(
                f"Tenant {tenant.slug!r} is missing Jimmy Johns or Hiyo — "
                "seed failed."
            )

        self._report_existing_templates(tenant)

        ft_cache: dict = {}
        for spec in (JJ_SPEC, HIYO_SPEC):
            for _, fields in spec:
                for _, kind, _, _ in fields:
                    self._resolve_field_type(kind, creator, apply, ft_cache)

        if apply:
            with transaction.atomic():
                self._upsert_template(
                    tenant, JJ_TEMPLATE_NAME, jj_type, JJ_SPEC, creator, apply, ft_cache
                )
                self._upsert_template(
                    tenant,
                    HIYO_TEMPLATE_NAME,
                    hiyo_type,
                    HIYO_SPEC,
                    creator,
                    apply,
                    ft_cache,
                )
        else:
            self._upsert_template(
                tenant, JJ_TEMPLATE_NAME, jj_type, JJ_SPEC, creator, apply, ft_cache
            )
            self._upsert_template(
                tenant, HIYO_TEMPLATE_NAME, hiyo_type, HIYO_SPEC, creator, apply, ft_cache
            )

        self._photo_buckets(tenant, creator, apply)
        self._pin_programs(tenant, jj_type, hiyo_type, apply)
        # Retiring an event type can repoint a leftover template onto one of
        # the brand programs. resolve_template_for_event picks the lowest id,
        # so that leftover would win over the PDF form until we fold it. We
        # don't retire anything on this existing tenant, but the fold also
        # keeps re-runs idempotent.
        self._fold_extra_templates(tenant, jj_type, hiyo_type, creator, apply, ft_cache)
        self._location_mode(tenant, apply)
        self._checkin_code(tenant, apply, opts.get("prefix"))

    def _print_spec(self) -> None:
        self.stdout.write("\nJIMMY JOHNS (PDF fields):")
        for section_name, fields in JJ_SPEC:
            self.stdout.write(f"\n  SECTION {section_name!r}")
            for fname, kind, required, _ in fields:
                req = "REQUIRED" if required else "optional"
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}")
        self.stdout.write("\nHIYO (PDF fields):")
        for section_name, fields in HIYO_SPEC:
            self.stdout.write(f"\n  SECTION {section_name!r}")
            for fname, kind, required, _ in fields:
                req = "REQUIRED" if required else "optional"
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}")
        self.stdout.write("\nPhoto buckets:")
        for program, buckets in PHOTO_BUCKETS_BY_PROGRAM.items():
            for bucket in buckets:
                self.stdout.write(f"    - {bucket['name']!r}  ({program})")

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
        """The brand's event type — exact-name match only.

        Breakaway has history ("Jimmy Johns Silent DJ", "HIYO Exit Sampling"
        request types). An icontains match would adopt one of those and show
        it on the picker; Kyle wants the selector to read exactly "Jimmy
        Johns" vs "Hiyo", so anything short of an exact match creates a
        clean program.
        """
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

        match = qs.filter(name__iexact=name).first()
        if match:
            return match
        if not apply:
            self.stdout.write(
                self.style.WARNING(f"  DRY-RUN — would create event type {name!r}")
            )
            return None
        slug_base = slug
        suffix = 2
        # Slugs are tenant-scoped unique; a legacy "jimmy-johns" row from
        # another name would collide with a plain create.
        while EventType.objects.filter(tenant_id=tenant.id, slug=slug).exists():
            slug = f"{slug_base}-{suffix}"
            suffix += 1
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
        wanted = {JJ_TEMPLATE_NAME, HIYO_TEMPLATE_NAME}
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
        for spec in ALL_BUCKETS:
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

    def _pin_programs(self, tenant, jj_type, hiyo_type, apply: bool) -> None:
        """Default Jimmy Johns; BA picks Jimmy Johns vs Hiyo on the link.

        Only the standing link's pins are touched. ``is_default`` is left
        alone — Breakaway is an existing tenant and its admin request-flow
        default is not ours to change.
        """
        current = getattr(tenant, "checkin_event_type_id", None)
        already = current == jj_type.id
        if already:
            self.stdout.write(
                f"\ncheckin_event_type already pinned to {jj_type.name!r}."
            )
        elif not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDRY-RUN — would pin checkin_event_type="
                    f"{jj_type.name!r} (id {jj_type.id})"
                )
            )
        else:
            tenant.checkin_event_type = jj_type
            tenant.save(update_fields=["checkin_event_type"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nPinned checkin_event_type={jj_type.name!r} "
                    f"(id {jj_type.id})"
                )
            )
        selectable = [jj_type, hiyo_type]
        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    "  DRY-RUN — would make selectable: "
                    + ", ".join(t.name for t in selectable)
                )
            )
            return
        tenant.checkin_event_types.set(selectable)
        self.stdout.write(
            "  selectable = "
            + ", ".join(f"[{t.id}] {t.name}" for t in selectable)
        )

    def _delete_template(self, template) -> None:
        from recaps.models import CustomField

        CustomField.objects.filter(custom_recap_template=template).delete()
        template.delete()

    def _fold_extra_templates(
        self, tenant, jj_type, hiyo_type, creator, apply: bool, ft_cache: dict
    ) -> None:
        """Leave one named template per brand so walk-up hits the PDF form.

        ``resolve_template_for_event`` does
        ``filter(event_type_id=...).order_by("id").first()``. A leftover on
        the same event type wins over the PDF form because it has the lower
        id. Unused leftovers are deleted. Leftovers with recaps keep their
        rows: drop the empty named clone, rename the leftover, then upsert
        the PDF fields onto it.
        """
        from recaps.models import CustomRecap, CustomRecapTemplate

        pairs = [
            (jj_type, JJ_TEMPLATE_NAME, JJ_SPEC),
            (hiyo_type, HIYO_TEMPLATE_NAME, HIYO_SPEC),
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
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. BRK.")
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
