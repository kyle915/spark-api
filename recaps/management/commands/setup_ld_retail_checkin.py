"""Stand up Liquid Death's retail-sampling check-in link.

LD ALREADY HAS ITS RECAP FORM — template "Liquid Death-Retail Sampling"
(event_type "Retail Sampling"). This command does NOT create one. Seeding a
second would split the brand's recaps across two forms and halve every
dashboard number; the Feel Free near-miss is written up in
`setup_feel_free_checkin`. What's missing is only:

1. a standing ``LD-`` check-in code on the tenant,
2. the event type that code stamps on the events it opens,
3. a "Products Sampled" multi-select on the existing template, and
4. the four labelled PHOTO BUCKETS the recap step uploads into.

(2) is the subtle one. LD runs two programs — Event Activation and Retail
Sampling — and each has its own template. The walk-in path used to pick the
tenant's LOWEST-ID event type, so a retail BA could be handed the 7-field
activation form and nobody would notice, because the recap still submits
fine. Pinning ``Tenant.checkin_event_type`` makes the link deterministic.

(4) is a pair of writes that have to agree: a ``FileRecapCategory`` per bucket
(the rows the recap PDF groups by) and ``Tenant.checkin_photo_buckets`` (the
ordered list the check-in page renders and the submit path validates against).
Categories are matched case/spacing-insensitively before anything is created,
so re-running never leaves LD with both a "Table setup" and a "Table Set Up"
— two near-identical buckets in the recap PDF is the same failure mode as the
receipt that once landed under "Table setup".

Dry-run by default; ``--apply`` writes. Idempotent throughout.
"""

from __future__ import annotations

import re
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


# The four labelled dropzones on the check-in recap, in render order. Each one
# is a FileRecapCategory of LD's own, so the recap PDF can finally separate the
# table shot from the shelf shot from the receipt instead of piling all of them
# into "Sampling photos".
#
# `min` is a BA-facing nudge only — the page shows a live "3 of 8" so someone
# who's short can see it — and never blocks submit. A BA in a store parking lot
# on one bar has to be able to finish and clock out; failed photos already
# don't block, and a soft target must not be stricter than a failed upload.
PHOTO_BUCKETS: list[dict] = [
    {"name": "Table Set Up"},
    {"name": "Product Display"},
    {
        "name": "Consumer Sampling Pictures",
        "helper": "please try to upload 8+",
        "min": 8,
    },
    {"name": "Product Receipt"},
]

# Categories that back a positional upload sentinel ("1" = photos, "2" =
# receipts, see recaps.mutations). A bucket must never absorb one of these by
# renaming it: the sentinel resolves by NAME, so renaming "Sampling photos"
# would make the fallback path create a fresh "Sampling photos" beside it and
# split LD's photos across two rows. These names are matched, then left alone.
SENTINEL_CATEGORY_NAMES = ("Sampling photos", "Receipts")


