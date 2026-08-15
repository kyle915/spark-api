"""Set up Total Wireless field capture: recap template + standing check-in link.

Two things in one idempotent command, because they only make sense together:

1. The **BA Event Recap** custom template — the exact seven fields off the
   client's own recap PDF (date/location are captured by the event itself, so
   the template holds what the BA actually types). Expense Receipts is
   deliberately NOT here; Kyle removed it.

2. The tenant's **standing check-in code** — one durable link
   (`/checkin/<code>`) pinned on the Total Wireless page and shared with every
   BA. They open it on a phone, give their name, pick the store + date, clock
   in/out and file this recap. Several BAs at one store on one day land on the
   SAME event (see ambassadors/checkin_web.find_or_create_walkin_event).

DRY-RUN by default: prints the resolved tenant, the planned template and the
link it WOULD mint, without writing. Pass --apply to persist. Self-diagnosing —
an unmatched tenant dumps every tenant so the caller learns the exact slug.

Run via ``/internal/cron/setup-total-wireless-checkin`` (or the "Setup Total
Wireless check-in" GitHub Action) so it executes against prod.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# Template layout: (section name, [(field name, kind, required, options)]).
# `kind` is one of the canonical CustomRecapFieldType tokens the renderers
# fuzzy-match: text / number / longtext / image / select / multiselect.
#
# Taken field-for-field from the client's own "BA EVENT RECAP" PDF. Date and
# Event Location are NOT template fields — they belong to the event the
# check-in resolves, and the recap PDF already renders them from there, so
# duplicating them would make the BA type what Spark already knows.
TEMPLATE_NAME = "Total Wireless \u00b7 BA Event Recap"
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
            ("Consumer Feedback/Quotes", "longtext", True, []),
        ],
    ),
    (
        "Photos",
        [
            ("Consumer Sampling Pictures", "image", True, []),
        ],
    ),
    (
        "Wrap Up",
        [
            ("Anything you'd improve or change?", "longtext", False, []),
        ],
    ),
]


def _match_field_type(kind: str, name_lower: str) -> bool:
    """Does an existing CustomRecapFieldType named `name_lower` serve `kind`?

    Mirrors the FE `customFieldKind` fuzzy rules so a field renders as the
    intended control. Order matters: multiselect before select (both contain
    'select'); text is an exact match so it never swallows longtext/multiselect.
    """
    if kind == "image":
        return any(t in name_lower for t in ("image", "photo", "img"))
    if kind == "multiselect":
        return name_lower == "multiselect" or "multi" in name_lower
    if kind == "select":
        return name_lower == "select" or "dropdown" in name_lower
    if kind == "number":
        return name_lower == "number" or "num" in name_lower
    if kind == "longtext":
        return "long" in name_lower or "textarea" in name_lower or "paragraph" in name_lower
    if kind == "text":
        return name_lower == "text"
    return False


class Command(BaseCommand):
    help = (
        "Set up Total Wireless: recap template + standing check-in link "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="total-wireless",
            help="tenant name/slug substring (case-insensitive). Default: 'total-wireless'.",
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
            help="event type name substring to attach the template to. Default: prefer 'retail', else the tenant's first.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing).",
        )

    # ---- resolvers -------------------------------------------------------

    def _resolve_tenant(self, needle: str):
        from tenants.models import Tenant
        from django.db.models import Q

        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if len(matches) == 1:
            return matches[0]
        # Self-diagnosing: dump every tenant so the caller learns the exact
        # slug (or that the tenant isn't onboarded yet).
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

        qs = EventType.objects.filter(tenant_id=tenant.id).order_by("id")
        if hint:
            match = qs.filter(name__icontains=hint).first()
            if not match:
                raise CommandError(
                    f"No event type on tenant {tenant.slug!r} matches {hint!r}."
                )
            return match
        # Prefer a "Retail Sampling"-style type; reuse the tenant's existing
        # template event type if one is already set; else the first.
        from recaps.models import CustomRecapTemplate

        existing = (
            CustomRecapTemplate.objects.filter(tenant_id=tenant.id)
            .select_related("event_type")
            .first()
        )
        return (
            qs.filter(name__icontains="retail").first()
            or (existing.event_type if existing else None)
            or qs.first()
        )

    def _resolve_field_type(self, kind: str, creator, apply: bool, cache: dict):
        """Find (or, under --apply, create) the CustomRecapFieldType for a kind.

        Reuses an existing global row when one fuzzy-matches (so we don't add a
        near-duplicate to what Girl Beer / Borjomi already seeded); otherwise
        falls back to the canonical lowercase name.
        """
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
        self.stdout.write(f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")
        self.stdout.write("=" * 68)

        if event_type is None:
            raise CommandError(
                f"Tenant {tenant.slug!r} has no event types — run set_tenant_event_types "
                f"first, then re-run this."
            )

        # Resolve field types up front (reports would-create rows in dry-run).
        ft_cache: dict = {}
        for _, fields in SPEC:
            for _, kind, _, _ in fields:
                self._resolve_field_type(kind, creator, apply, ft_cache)

        from recaps.models import CustomRecapTemplate, RecapSection, CustomField

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
                    f"Template {'CREATED' if made else 'exists'} "
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
                    opt = f" options={options}" if options else ""
                    self.stdout.write(
                        f"    - {fname!r}  [{kind}] {req}{opt}"
                    )
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
                        if list(field.options or []) != list(options):
                            field.options = list(options)
                            changed.append("options")
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

        self._checkin_code(tenant, apply)

    # ---- standing check-in link -----------------------------------------

    def _checkin_code(self, tenant, apply: bool) -> None:
        """Mint (or report) the tenant's standing check-in code.

        Deliberately short and unambiguous: BAs read these off a text message,
        so the alphabet drops the characters people mistype between each other
        (0/O, 1/I/L). Never regenerated once set — the link is pinned on the
        client page and shared around, so rotating it would silently break every
        copy already in circulation.
        """
        import secrets

        ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        base = (getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "") or
                "https://client.igniteproductions.co").rstrip("/")

        self.stdout.write("\n" + "=" * 68)
        existing = (getattr(tenant, "checkin_code", "") or "").strip()
        if existing:
            self.stdout.write(
                f"Check-in code already set: {existing}\n"
                f"  Link: {base}/checkin/{existing}\n"
                "  (left as-is — rotating it would break every copy already shared)"
            )
            return

        from tenants.models import Tenant

        code = None
        for _ in range(12):
            candidate = "TW-" + "".join(secrets.choice(ALPHABET) for _ in range(6))
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
