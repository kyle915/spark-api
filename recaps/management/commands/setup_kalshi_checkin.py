"""Set up Kalshi: tenant + Event Activation recap + standing check-in.

Mirrors the Breakaway Hiyo festival recap (HIYO_SPEC) structure, re-branded
for Kalshi (slug: kalshi):
- Template: "Kalshi · Event Activation Recap"
- Sections: Event Details, Sampling Counts, Feedback
- Metrics: Event Location, Total Tables Distributed, estimated foot traffic /
  impressions, key highlight, consumer comments, brand awareness percent
  ("What percent of consumer had heard of or tried Kalshi before?")
- Photo bucket: Activation Photos (library + camera; photo and video)
- Standing walk-up check-in code: durable KA-XXXXXX link pinned to Event
  Activation only.

Recaps stay human-reviewed (approved=False until admin approve).
Idempotent: an existing checkin_code is never rotated.

DRY-RUN by default. Run via ``/internal/cron/setup-kalshi-checkin`` (or
the "Setup Kalshi check-in" GitHub Action) against prod.
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

TENANT_NAME = "Kalshi"
TENANT_SLUG = "kalshi"
TEMPLATE_NAME = "Kalshi · Event Activation Recap"
CODE_PREFIX = "KA-"
PROGRAM_NAME = "Event Activation"

# Breakaway Hiyo festival form with Kalshi field renames — no Hiyo / White
# Claw / Jimmy Johns / Surge copy on this path.
SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    (
        "Event Details",
        [
            ("Event Location", "text", True, []),
        ],
    ),
    (
        "Sampling Counts",
        [
            ("Total Tables Distributed", "number", True, []),
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
                "What percent of consumer had heard of or tried Kalshi before?",
                "text",
                True,
                [],
            ),
        ],
    ),
]

SECTION_ORDER = {
    "Event Details": 0,
    "Sampling Counts": 1,
    "Feedback": 2,
}

PHOTO_BUCKETS: list[dict] = [
    {
        "name": "Activation Photos",
        "accept": "image/*,video/*",
        "helper": "Photos and videos — library or camera",
    },
]

PHOTO_BUCKETS_BY_PROGRAM: dict[str, list[dict]] = {
    PROGRAM_NAME: list(PHOTO_BUCKETS),
}

ALL_BUCKETS: list[dict] = list(PHOTO_BUCKETS)


class Command(BaseCommand):
    help = (
        "Set up Kalshi: tenant (if missing), Event Activation recap template, "
        "Activation Photos bucket, and standing check-in link "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="kalshi",
            help=(
                "tenant name/slug (exact slug preferred). "
                "Default: 'kalshi'."
            ),
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
            help=f"event type name substring. Default: {PROGRAM_NAME!r}.",
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'KA' -> "
                f"KA-XXXXXX. Blank keeps {CODE_PREFIX!r}."
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
        template_name = opts["template_name"]

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
        self.stdout.write(f"Template   : {template_name!r}")
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
                    f"{len(SPEC)} sections + "
                    f"{sum(len(f) for _, f in SPEC)} fields, "
                    f"photo buckets, and a {CODE_PREFIX} standing link. "
                    "Re-run with --apply to write."
                )
            )
            return

        event_type = self._resolve_event_type(
            tenant, opts.get("event_type"), creator, apply
        )
        self.stdout.write(
            f"Event type : {getattr(event_type, 'name', None)!r} "
            f"(id {getattr(event_type, 'id', None)})"
        )
        if event_type is None:
            raise CommandError(
                f"Tenant {tenant.slug!r} has no event types — seed failed."
            )

        self._report_existing_templates(tenant, template_name)

        ft_cache: dict = {}
        for _, fields in SPEC:
            for _, kind, _, _ in fields:
                self._resolve_field_type(kind, creator, apply, ft_cache)

        if apply:
            with transaction.atomic():
                self._upsert_template(
                    tenant, template_name, event_type, SPEC, creator, apply, ft_cache
                )
        else:
            self._upsert_template(
                tenant, template_name, event_type, SPEC, creator, apply, ft_cache
            )

        self._photo_buckets(tenant, creator, apply)
        self._pin_event_type(tenant, event_type, apply)
        self._fold_extra_templates(
            tenant, event_type, template_name, SPEC, creator, apply, ft_cache
        )
        self._location_mode(tenant, apply)
        self._checkin_code(tenant, apply, opts.get("prefix"))

    def _print_spec(self) -> None:
        for s_idx, (section_name, fields) in enumerate(SPEC):
            self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
            for fname, kind, required, options in fields:
                req = "REQUIRED" if required else "optional"
                extra = f" options={options!r}" if options else ""
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}{extra}")
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

        # Exact slug only — AGENTS: tenant seed/setup resolves by exact slug.
        exact = list(
            Tenant.objects.filter(slug__iexact=TENANT_SLUG).order_by("id")
        )
        if len(exact) == 1:
            return exact[0]

        needle_slug = slugify((needle or "").strip()) or TENANT_SLUG
        if needle_slug.lower() != TENANT_SLUG:
            exact_needle = list(
                Tenant.objects.filter(slug__iexact=needle_slug).order_by("id")
            )
            if len(exact_needle) == 1:
                return exact_needle[0]

        matches = list(
            Tenant.objects.filter(
                Q(name__iexact=TENANT_NAME) | Q(slug__iexact=TENANT_SLUG)
            )
            .distinct()
            .order_by("id")
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            prefer = [
                t
                for t in matches
                if (t.slug or "").lower() == TENANT_SLUG
                or (t.name or "").lower() == TENANT_NAME.lower()
            ]
            if len(prefer) == 1:
                return prefer[0]
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

    def _resolve_event_type(self, tenant, hint: str | None, creator, apply: bool):
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

        wanted = (hint or PROGRAM_NAME).strip()
        match = (
            qs.filter(name__iexact=wanted).first()
            or qs.filter(name__icontains=wanted).first()
        )
        if match:
            return match

        # DEFAULT_EVENT_TYPES has Event / Retail / On-Premise — not Event
        # Activation. Create the program when missing (Brand Revolution-style).
        if not apply:
            self.stdout.write(f"  would create event type {PROGRAM_NAME!r}")
            return EventType(name=PROGRAM_NAME, tenant=tenant, id=0)

        et = EventType.objects.create(
            name=PROGRAM_NAME,
            slug=slugify(PROGRAM_NAME),
            tenant=tenant,
            created_by=creator,
            is_default=True,
        )
        self.stdout.write(f"  + event type {PROGRAM_NAME!r} [{et.id}]")
        return et

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
                    f"  Re-pointed {template_name!r} event_type → {event_type.name!r}"
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

    def _pin_event_type(self, tenant, event_type, apply: bool) -> None:
        if event_type is None or not getattr(event_type, "id", None):
            return
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
        if apply:
            tenant.checkin_event_types.set([event_type])
            if not event_type.is_default:
                from events.models import EventType

                EventType.objects.filter(tenant_id=tenant.id, is_default=True).exclude(
                    pk=event_type.pk
                ).update(is_default=False)
                event_type.is_default = True
                event_type.save(update_fields=["is_default"])

    def _delete_template(self, template) -> None:
        from recaps.models import CustomField

        CustomField.objects.filter(custom_recap_template=template).delete()
        template.delete()

    def _fold_extra_templates(
        self,
        tenant,
        event_type,
        keep_name: str,
        spec,
        creator,
        apply: bool,
        ft_cache: dict,
    ) -> None:
        """Leave one named template on the event type so walk-up hits it.

        ``resolve_template_for_event`` does
        ``filter(event_type_id=...).order_by("id").first()``. A leftover on
        the same event type wins over the intended form if it has the lower
        id. Unused leftovers (0 recaps) are deleted. Leftovers with recaps
        keep their rows: drop the empty keeper, rename the leftover, and
        upsert fields onto it.
        """
        from recaps.models import CustomRecap, CustomRecapTemplate

        tpls = list(
            CustomRecapTemplate.objects.filter(
                tenant_id=tenant.id, event_type=event_type
            ).order_by("id")
        )
        keepers = [t for t in tpls if t.name == keep_name]
        extras = [t for t in tpls if t.name != keep_name]
        if not extras:
            return

        self.stdout.write(f"\nExtra templates on {event_type.name!r}:")
        reupsert = False
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
                    self.stdout.write(self.style.WARNING(f"{msg} — would delete"))
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
                            f"{msg} — would drop empty {keep_name!r} and rename leftover"
                        )
                    )
                    continue
                self._delete_template(keeper)
                extra.name = keep_name
                extra.save(update_fields=["name"])
                keepers = [extra]
                reupsert = True
                self.stdout.write(
                    self.style.SUCCESS(f"{msg} — renamed leftover to {keep_name!r}")
                )
                continue
            self.stdout.write(
                self.style.WARNING(
                    f"    leftover {extra.name!r} (id {extra.id}, {n} recap(s)) AND "
                    f"{keep_name!r} also has recaps — left both"
                )
            )
        if apply and reupsert:
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
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. KA.")
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
