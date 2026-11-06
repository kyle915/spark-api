"""Mark Anthony Brands · Total Wine Long Drink retail check-in.

Torch THC–style Retail Sampling recap, worded for Long Drink (not THC),
with Products Sampled limited to The Finnish Long Drink SKUs (+ can art
from ``onboard_long_drink_products``). Standing walk-up stays
``MAB-V696HM`` (never reminted). Retail photo buckets match LD/MAB retail.

Keeps Event Activation + On-Premise selectable on the same link; only the
Retail Sampling form is rewritten for Long Drink Total Wine demos.

DRY-RUN by default. Run via ``/internal/cron/setup-mab-long-drink-checkin``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from recaps.field_conditions import FIELD_CONDITIONS_KEY
from recaps.management.commands.long_drink_products import (
    PRODUCT_TYPE,
    long_drink_product_options,
)
from recaps.management.commands.setup_mab_checkin import (
    CODE_PREFIX,
    PHOTO_BUCKETS,
    PROGRAMS as MAB_PROGRAMS,
)
from recaps.management.commands.setup_total_wireless_checkin import (
    _match_field_type,
)
from recaps.products_sampled import (
    PRODUCTS_SAMPLED_FIELD,
    PRODUCTS_SAMPLED_TYPES_KEY,
)

TENANT_NAME = "Mark Anthony Brands"
TENANT_SLUG = "mark-anthony-brands"
# Already shared when MAB check-in was first applied. Never remint.
STANDING_CODE = "MAB-V696HM"
RETAIL_PROGRAM = "Retail Sampling"
RETAIL_TEMPLATE_NAME = "Mark Anthony Brands-Retail Sampling"

SAMPLE_FORMAT_FIELD = "Sample format"
SAMPLE_FORMAT_OPTS = ["Full can", "4oz pour", "No samples (dry demo)"]
POUR_OPT = "4oz pour"
POURS_SERVED_FIELD = "4oz pours served"
POURS_CONDITION = {
    "field": SAMPLE_FORMAT_FIELD,
    "includes": POUR_OPT,
    "required": True,
}

COMPETITOR_FIELD = (
    "Ask consumers what competitor products they drink. "
    "And if so, which one. Why do you like it?"
)

# Torch THC-Retail Sampling shape, Long Drink copy (no THC / dose language).
RETAIL_SPEC: list[tuple[str, list[tuple]]] = [
    (
        "Consumer Engagement",
        [
            (
                "Dry demo? (no product tasted)",
                "select",
                True,
                ["No", "Yes"],
                "",
            ),
            (
                "Sample format",
                "multiselect",
                True,
                list(SAMPLE_FORMAT_OPTS),
                "Pick both Full can and 4oz pour if you handed out cans and poured",
            ),
            (
                "4oz pours served",
                "number",
                False,
                [],
                "Each 4oz cup = 1 pour. Full cans handed out go under cans purchased.",
            ),
            (
                "Total number of consumers sampled",
                "number",
                True,
                [],
                "People 21+ who actually tasted Long Drink",
            ),
            (
                "First Time consumers?",
                "number",
                True,
                [],
                "First time trying Long Drink",
            ),
            (
                "People engaged",
                "number",
                True,
                [],
                "People you talked with about Long Drink, tasted or not",
            ),
            (
                "How many consumers that were engaged with knew about "
                "Long Drink product/brand?",
                "number",
                True,
                [],
                "",
            ),
            (
                "How many consumers would be willing to purchase Long Drink "
                "after tasting it?",
                "number",
                True,
                [],
                "",
            ),
            (
                "How many consumers would NOT be willing to purchase Long Drink "
                "after tasting it?",
                "number",
                True,
                [],
                "",
            ),
            (
                "How many single cans did consumers purchase?",
                "number",
                True,
                [],
                "",
            ),
            (
                "How many packs did consumers purchase?",
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
                "e.g. Mostly 25-40, even male/female split, Total Wine weekend crowd",
            ),
            (
                "Consumer Feedback",
                "longtext",
                True,
                [],
                "Which flavors landed, and what people said about taste and refreshment",
            ),
            (
                "Positive stories from your sampling today",
                "longtext",
                True,
                [],
                "",
            ),
            (
                "Reasons to decline to purchase?",
                "longtext",
                True,
                [],
                "",
            ),
            (
                "Quotes from Consumers",
                "longtext",
                True,
                [],
                '"This tastes like a real citrus soda, not beer." - female, ~30',
            ),
            (COMPETITOR_FIELD, "longtext", False, [], ""),
            (
                "Anything you'd change or do differently?",
                "longtext",
                True,
                [],
                "",
            ),
            (
                "Account Feedback",
                "longtext",
                True,
                [],
                "What Total Wine staff / managers said about Long Drink or the table",
            ),
        ],
    ),
    (
        "Additional Insights",
        [
            ("Account Spend Amount", "number", True, [], ""),
        ],
    ),
    (
        "Products Sampled",
        [
            (
                PRODUCTS_SAMPLED_FIELD,
                "multiselect",
                True,
                list(long_drink_product_options()),
                "Pick every Long Drink SKU you poured, then enter cans sampled",
            ),
        ],
    ),
]

SECTION_ORDER = {
    "Consumer Engagement": 0,
    "Feedback & Account Notes": 1,
    "Additional Insights": 2,
    "Products Sampled": 3,
}


class Command(BaseCommand):
    help = (
        "Set up Mark Anthony Brands Total Wine Long Drink retail check-in "
        "(Torch-style recap, Long Drink SKUs + art, standing MAB- link)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="mark anthony",
            help="tenant name/slug substring. Default: 'mark anthony'.",
        )
        parser.add_argument(
            "--owner-email",
            dest="owner_email",
            default="kyle@igniteproductions.co",
            help="Owner for new Product rows / field types.",
        )
        parser.add_argument(
            "--code",
            default=STANDING_CODE,
            help=f"Standing walk-up code (default {STANDING_CODE}; never remint).",
        )
        parser.add_argument(
            "--skip-images",
            dest="skip_images",
            action="store_true",
            help="Skip Long Drink artwork downloads.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for dry-run).",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        creator = self._resolve_creator(opts["owner_email"])
        tenant = self._resolve_tenant(opts["tenant"])
        forced_code = (opts.get("code") or STANDING_CODE).strip().upper()

        self.stdout.write("=" * 68)
        self.stdout.write(
            f"Tenant     : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
        )
        self.stdout.write(
            f"Check-in   : {(tenant.checkin_code or '').strip() or '(none)'} "
            f"→ keep/force {forced_code!r}"
        )
        self.stdout.write(f"Template   : {RETAIL_TEMPLATE_NAME!r}")
        self.stdout.write(
            f"SKUs       : {len(long_drink_product_options())} Long Drink "
            "(Torch-style can art)"
        )
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        self._print_spec()

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY-RUN — would: ensure MAB walk-up + retail buckets, "
                    "rewrite Retail Sampling to Torch/Long Drink wording, "
                    "onboard Long Drink SKUs (+ images), pin Retail default, "
                    f"force check-in code {forced_code!r}. "
                    "Re-run with --apply to write."
                )
            )
            return

        # Standing link + photo buckets for all MAB programs (idempotent).
        call_command(
            "setup_mab_checkin",
            tenant=tenant.slug,
            code=forced_code,
            apply=True,
            stdout=self.stdout,
        )
        tenant.refresh_from_db()

        call_command(
            "onboard_long_drink_products",
            owner_email=opts["owner_email"],
            tenant=tenant.slug,
            apply=True,
            skip_images=bool(opts["skip_images"]),
            stdout=self.stdout,
        )

        retail_type = self._ensure_event_type(tenant, RETAIL_PROGRAM, creator)
        ft_cache: dict = {}
        with transaction.atomic():
            self._upsert_retail_template(
                tenant, retail_type, creator, ft_cache
            )
            # Ensure Retail stays pinned default; keep other programs selectable.
            types = list(tenant.checkin_event_types.all())
            if retail_type not in types:
                types.insert(0, retail_type)
            # Stable order: Retail first, then the rest alphabetically by name.
            ordered = [retail_type] + [
                t for t in types if t.id != retail_type.id
            ]
            tenant.checkin_event_type = retail_type
            tenant.checkin_code = forced_code or tenant.checkin_code
            tenant.save(update_fields=["checkin_event_type", "checkin_code"])
            tenant.checkin_event_types.set(ordered)

        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "")
            or "https://client.igniteproductions.co"
        ).rstrip("/")
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Check-in code : {tenant.checkin_code}")
        )
        self.stdout.write(f"Link          : {base}/checkin/{tenant.checkin_code}")
        self.stdout.write(
            f"Retail form   : {RETAIL_TEMPLATE_NAME} "
            f"(Long Drink · Torch-style fields)"
        )
        self.stdout.write(
            "Photo buckets : "
            + " | ".join(b["name"] for b in PHOTO_BUCKETS)
        )
        self.stdout.write(
            "Programs      : " + ", ".join(p["label"] for p in MAB_PROGRAMS)
        )

    def _print_spec(self) -> None:
        self.stdout.write(f"\nRetail template {RETAIL_TEMPLATE_NAME!r}")
        for s_idx, (section, fields) in enumerate(RETAIL_SPEC):
            self.stdout.write(f"\n[{s_idx}] SECTION {section!r}")
            for row in fields:
                fname, kind, required, options = row[0], row[1], row[2], row[3]
                req = "REQUIRED" if required else "optional"
                extra = f" options={options!r}" if options else ""
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}{extra}")

    def _resolve_creator(self, owner_email: str):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        if owner_email:
            hit = User.objects.filter(email__iexact=owner_email).first()
            if hit:
                return hit
        creator = (
            User.objects.filter(is_superuser=True).order_by("id").first()
            or User.objects.order_by("id").first()
        )
        if creator is None:
            raise CommandError("No user available to own the created rows.")
        return creator

    def _resolve_tenant(self, needle: str):
        from tenants.models import Tenant

        search = (needle or "").strip()
        if search.lower() == "mab":
            search = "mark anthony"
        exact = list(Tenant.objects.filter(slug=TENANT_SLUG).order_by("id"))
        if len(exact) == 1:
            return exact[0]
        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=search) | Q(slug__icontains=search)
            )
            .distinct()
            .order_by("id")
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise CommandError(
                f"No tenant matches {needle!r}. Mark Anthony Brands must exist "
                "(do not invent a new client)."
            )
        raise CommandError(
            f"{needle!r} matched {len(matches)} tenants — narrow --tenant."
        )

    def _ensure_event_type(self, tenant, name: str, creator):
        from events.models import EventType

        et, created = EventType.objects.get_or_create(
            tenant=tenant,
            name=name,
            defaults={"created_by": creator},
        )
        if created:
            self.stdout.write(f"  + event type {name!r} [{et.id}]")
        return et

    def _resolve_field_type(self, kind: str, creator, cache: dict):
        if kind in cache:
            return cache[kind]
        from recaps.models import CustomRecapFieldType

        existing = None
        for ft in CustomRecapFieldType.objects.all():
            if _match_field_type(kind, (ft.name or "").lower()):
                existing = ft
                break
        if existing is None:
            existing = CustomRecapFieldType.objects.create(
                name=kind, created_by=creator
            )
        cache[kind] = existing
        return existing

    def _upsert_retail_template(self, tenant, event_type, creator, ft_cache):
        from recaps.models import (
            CustomField,
            CustomRecapTemplate,
            RecapSection,
        )

        tpl = (
            CustomRecapTemplate.objects.filter(
                tenant_id=tenant.id, name=RETAIL_TEMPLATE_NAME
            )
            .order_by("id")
            .first()
        )
        if tpl is None:
            tpl = CustomRecapTemplate.objects.create(
                name=RETAIL_TEMPLATE_NAME,
                tenant=tenant,
                event_type=event_type,
                created_by=creator,
            )
            self.stdout.write(f"  + template {RETAIL_TEMPLATE_NAME!r} [{tpl.id}]")
        else:
            if tpl.event_type_id != event_type.id:
                tpl.event_type = event_type
                tpl.save(update_fields=["event_type", "updated_at"])
            self.stdout.write(f"  = template {RETAIL_TEMPLATE_NAME!r} [{tpl.id}]")

        # Layout: Long Drink type filter + pours condition.
        layout = dict(tpl.layout) if isinstance(tpl.layout, dict) else {}
        layout[PRODUCTS_SAMPLED_TYPES_KEY] = [PRODUCT_TYPE]
        conditions = dict(layout.get(FIELD_CONDITIONS_KEY) or {})
        conditions[POURS_SERVED_FIELD] = dict(POURS_CONDITION)
        layout[FIELD_CONDITIONS_KEY] = conditions
        tpl.layout = layout
        tpl.save(update_fields=["layout", "updated_at"])

        wanted_names: set[str] = set()
        for section_name, fields in RETAIL_SPEC:
            section, _ = RecapSection.objects.get_or_create(
                tenant_id=tenant.id,
                name=section_name,
                defaults={
                    "order": SECTION_ORDER.get(section_name, 99),
                    "created_by": creator,
                },
            )
            if section.order != SECTION_ORDER.get(section_name, section.order):
                section.order = SECTION_ORDER.get(section_name, 99)
                section.save(update_fields=["order", "updated_at"])

            for order, row in enumerate(fields):
                fname, kind, required, options = row[0], row[1], row[2], row[3]
                placeholder = row[4] if len(row) > 4 else ""
                wanted_names.add(fname)
                ft = self._resolve_field_type(kind, creator, ft_cache)
                field = CustomField.objects.filter(
                    custom_recap_template=tpl, name=fname
                ).first()
                want = {
                    "recap_section_id": section.id,
                    "custom_field_type_id": ft.id,
                    "required": required,
                    "options": list(options),
                    "placeholder": placeholder or "",
                    "order": order,
                }
                if field is None:
                    CustomField.objects.create(
                        custom_recap_template=tpl,
                        recap_section=section,
                        name=fname,
                        custom_field_type=ft,
                        required=required,
                        options=list(options),
                        placeholder=placeholder or "",
                        order=order,
                        created_by=creator,
                    )
                    self.stdout.write(f"    + field {fname!r}")
                else:
                    changed = [
                        k for k, v in want.items() if getattr(field, k) != v
                    ]
                    if changed:
                        for k in changed:
                            setattr(field, k, want[k])
                        field.save(update_fields=[*changed, "updated_at"])
                        self.stdout.write(f"    ~ field {fname!r}")

        # Drop retail fields that are no longer on the Long Drink Torch form
        # only when they have never been answered (safe prune).
        from recaps.models import CustomFieldValue

        stale = CustomField.objects.filter(custom_recap_template=tpl).exclude(
            name__in=wanted_names
        )
        for field in stale:
            if CustomFieldValue.objects.filter(custom_field=field).exists():
                self.stdout.write(
                    self.style.WARNING(
                        f"    ! keeping answered legacy field {field.name!r}"
                    )
                )
                continue
            self.stdout.write(f"    - removed unused field {field.name!r}")
            field.delete()

        return tpl
