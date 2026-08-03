"""Stand up Liquid Death's retail-sampling check-in link.

LD ALREADY HAS ITS RECAP FORM — template "Liquid Death-Retail Sampling"
(event_type "Retail Sampling"). This command does NOT create one. Seeding a
second would split the brand's recaps across two forms and halve every
dashboard number; the Feel Free near-miss is written up in
`setup_feel_free_checkin`. What's missing is only:

1. a standing ``LD-`` check-in code on the tenant,
2. the event type that code stamps on the events it opens, and
3. a "Products Sampled" multi-select on the existing template.

(2) is the subtle one. LD runs two programs — Event Activation and Retail
Sampling — and each has its own template. The walk-in path used to pick the
tenant's LOWEST-ID event type, so a retail BA could be handed the 7-field
activation form and nobody would notice, because the recap still submits
fine. Pinning ``Tenant.checkin_event_type`` makes the link deterministic.

Dry-run by default; ``--apply`` writes. Idempotent throughout.
"""

from __future__ import annotations

import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from tenants.models import Tenant

CODE_PREFIX = "LD-"
# No 0/O/1/I/L — the code gets read aloud and retyped off a text message.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

TEMPLATE_HINT = "retail sampling"
EVENT_TYPE_HINT = "retail sampling"

PRODUCTS_SECTION = "Products Sampled"
PRODUCTS_FIELD = "Products Sampled"

# The brand's full SKU list, exactly as it reads on the LD request form
# (/spark-form/ighn-liquid-death) so the two surfaces can't drift. Category
# prefixes keep 31 options scannable on a phone — a flat alphabetical list of
# 31 horror puns is unreadable at arm's length in a store aisle.
PRODUCTS: list[tuple[str, list[str]]] = [
    (
        "Sparkling Water",
        [
            "Mt. Death",
            "Scream Soda",
            "Killer Cola",
            "Killbert Grape",
            "Strawberry Terror",
            "Squeezed-to-Death",
            "Severed Lime",
            "Rootbeer Wrath",
            "Psycho Cider",
            "Pina Killada",
            "Mango Chainsaw",
            "Grave Fruit",
            "Doctor Death",
            "Deathberry Inferno",
            "Cherry Obituary",
            "Cereal Criminal",
        ],
    ),
    (
        "Sparkling Energy",
        [
            "Tropical Terror",
            "Scary Strawberry",
            "Orange Horror",
            "Murder Mystery",
        ],
    ),
    (
        "Mountain Water",
        [
            "Sparkling Water",
            "Still Water",
        ],
    ),
    (
        "Iced Tea",
        [
            "Unsweet Reaper",
            "Death Island",
            "Pop-Tarts™ Carnage",
            "Sweet Reaper",
            "Slaughter Berry",
            "Rest-in-Peach",
            "Green Guillotine",
            "Dead Billionaire",
            "Blueberry Buzzsaw",
        ],
    ),
]


def product_options() -> list[str]:
    """The 31 SKUs as ``"Category — Name"`` choice values."""
    return [f"{cat} — {name}" for cat, names in PRODUCTS for name in names]


