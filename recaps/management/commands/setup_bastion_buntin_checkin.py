"""Set up Bastion Buntin: tenant + Future Golf / Avocados clients + standing check-in.

Bastion Buntin is the agency tenant (slug ``bastion-buntin``). Spark's Clients
page lists Tenants; for an agency running several end brands off one BA crew
we mirror Breakaway: each client is a selectable EventType program on the
same standing walk-up URL (not a BrandGroup of separate tenants — that
pattern is for sibling brands that each need their own client portal).

Clients (walk-up programs):
  * Future Golf
  * Avocados from Mexico  (official brand spelling; Kyle's "Avacados" typo)

Recap: Liquid Death / Martinelli's Event Activation shape (Consumer Engagement
+ Feedback & Account Notes + LD photo buckets), with Bastion wording and
**without** the samples-given metric ("How many TOTAL consumers did you
sample?" / "Total samples distributed"). No Products Sampled section — there
is no Bastion product catalog yet. Address location mode. Recaps stay
Needs review (approved=False until admin approve). Requests stay Pending
(``auto_approve_requests`` left false).

Idempotent: an existing ``checkin_code`` is never rotated.

DRY-RUN by default. Run via ``/internal/cron/setup-bastion-buntin-checkin``
(or the "Setup Bastion Buntin check-in" GitHub Action) against prod.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from recaps.management.commands.setup_total_wireless_checkin import (
    _match_field_type,
)

TENANT_NAME = "Bastion Buntin"
TENANT_SLUG = "bastion-buntin"
CODE_PREFIX = "BB-"

FG_PROGRAM = "Future Golf"
AFM_PROGRAM = "Avocados from Mexico"
FG_TEMPLATE_NAME = "Bastion Buntin · Future Golf Event Recap"
AFM_TEMPLATE_NAME = "Bastion Buntin · Avocados from Mexico Event Recap"
# Shared display name Kyle asked for when referring to the event form.
SHARED_TEMPLATE_LABEL = "Bastion Buntin · Event Activation Recap"

ALIAS_SLUGS = frozenset({TENANT_SLUG})
ALIAS_NAMES = frozenset({"bastion buntin", "bastion"})


def _event_spec(brand_label: str) -> list[
    tuple[str, list[tuple[str, str, bool, list[str]]]]
]:
    """LD / Martinelli's Event Activation minus the samples-given count.

    Kept: willingness-to-purchase, brand awareness, prior trial, demographics,
    top FAQs, helpful feedback. Dropped: "How many TOTAL consumers did you
    sample?" (and any "Total samples distributed" twin). No Products Sampled.
    """
    return [
        (
            "Consumer Engagement",
            [
                (
                    "How many consumers would be willing to purchase the product "
                    "after tasting it?",
                    "number",
                    True,
                    [],
                ),
                (
                    f"How many consumers that were engaged with knew about "
                    f"{brand_label}?",
                    "number",
                    True,
                    [],
                ),
                (
                    f"How many consumers had tried {brand_label} before?",
                    "number",
                    True,
                    [],
                ),
                (
                    "How many people did you interact with?",
                    "number",
                    True,
                    [],
                ),
            ],
        ),
        (
            "Feedback & Account Notes",
            [
                ("Demographics", "longtext", True, []),
                (
                    "What were the top 5 frequently asked questions you received "
                    "from consumers?",
                    "longtext",
                    True,
                    [],
                ),
                ("Helpful feedback", "longtext", True, []),
            ],
        ),
    ]


FG_SPEC = _event_spec("Future Golf")
AFM_SPEC = _event_spec("Avocados from Mexico")

SECTION_ORDER = {
    "Consumer Engagement": 0,
    "Feedback & Account Notes": 1,
}

# Liquid Death / Martinelli's Event Activation dropzones (address mode).
CONSUMER_SAMPLING: dict = {
    "name": "Consumer Sampling Pictures",
    "helper": "please try to upload 8+",
    "min": 8,
}
PHOTO_BUCKETS: list[dict] = [
    {"name": "Activation Set Up"},
    CONSUMER_SAMPLING,
    {"name": "Expense Receipts (Parking)"},
]
PHOTO_BUCKETS_BY_PROGRAM: dict[str, list[dict]] = {
    FG_PROGRAM: list(PHOTO_BUCKETS),
    AFM_PROGRAM: list(PHOTO_BUCKETS),
}
ALL_BUCKETS: list[dict] = list(PHOTO_BUCKETS)

PROGRAMS: tuple[tuple[str, str, str, list], ...] = (
    (FG_PROGRAM, "future-golf", FG_TEMPLATE_NAME, FG_SPEC),
    (AFM_PROGRAM, "avocados-from-mexico", AFM_TEMPLATE_NAME, AFM_SPEC),
)


def _needle_is_bastion(needle: str) -> bool:
    raw = (needle or "").strip().lower()
    if not raw:
        return True
    if raw in ALIAS_NAMES or raw in ALIAS_SLUGS:
        return True
    return slugify(raw) in ALIAS_SLUGS


class Command(BaseCommand):
    help = (
        "Set up Bastion Buntin: tenant, Future Golf + Avocados from Mexico "
        "Event Activation recaps (LD shape minus samples-given), photo "
        "buckets, and a standing check-in link (dry-run by default; "
        "--apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default=TENANT_SLUG,
            help=(
                f"Exact tenant slug or request_url_name. Default: {TENANT_SLUG!r}."
            ),
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'BB' -> "
                f"BB-XXXXXX. Blank keeps {CODE_PREFIX!r}."
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
        self.stdout.write(f"Form shape : {SHARED_TEMPLATE_LABEL!r}")
        for _prog, _slug, tpl_name, _spec in PROGRAMS:
            self.stdout.write(f"  template : {tpl_name!r}")
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)
        self.stdout.write(
            "Clients (walk-up programs): Future Golf, Avocados from Mexico"
        )
        self.stdout.write(
            "Dropped field: samples given / TOTAL consumers sampled / "
            "Total samples distributed"
        )

        if tenant is None:
            self._print_spec()
            self.stdout.write("\n" + "=" * 68)
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would create tenant {TENANT_NAME!r}, "
                    f"Future Golf + Avocados from Mexico templates, "
                    f"photo buckets, and a {CODE_PREFIX} standing link. "
                    "Re-run with --apply to write."
                )
            )
            return

        event_types: list = []
        for prog_name, prog_slug, _tpl, _spec in PROGRAMS:
            et = self._ensure_program(
                tenant, prog_name, prog_slug, creator, apply
            )
            self.stdout.write(
                f"Program    : {getattr(et, 'name', prog_name)!r} "
                f"(id {getattr(et, 'id', None)})"
            )
            event_types.append(et)
            self._ensure_request_type(tenant, prog_name, creator, apply)

        if any(et is None for et in event_types):
            if not apply:
                self._print_spec()
                return
            raise CommandError(
                f"Tenant {tenant.slug!r} is missing Future Golf or "
                "Avocados from Mexico — seed failed."
            )

        self._report_existing_templates(tenant)

        ft_cache: dict = {}
        for _prog, _slug, _tpl, spec in PROGRAMS:
            for _, fields in spec:
                for _, kind, _, _ in fields:
                    self._resolve_field_type(kind, creator, apply, ft_cache)

        template_jobs = [
            (tpl_name, et, spec)
            for (_prog, _slug, tpl_name, spec), et in zip(PROGRAMS, event_types)
        ]

        def _run():
            for tpl_name, et, spec in template_jobs:
                self._upsert_template(
                    tenant, tpl_name, et, spec, creator, apply, ft_cache
                )

        if apply:
            with transaction.atomic():
                _run()
        else:
            _run()

        self._photo_buckets(tenant, creator, apply)
        self._pin_programs(tenant, event_types, apply)
        self._fold_extra_templates(
            tenant, template_jobs, creator, apply, ft_cache
        )
        self._location_mode(tenant, apply)
        self._checkin_code(tenant, apply, opts.get("prefix") or "")

    def _print_spec(self) -> None:
        for prog_name, _slug, tpl_name, spec in PROGRAMS:
            self.stdout.write(f"\n### {prog_name} → {tpl_name!r}")
            for section_name, fields in spec:
                s_idx = SECTION_ORDER[section_name]
                self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
                for fname, kind, required, options in fields:
                    req = "REQUIRED" if required else "optional"
                    extra = f" options={options!r}" if options else ""
                    self.stdout.write(f"    - {fname!r}  [{kind}] {req}{extra}")
        self.stdout.write("\nPhoto buckets (both programs):")
        for bucket in PHOTO_BUCKETS:
            self.stdout.write(f"    - {bucket['name']!r}")
        self.stdout.write("\nLocation mode: 'address'")

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
        if not _needle_is_bastion(needle):
            raise CommandError(
                f"--tenant {needle!r} is not Bastion Buntin. "
                f"Pass exact slug {TENANT_SLUG!r}."
            )

        found = self._find_existing_tenant()
        if found is not None:
            return found

        self.stdout.write(
            self.style.WARNING(
                f"No tenant matches {TENANT_SLUG!r} — "
                f"{'creating' if apply else 'would create'} "
                f"{TENANT_NAME!r} (slug {TENANT_SLUG!r})."
            )
        )
        if not apply:
            return None
        return self._create_tenant(creator)

    def _find_existing_tenant(self):
        """Exact slug, then exact request_url_name, then exact alias name."""
        from django.db.models import Q

        from tenants.models import Tenant

        by_id: dict[int, object] = {}

        def _add(qs):
            for tenant in qs:
                by_id[tenant.id] = tenant

        _add(Tenant.objects.filter(slug__iexact=TENANT_SLUG).order_by("id"))
        if not by_id:
            _add(
                Tenant.objects.filter(
                    request_url_name__iexact=TENANT_SLUG
                ).order_by("id")
            )
        if not by_id:
            name_q = Q()
            for alias in ALIAS_NAMES:
                name_q |= Q(name__iexact=alias)
            _add(Tenant.objects.filter(name_q).order_by("id"))

        if not by_id:
            return None
        rows = list(by_id.values())
        if len(rows) > 1:
            for tenant in rows:
                self.stdout.write(
                    f"  [{tenant.id}] name={tenant.name!r} "
                    f"slug={tenant.slug!r} "
                    f"request_url_name={tenant.request_url_name!r}"
                )
            raise CommandError(
                f"{len(rows)} tenants match Bastion Buntin / {TENANT_SLUG!r}. "
                "Resolve the duplicate before setup."
            )
        return rows[0]

    def _create_tenant(self, creator):
        """Create the tenant the same way ``createTenant`` seeds a new brand."""
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
            RateType.objects.create(name=rate_type, tenant=tenant, created_by=creator)
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
        """Client program event type — exact-name match only."""
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
        candidate = slug
        suffix = 2
        while EventType.objects.filter(tenant_id=tenant.id, slug=candidate).exists():
            candidate = f"{slug_base}-{suffix}"
            suffix += 1
        created = EventType.objects.create(
            name=name,
            slug=candidate,
            tenant=tenant,
            created_by=creator,
            is_default=False,
        )
        self.stdout.write(
            self.style.SUCCESS(f"  Created event type {name!r} (id {created.id})")
        )
        return created

    def _ensure_request_type(self, tenant, name: str, creator, apply: bool) -> None:
        from events.models import RequestType

        if RequestType.objects.filter(tenant_id=tenant.id, name__iexact=name).exists():
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING(f"  DRY-RUN — would create request type {name!r}")
            )
            return
        RequestType.objects.create(name=name, tenant=tenant, created_by=creator)
        self.stdout.write(self.style.SUCCESS(f"  + request type {name!r}"))

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
        wanted = {FG_TEMPLATE_NAME, AFM_TEMPLATE_NAME}
        for template in rows:
            n_fields = CustomField.objects.filter(
                custom_recap_template=template
            ).count()
            same = " <-- SAME NAME, will be reused" if template.name in wanted else ""
            self.stdout.write(
                f"  [{template.id}] {template.name!r} — {n_fields} field(s), "
                f"event_type={getattr(template.event_type, 'name', None)!r}{same}"
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
            changed_tpl = []
            if template.event_type_id != event_type.id:
                template.event_type = event_type
                changed_tpl.append("event_type")
            if template.product_samples is not False:
                template.product_samples = False
                changed_tpl.append("product_samples")
            if changed_tpl:
                template.save(update_fields=changed_tpl)
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

            if apply:
                # Drop the samples-given field if a prior seed added it.
                stale = CustomField.objects.filter(
                    custom_recap_template=template,
                    recap_section=section,
                ).exclude(name__in=[f[0] for f in fields])
                for old in stale:
                    lowered = (old.name or "").lower()
                    if (
                        "total consumers did you sample" in lowered
                        or "samples distributed" in lowered
                        or "samples given" in lowered
                        or "samples handed" in lowered
                    ):
                        self.stdout.write(
                            self.style.WARNING(
                                f"    - removing samples-given field {old.name!r}"
                            )
                        )
                        old.delete()

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

    def _pin_programs(self, tenant, event_types: list, apply: bool) -> None:
        """Default Future Golf; BA picks Future Golf / Avocados from Mexico."""
        primary = event_types[0]
        current = getattr(tenant, "checkin_event_type_id", None)
        already = current == primary.id
        if already:
            self.stdout.write(
                f"\ncheckin_event_type already pinned to {primary.name!r}."
            )
        elif not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDRY-RUN — would pin checkin_event_type="
                    f"{primary.name!r} (id {primary.id})"
                )
            )
        else:
            tenant.checkin_event_type = primary
            tenant.save(update_fields=["checkin_event_type"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nPinned checkin_event_type={primary.name!r} "
                    f"(id {primary.id})"
                )
            )
        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    "  DRY-RUN — would make selectable: "
                    + ", ".join(t.name for t in event_types)
                )
            )
            return
        tenant.checkin_event_types.set(event_types)
        self.stdout.write(
            "  selectable = "
            + ", ".join(f"[{t.id}] {t.name}" for t in event_types)
        )
        if apply and not primary.is_default:
            from events.models import EventType

            EventType.objects.filter(tenant_id=tenant.id, is_default=True).exclude(
                pk=primary.pk
            ).update(is_default=False)
            primary.is_default = True
            primary.save(update_fields=["is_default"])

    def _delete_template(self, template) -> None:
        from recaps.models import CustomField

        CustomField.objects.filter(custom_recap_template=template).delete()
        template.delete()

    def _fold_extra_templates(
        self,
        tenant,
        template_jobs: list,
        creator,
        apply: bool,
        ft_cache: dict,
    ) -> None:
        """Leave one named template per client program so walk-up hits it."""
        from recaps.models import CustomRecap, CustomRecapTemplate

        reupsert: list = []
        for keep_name, event_type, spec in template_jobs:
            if not getattr(event_type, "id", None):
                continue
            tpls = list(
                CustomRecapTemplate.objects.filter(
                    tenant_id=tenant.id, event_type=event_type
                ).order_by("id")
            )
            keepers = [t for t in tpls if t.name == keep_name]
            extras = [t for t in tpls if t.name != keep_name]
            if not extras:
                continue

            self.stdout.write(f"\nExtra templates on {event_type.name!r}:")
            for extra in extras:
                n = CustomRecap.objects.filter(custom_recap_template=extra).count()
                keeper = keepers[0] if keepers else None
                n_keep = (
                    CustomRecap.objects.filter(custom_recap_template=keeper).count()
                    if keeper
                    else 0
                )
                if n == 0:
                    msg = f"    leftover {extra.name!r} (id {extra.id}, 0 recaps)"
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
                        f"    leftover {extra.name!r} (id {extra.id}, {n} recap(s)); "
                        f"empty {keep_name!r} would lose walk-up (order_by id)"
                    )
                    if not apply:
                        self.stdout.write(
                            self.style.WARNING(
                                f"{msg} — would drop empty {keep_name!r} "
                                "and rename leftover"
                            )
                        )
                        continue
                    self._delete_template(keeper)
                    extra.name = keep_name
                    extra.save(update_fields=["name"])
                    keepers = [extra]
                    reupsert.append((keep_name, event_type, spec))
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"{msg} — renamed leftover to {keep_name!r}"
                        )
                    )
                    continue
                self.stdout.write(
                    self.style.WARNING(
                        f"    leftover {extra.name!r} (id {extra.id}, {n} recap(s)) "
                        f"AND {keep_name!r} also has recaps — left both"
                    )
                )
        if apply:
            for keep_name, event_type, spec in reupsert:
                self._upsert_template(
                    tenant, keep_name, event_type, spec, creator, apply, ft_cache
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
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. BB.")
        code_prefix = f"{cleaned}-" if cleaned else CODE_PREFIX

        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
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
                secrets.choice(alphabet) for _ in range(6)
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
        self.stdout.write(
            f"  Request form: {base}/spark-form/"
            f"{tenant.request_url_name or TENANT_SLUG}"
        )
