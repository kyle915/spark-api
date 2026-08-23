"""Set up Grub Hub: tenant + recap template + standing check-in.

The Grub Hub twin of ``setup_g7_entertainment_checkin`` — same BA Event
Recap shape off the client's PDF (Leah Love, #1446, 08/19/2026), plus two
campus account-linking counts Kyle asked for on top of the standard fields.

1. The **BA Event Recap** custom template, field-for-field off the client's
   own "BA EVENT RECAP" PDF. Date and Event Location are NOT template
   fields: clock-in on the standing link already captures where/when the BA
   started, and the recap PDF already renders date/location from the event.
   Event Name stays — the activation name (e.g. "Wilson County Fair") is not
   the GPS address.

2. The tenant's **standing check-in code** — one durable ``/checkin/<code>``
   link the field team uses all season. Start your shift → clock in → file
   this recap → clock out. Several BAs at one address on one day land on
   the SAME event.

Photos are labelled **buckets** (Consumer Sampling Pictures, Expense
Receipts) rather than template image fields.

Recaps stay human-reviewed (not Feel Free auto-approve). Location mode is
Event-style address find-or-create.

DRY-RUN by default. Run via ``/internal/cron/setup-grubhub-checkin`` (or
the "Setup Grub Hub check-in" GitHub Action) so it executes against prod.
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

TENANT_NAME = "Grub Hub"
TENANT_SLUG = "grub-hub"
TEMPLATE_NAME = "Grub Hub · BA Event Recap"
CODE_PREFIX = "GH-"
# Grub Hub runs one program. Clock-in, request, and walk-up all stamp this —
# Retail Sampling / On-Premise Sampling are seeded by createTenant and
# then retired so they never show up as a picker.
PROGRAM_NAME = "Event"

# Taken field-for-field from the "BA EVENT RECAP" PDF. Date and Event
# Location belong to the event the check-in resolves. Consumer Sampling
# Pictures and Expense Receipts are photo buckets, not template image
# fields — see PHOTO_BUCKETS.
SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    (
        "Event Details",
        [
            ("Event Name", "text", True, []),
        ],
    ),
    (
        "Consumer Engagement",
        [
            ("How many consumers did you interact with?", "number", True, []),
            (
                "How many students knew they could link their GrubHub "
                "account on-campus?",
                "number",
                True,
                [],
            ),
            (
                "How many students did you help link their account?",
                "number",
                True,
                [],
            ),
            ("Consumer Feedback/Quotes", "longtext", True, []),
        ],
    ),
    (
        "Wrap Up",
        [
            ("Anything you'd improve or change?", "longtext", False, []),
        ],
    ),
]

# Labelled dropzones on the walk-up recap. Names match FileRecapCategory
# rows so the recap PDF groups shots the way the client's form did.
PHOTO_BUCKETS: list[dict] = [
    {"name": "Consumer Sampling Pictures"},
    {"name": "Expense Receipts"},
]


class Command(BaseCommand):
    help = (
        "Set up Grub Hub: tenant (if missing), BA Event Recap template, and "
        "standing check-in link (dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="grub",
            help=(
                "tenant name/slug substring (case-insensitive). "
                "Default: 'grub'."
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
            help=(
                "event type name substring. Default: Event "
                "(Grub Hub is a single program)."
            ),
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'GH' -> "
                f"GH-XXXXXX. Blank keeps {CODE_PREFIX!r}."
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

        event_type = self._resolve_event_type(tenant, opts.get("event_type"), creator, apply)
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
                if template.event_type_id != event_type.id:
                    template.event_type = event_type
                    template.save(update_fields=["event_type"])
                    self.stdout.write(
                        f"  Re-pointed template event_type → {event_type.name!r}"
                    )
                self.stdout.write(
                    f"\nTemplate {'CREATED' if made else 'exists'} "
                    f"(id {template.id}, uuid {template.uuid}, "
                    f"event_type={event_type.name!r})"
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

        self._photo_buckets(tenant, creator, apply)
        self._pin_event_type(tenant, event_type, apply)
        self._retire_other_programs(tenant, event_type, apply)
        self._location_mode(tenant, apply)
        self._checkin_code(tenant, apply, opts.get("prefix"))

    def _print_spec(self) -> None:
        for s_idx, (section_name, fields) in enumerate(SPEC):
            self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
            for fname, kind, required, _ in fields:
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

    def _resolve_event_type(self, tenant, hint: str | None, creator, apply: bool):
        from events.models import EventType
        from recaps.models import CustomRecapTemplate
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

        if hint:
            match = (
                qs.filter(name__iexact=hint).first()
                or qs.filter(name__icontains=hint).first()
            )
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
        # Event, not sampling — one program. Preferring "sampling" silently
        # pinned Retail Sampling and left the BA a picker of three.
        return (
            qs.filter(name__iexact=PROGRAM_NAME).first()
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

        current = getattr(tenant, "checkin_photo_buckets", None)
        if current == PHOTO_BUCKETS:
            self.stdout.write("  checkin_photo_buckets already set — left as-is.")
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING("  DRY-RUN — would set checkin_photo_buckets")
            )
            return
        tenant.checkin_photo_buckets = PHOTO_BUCKETS
        tenant.save(update_fields=["checkin_photo_buckets"])
        self.stdout.write(self.style.SUCCESS("  checkin_photo_buckets set."))

    def _pin_event_type(self, tenant, event_type, apply: bool) -> None:
        if event_type is None:
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

    def _retire_other_programs(self, tenant, event_type, apply: bool) -> None:
        """Leave Event as the only EventType so request + walk-up have no picker.

        createTenant seeds Retail Sampling / On-Premise Sampling / Event.
        Those extras stay in the catalog until we repoint events + the recap
        template onto Event and delete them. RESTRICT FKs block a plain delete.
        """
        from events.models import Event, EventType
        from recaps.models import CustomRecapTemplate

        if event_type is None:
            return
        others = list(
            EventType.objects.filter(tenant_id=tenant.id)
            .exclude(pk=event_type.pk)
            .order_by("id")
        )
        if not others:
            self.stdout.write("\nNo other event types — Event is the only program.")
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
            if not apply:
                self.stdout.write(
                    self.style.WARNING(
                        f"    would retire {extra.name!r}{ref_txt}"
                    )
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
        # Digits stay; drop punctuation only. isalpha() would mint G-XXXXXX.
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if raw and not 1 <= len(cleaned) <= 4:
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. GH.")
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