class Command(BaseCommand):
    help = (
        "Mint Liquid Death's retail-sampling check-in link and add the "
        "Products Sampled picker to their existing template (dry-run default)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="liquid death")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--code",
            default="",
            help="force a specific check-in code (to keep an already-shared link).",
        )
        parser.add_argument(
            "--training-url",
            default="",
            help="BA reference link shown on the check-in page. Blank = leave as-is.",
        )
        parser.add_argument(
            "--skip-products",
            action="store_true",
            help="don't touch the template; only mint the code / pin the type.",
        )

    # -- helpers -----------------------------------------------------------

    def _resolve_tenant(self, needle: str) -> Tenant:
        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if not matches:
            raise CommandError(f"No tenant matches {needle!r}.")
        if len(matches) > 1:
            for t in matches:
                self.stdout.write(f"  [{t.id}] {t.name!r} / {t.slug!r}")
            raise CommandError(f"{len(matches)} tenants match {needle!r}.")
        return matches[0]

    def _mint_code(self) -> str:
        for _ in range(50):
            candidate = CODE_PREFIX + "".join(
                secrets.choice(ALPHABET) for _ in range(6)
            )
            if not Tenant.objects.filter(checkin_code=candidate).exists():
                return candidate
        raise CommandError("Could not mint an unused check-in code.")

    # -- main --------------------------------------------------------------

    def handle(self, *args, **opts):
        from events.models import EventType
        from recaps.models import CustomRecapTemplate

        tenant = self._resolve_tenant(opts["tenant"].strip())
        apply = bool(opts["apply"])
        forced_code = (opts["code"] or "").strip().upper()
        training_url = (opts["training_url"] or "").strip()
        skip_products = bool(opts["skip_products"])

        self.stdout.write(f"Tenant     : [{tenant.id}] {tenant.name!r}")
        self.stdout.write(f"Code (now) : {tenant.checkin_code or '(none)'}")

        # -- the event type that decides which form a BA gets --------------
        etypes = list(EventType.objects.filter(tenant=tenant).order_by("id"))
        if not etypes:
            raise CommandError(f"Tenant {tenant.slug!r} has no event types.")
        retail = next(
            (e for e in etypes if EVENT_TYPE_HINT in (e.name or "").lower()), None
        )
        self.stdout.write("Event types: " + ", ".join(f"[{e.id}] {e.name}" for e in etypes))
        if retail is None:
            raise CommandError(
                f"No event type on {tenant.slug!r} matches {EVENT_TYPE_HINT!r} — "
                "refusing to guess which form the link should open."
            )
        self.stdout.write(f"  -> pinning checkin_event_type = [{retail.id}] {retail.name!r}")
        if etypes[0].id != retail.id:
            self.stdout.write(
                self.style.WARNING(
                    f"     (without this pin the link would have used "
                    f"[{etypes[0].id}] {etypes[0].name!r} — the wrong form)"
                )
            )

        # -- the template we ADD to (never create) -------------------------
        template = None
        if not skip_products:
            templates = list(
                CustomRecapTemplate.objects.filter(tenant=tenant).order_by("id")
            )
            self.stdout.write("Templates  : " + ", ".join(
                f"[{t.id}] {t.name!r}" for t in templates
            ) or "  (none)")
            template = next(
                (
                    t
                    for t in templates
                    if t.event_type_id == retail.id
                    or TEMPLATE_HINT in (t.name or "").lower()
                ),
                None,
            )
            if template is None:
                raise CommandError(
                    "No existing retail-sampling template found. This command "
                    "deliberately does NOT create one — check the list above."
                )
            from recaps.models import CustomField

            existing = {
                (f.name or "").strip().lower()
                for f in CustomField.objects.filter(custom_recap_template=template)
            }
            already = PRODUCTS_FIELD.strip().lower() in existing
            self.stdout.write(
                f"  -> template [{template.id}] {template.name!r} "
                f"({len(existing)} fields); {PRODUCTS_FIELD!r} "
                + ("ALREADY PRESENT — will update options" if already else "will be ADDED")
            )

        opts_list = product_options()
        self.stdout.write(f"Products   : {len(opts_list)} options")

        if not apply:
            self.stdout.write("")
            for o in opts_list:
                self.stdout.write(f"    · {o}")
            self.stdout.write("")
            self.stdout.write("DRY RUN — re-run with --apply to write.")
            return

        with transaction.atomic():
            tenant.checkin_event_type = retail
            if forced_code:
                tenant.checkin_code = forced_code
            elif not tenant.checkin_code:
                tenant.checkin_code = self._mint_code()
            if training_url:
                tenant.checkin_training_url = training_url
            tenant.save(
                update_fields=[
                    "checkin_event_type",
                    "checkin_code",
                    "checkin_training_url",
                ]
            )

            if template is not None:
                self._ensure_products_field(template, tenant, opts_list)

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Check-in code : {tenant.checkin_code}")
        )
        self.stdout.write(
            f"Link          : https://admin.igniteproductions.co/checkin/"
            f"{tenant.checkin_code}"
        )
        if tenant.checkin_training_url:
            self.stdout.write(f"Training link : {tenant.checkin_training_url}")

    # -- writes ------------------------------------------------------------

    def _ensure_products_field(self, template, tenant, options: list[str]) -> None:
        """Add (or refresh) the Products Sampled multi-select, in place.

        Re-running only rewrites ``options``, so adding a SKU to the brand's
        line-up is a re-dispatch — it never duplicates the field or disturbs
        recaps already filed against it.
        """
        from recaps.models import CustomField, CustomRecapFieldType, RecapSection

        creator = getattr(template, "created_by", None) or getattr(
            tenant, "created_by", None
        )

        field = CustomField.objects.filter(
            custom_recap_template=template, name__iexact=PRODUCTS_FIELD
        ).first()
        if field is not None:
            field.options = options
            field.save(update_fields=["options"])
            self.stdout.write(
                f"  refreshed [{field.id}] {field.name!r} → {len(options)} options"
            )
            return

        # "multiselect" is the canonical token; match loosely because the type
        # table is seeded per-environment and spellings drift.
        ftype = next(
            (
                ft
                for ft in CustomRecapFieldType.objects.all()
                if "multi" in (ft.name or "").lower()
            ),
            None,
        )
        if ftype is None:
            ftype = CustomRecapFieldType.objects.create(
                name="multiselect", created_by=creator
            )

        section, _ = RecapSection.objects.get_or_create(
            tenant_id=tenant.id,
            name=PRODUCTS_SECTION,
            defaults={"order": 5, "created_by": creator},
        )
        last = (
            CustomField.objects.filter(custom_recap_template=template)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
        )
        created = CustomField.objects.create(
            custom_recap_template=template,
            recap_section=section,
            name=PRODUCTS_FIELD,
            custom_field_type=ftype,
            # A BA who sampled nothing (store refused, product never arrived)
            # still has to be able to file the recap.
            required=False,
            options=options,
            order=(last or 0) + 1,
            created_by=creator,
        )
        self.stdout.write(
            f"  created [{created.id}] {created.name!r} → {len(options)} options"
        )