def _norm(name: str | None) -> str:
    """Fold a category label for comparison — case and punctuation dropped, so
    "Table Set Up", "Table setup" and "table-setup" are recognised as one."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


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

        # -- the four labelled photo buckets ------------------------------
        bucket_plan = self._plan_photo_buckets(tenant)
        self._report_live_buckets(tenant)

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
            tenant.checkin_photo_buckets = self._ensure_photo_buckets(
                tenant, bucket_plan
            )
            tenant.save(
                update_fields=[
                    "checkin_event_type",
                    "checkin_code",
                    "checkin_training_url",
                    "checkin_photo_buckets",
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
        self.stdout.write(
            "Photo buckets : "
            + " | ".join(b["name"] for b in tenant.checkin_photo_buckets or [])
        )

    # -- photo buckets -------------------------------------------------------

    def _plan_photo_buckets(self, tenant) -> list[dict]:
        """Report LD's CURRENT categories, then decide per bucket: reuse the
        row that already plays this role, relabel it, or create a new one.

        Reporting first is the point — categories are shared with the app and
        admin recap forms, and the failure mode here is silent (a second
        near-identical bucket, or a rename that steals a sentinel's target), so
        the dry run has to show what exists before anything is written.
        """
        from recaps.models import FileRecapCategory

        existing = list(
            FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id")
        )
        self.stdout.write("")
        self.stdout.write(
            f"Categories : {len(existing)} on this tenant today"
            + ("" if existing else "  (none — all four will be created)")
        )
        for cat in existing:
            protected = " [sentinel target — never renamed]" if any(
                _norm(cat.name) == _norm(n) for n in SENTINEL_CATEGORY_NAMES
            ) else ""
            self.stdout.write(f"    [{cat.id}] {cat.name!r}{protected}")

        # Lowest id wins a tie, so a duplicate pair resolves to the older row
        # (the one history is already filed against) rather than flip-flopping.
        by_norm: dict[str, object] = {}
        dupes: list = []
        for cat in existing:
            key = _norm(cat.name)
            if key in by_norm:
                dupes.append(cat)
            else:
                by_norm[key] = cat
        for cat in dupes:
            self.stdout.write(
                self.style.WARNING(
                    f"    ! [{cat.id}] {cat.name!r} duplicates "
                    f"[{by_norm[_norm(cat.name)].id}] — leaving it alone; "
                    "the older row keeps the bucket"
                )
            )

        self.stdout.write("")
        self.stdout.write(f"Buckets    : {len(PHOTO_BUCKETS)} on the check-in recap")
        plan: list[dict] = []
        for spec in PHOTO_BUCKETS:
            name = spec["name"]
            match = by_norm.get(_norm(name))
            entry = {**spec, "category": match}
            plan.append(entry)
            hint = (
                f" (min {spec['min']}, {spec['helper']!r})" if spec.get("min") else ""
            )
            if match is None:
                self.stdout.write(f"    + {name!r} — will be CREATED{hint}")
            elif match.name == name:
                self.stdout.write(f"    = {name!r} — [{match.id}] already correct{hint}")
            else:
                self.stdout.write(
                    f"    ~ {name!r} — reusing [{match.id}] {match.name!r}, "
                    f"relabelling in place{hint}"
                )
        return plan

    def _report_live_buckets(self, tenant) -> None:
        """Read-only: what the check-in page will actually be served, and where
        recent check-in photos actually landed.

        Both halves are unverifiable from outside. The check-in link is a
        standing TENANT code, so its public payload carries no event and
        therefore no buckets until a BA identifies — there is no way to GET the
        thing the page consumes without first writing a walk-in event and a
        stub BA into the brand's live data. And nothing public reports which
        category a file ended up in, which is the one fact that distinguishes
        "four labelled dropzones" from "four dropzones that all feed one pile".

        Printing both here keeps verification read-only.
        """
        from ambassadors.checkin_web import serialize_photo_buckets
        from recaps.models import CustomRecapFile

        self.stdout.write("")
        # serialize_photo_buckets reads event.tenant, so a stand-in with the
        # right tenant is all it needs — nothing is saved.
        from events.models import Event

        buckets = serialize_photo_buckets(Event(tenant=tenant))
        self.stdout.write(f"Page will see: {len(buckets)} bucket(s)")
        for b in buckets:
            hint = f"  min {b['min']} — {b['helper']!r}" if b["min"] else ""
            self.stdout.write(f"    id={b['id']:<4} {b['name']!r}{hint}")
        missing = [
            e.get("name")
            for e in (tenant.checkin_photo_buckets or [])
            if isinstance(e, dict)
        ]
        if len(buckets) != len(missing):
            self.stdout.write(
                self.style.WARNING(
                    f"    ! {len(missing) - len(buckets)} configured bucket(s) "
                    "have no category row and are being SKIPPED"
                )
            )

        # Where check-in photos are actually landing. Scoped to the blob prefix
        # the check-in upload endpoint signs, so app/admin uploads can't muddy
        # the picture.
        recent = (
            CustomRecapFile.objects.filter(
                custom_recap__tenant_id=tenant.id,
                url__startswith="recap_files/checkin/",
            )
            .select_related("file_recap_category")
            .order_by("-id")[:60]
        )
        tally: dict[str, int] = {}
        for f in recent:
            key = (
                f.file_recap_category.name
                if f.file_recap_category
                else "(uncategorised)"
            )
            tally[key] = tally.get(key, 0) + 1
        self.stdout.write(
            f"Recent check-in photos ({sum(tally.values())} newest) by category:"
        )
        if not tally:
            self.stdout.write("    (none yet)")
        for name, count in sorted(tally.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"    {count:>4}  {name}")

    def _ensure_photo_buckets(self, tenant, plan: list[dict]) -> list[dict]:
        """Create/relabel each bucket's category and return the ordered config
        for ``Tenant.checkin_photo_buckets``.

        Relabelling in place (rather than creating a second row) keeps every
        photo already filed under the old label inside the bucket it belongs
        to — a fresh row would strand LD's history in an orphan category that
        still shows up in the recap PDF.
        """
        from recaps.models import FileRecapCategory

        creator = getattr(tenant, "created_by", None)
        config: list[dict] = []
        for entry in plan:
            name = entry["name"]
            cat = entry["category"]
            if cat is None:
                cat = FileRecapCategory.objects.create(
                    name=name, tenant_id=tenant.id, created_by=creator
                )
                self.stdout.write(f"  created  [{cat.id}] {name!r}")
            elif cat.name != name:
                old = cat.name
                cat.name = name
                cat.save(update_fields=["name", "updated_at"])
                self.stdout.write(f"  relabelled [{cat.id}] {old!r} → {name!r}")
            else:
                self.stdout.write(f"  kept     [{cat.id}] {name!r}")
            item: dict = {"name": name}
            if entry.get("helper"):
                item["helper"] = entry["helper"]
            if entry.get("min"):
                item["min"] = int(entry["min"])
            config.append(item)
        return config

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
