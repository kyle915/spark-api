"""Make Liquid Death's ONE standing check-in link serve their programs.

(Name kept for its live wiring — the `/internal/cron/setup-ld-retail-checkin`
endpoint and the "Setup LD retail check-in" workflow both point at it. It now
covers Retail Sampling, Event Activation, and Product Seeding.)

LD ALREADY HAS the Retail / Event recap forms — "Liquid Death-Retail Sampling"
and "Liquid Death-Event Activation". Product Seeding is seeded separately by
``seed_ld_product_seeding_recap_template`` (this command creates NONE of the
templates). Seeding a duplicate would split the brand's recaps across two forms
and halve every dashboard number; the Feel Free near-miss is written up in
`setup_feel_free_checkin`. What's missing is only:

1. a standing ``LD-`` check-in code on the tenant,
2. the event types made SELECTABLE on that one link, with the retail pin kept
   as the fallback for a request that names no program,
3. a "Products Sampled" / Cases-by-SKU multi-select on each program's template,
   and
4. the labelled PHOTO BUCKETS the recap step uploads into, per program.

(2) is why this exists. A second check-in link per program looks like the
obvious answer and is a trap: ``Tenant.checkin_code`` is a single column, so
minting the second silently repoints the first and every BA holding the old URL
lands on the wrong program. Making the program a question on ONE link avoids
that — the answer is stamped on the event, and `resolve_template_for_event`
picks the form by ``event_type_id``, machinery that already worked. Before any
of this the walk-in path used the tenant's LOWEST-ID event type, so a retail BA
could be handed the 7-field activation form and nobody would notice, because
the recap still submits fine.

(4) is a pair of writes that have to agree: a ``FileRecapCategory`` per bucket
(the rows the recap PDF groups by) and ``Tenant.checkin_photo_buckets`` (the
per-program lists the check-in page renders and the submit path validates
against). Categories are matched case/spacing-insensitively before anything is
created, so re-running never leaves LD with both a "Table setup" and a "Table
Set Up" — two near-identical buckets in the recap PDF is the same failure mode
as the receipt that once landed under "Table setup". A bucket BOTH programs want
("Consumer Sampling Pictures") is ONE row referenced by both lists: a recap
belongs to one event and therefore one program, so a shared row is never
ambiguous, and splitting it would fragment LD's photo history.

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

PRODUCTS_SECTION = "Products Sampled"
PRODUCTS_FIELD = "Products Sampled"

# The brand's full SKU list, exactly as it reads on the LD request form
# (/spark-form/ighn-liquid-death) so the two surfaces can't drift. Category
# prefixes keep the options scannable on a phone — a flat alphabetical list of
# horror-pun names is unreadable at arm's length in a store aisle.
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
            "Feastables Peanut Butter Cup",
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
    """Hardcoded spark-form SKUs as ``"Category — Name"`` choice values.

    Prefer :func:`product_options_for_tenant` at seed/runtime so live Product
    catalog rows win; this list is only the bootstrap fallback when the
    tenant has no catalog yet (and the Event Confirmation LD picker).
    """
    return [f"{cat} — {name}" for cat, names in PRODUCTS for name in names]


def product_options_for_tenant(tenant) -> list[str]:
    """Products Sampled choices: live catalog first, hardcoded list if empty.

    GraphQL also resolves Products Sampled from the catalog at read time, so
    a SKU added in /products appears on the pills without re-seeding. Seeding
    still writes the list onto ``CustomField.options`` as a cache/fallback.
    """
    from events.event_confirmations import catalog_product_options

    live = catalog_product_options(tenant)
    return live if live else product_options()


# Kyle's shot list, per program, in render order. Each entry is one labelled
# dropzone backed by a FileRecapCategory of LD's own, so the recap PDF can
# finally separate the table shot from the shelf shot from the receipt instead
# of piling all of them into "Sampling photos".
#
# The two lists differ because the required shots are a property of the PROGRAM,
# not the brand: a retail demo has a table and a shelf to photograph, an
# activation has neither and does have parking to expense. "Consumer Sampling
# Pictures" appears in both and is ONE shared category row.
#
# `min` is a BA-facing nudge only — the page shows a live "3 of 8" so someone
# who's short can see it — and never blocks submit. A BA in a store parking lot
# on one bar has to be able to finish and clock out; failed photos already
# don't block, and a soft target must not be stricter than a failed upload.
CONSUMER_SAMPLING: dict = {
    "name": "Consumer Sampling Pictures",
    "helper": "please try to upload 8+",
    "min": 8,
}

# The programs LD runs off one link. Each names the event type to match, the
# existing template to extend, and its own dropzones.
#
# The FIRST entry is the default: what the link stamps when a request carries no
# program (an old page, a curl). Retail is the higher-volume program, so a
# fallback that lands there is the less wrong of the three.
#
# Product Seeding template name must stay free of "event"/"activation"/etc. so
# Recaps list activation chips leave Product Seeding under its own
# ``seeding`` filter (never Retail / Event / CONV).
PROGRAMS: list[dict] = [
    {
        "event_type": "retail sampling",
        "template": "retail sampling",
        "photos": [
            {"name": "Table Set Up"},
            {"name": "Product Display"},
            CONSUMER_SAMPLING,
            {"name": "Product Receipt"},
        ],
    },
    {
        "event_type": "event activation",
        "template": "event activation",
        "photos": [
            {"name": "Activation Set Up"},
            CONSUMER_SAMPLING,
            {"name": "Expense Receipts (Parking)"},
        ],
    },
    {
        "event_type": "product seeding",
        "template": "product seeding",
        "photos": [
            {"name": "Drop-off Placement"},
            {"name": "Product on Display"},
            {"name": "Delivery Receipt"},
        ],
        # Cases-by-SKU field is seeded on the Product Seeding template itself;
        # skip adding a second "Products Sampled" row onto that form.
        "skip_products_field": True,
    },
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
        "Make Liquid Death's standing check-in link serve Retail Sampling, "
        "Event Activation, and Product Seeding: selectable event types, "
        "per-program photo buckets, and the Products Sampled picker on "
        "sampling templates (dry-run default)."
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

        etypes = list(EventType.objects.filter(tenant=tenant).order_by("id"))
        if not etypes:
            raise CommandError(f"Tenant {tenant.slug!r} has no event types.")
        self.stdout.write(
            "Event types: " + ", ".join(f"[{e.id}] {e.name}" for e in etypes)
        )
        templates = list(
            CustomRecapTemplate.objects.filter(tenant=tenant).order_by("id")
        )
        self.stdout.write(
            "Templates  : "
            + (", ".join(f"[{t.id}] {t.name!r}" for t in templates) or "(none)")
        )

        # -- resolve each program: its event type and its template ---------
        from recaps.models import CustomField

        plan: list[dict] = []
        for spec in PROGRAMS:
            etype = next(
                (e for e in etypes if spec["event_type"] in (e.name or "").lower()),
                None,
            )
            if etype is None:
                raise CommandError(
                    f"No event type on {tenant.slug!r} matches "
                    f"{spec['event_type']!r} — refusing to guess which form the "
                    "link should open for that program."
                )
            template = None
            if not skip_products:
                template = next(
                    (
                        t
                        for t in templates
                        if t.event_type_id == etype.id
                        or spec["template"] in (t.name or "").lower()
                    ),
                    None,
                )
                if template is None:
                    raise CommandError(
                        f"No existing template for {etype.name!r}. This command "
                        "deliberately does NOT create one — check the list above."
                    )
            plan.append(
                {
                    "type": etype,
                    "template": template,
                    "photos": spec["photos"],
                    "skip_products_field": bool(
                        spec.get("skip_products_field")
                    ),
                }
            )

        self.stdout.write("")
        self.stdout.write("Selectable on the link (in this order):")
        for i, entry in enumerate(plan):
            etype = entry["type"]
            self.stdout.write(
                f"  [{etype.id}] {etype.name!r}"
                + ("   ← pinned default when a request names no program" if i == 0 else "")
            )
            tpl = entry["template"]
            if tpl is not None:
                names = {
                    (f.name or "").strip().lower()
                    for f in CustomField.objects.filter(custom_recap_template=tpl)
                }
                if entry.get("skip_products_field"):
                    sku_state = (
                        "Cases Dropped by SKU already on form "
                        "(seed_ld_product_seeding owns this field)"
                        if "cases dropped by sku" in names
                        or PRODUCTS_FIELD.strip().lower() in names
                        else "SKU field expected from seed_ld_product_seeding "
                        "— run that seeder first"
                    )
                    ps = (
                        "product_samples ON"
                        if tpl.product_samples
                        else "product_samples will be ENABLED"
                    )
                    self.stdout.write(
                        f"        form: [{tpl.id}] {tpl.name!r} "
                        f"({len(names)} fields); {sku_state}; {ps}"
                    )
                else:
                    state = (
                        "ALREADY PRESENT — options refreshed"
                        if PRODUCTS_FIELD.strip().lower() in names
                        else "will be ADDED"
                    )
                    ps = (
                        "product_samples ON"
                        if tpl.product_samples
                        else "product_samples will be ENABLED"
                    )
                    self.stdout.write(
                        f"        form: [{tpl.id}] {tpl.name!r} "
                        f"({len(names)} fields); "
                        f"{PRODUCTS_FIELD!r} {state}; {ps}"
                    )
        if etypes[0].id != plan[0]["type"].id:
            self.stdout.write(
                self.style.WARNING(
                    f"  (without the pin the fallback would be "
                    f"[{etypes[0].id}] {etypes[0].name!r} — the wrong form)"
                )
            )

        opts_list = product_options_for_tenant(tenant)
        src = (
            "tenant Product catalog"
            if opts_list != product_options()
            else "hardcoded spark-form list (catalog empty)"
        )
        self.stdout.write(f"Products   : {len(opts_list)} options ({src})")

        # -- the labelled photo buckets, per program -----------------------
        bucket_plan = self._plan_photo_buckets(tenant, plan)
        self._report_live_buckets(tenant, plan)

        if not apply:
            self.stdout.write("")
            for o in opts_list:
                self.stdout.write(f"    · {o}")
            self.stdout.write("")
            self.stdout.write("DRY RUN — re-run with --apply to write.")
            return

        with transaction.atomic():
            # The pin stays: it's the fallback for a request that carries no
            # program, and falling back to retail beats the lowest-id type.
            tenant.checkin_event_type = plan[0]["type"]
            if forced_code:
                tenant.checkin_code = forced_code
            elif not tenant.checkin_code:
                tenant.checkin_code = self._mint_code()
            if training_url:
                tenant.checkin_training_url = training_url
            tenant.checkin_photo_buckets = self._ensure_photo_buckets(
                tenant, plan, bucket_plan
            )
            tenant.save(
                update_fields=[
                    "checkin_event_type",
                    "checkin_code",
                    "checkin_training_url",
                    "checkin_photo_buckets",
                ]
            )
            # `set`, not `add` — this list is the whole truth, so dropping a
            # program from PROGRAMS also takes it off the link.
            tenant.checkin_event_types.set([e["type"] for e in plan])
            self.stdout.write(
                "  selectable = "
                + ", ".join(f"[{e['type'].id}] {e['type'].name}" for e in plan)
            )

            for entry in plan:
                if entry["template"] is not None:
                    if not entry.get("skip_products_field"):
                        self._ensure_products_field(
                            entry["template"], tenant, opts_list
                        )
                    self._ensure_product_samples_flag(entry["template"])
                    if entry.get("skip_products_field"):
                        # Refresh Cases Dropped by SKU options from catalog.
                        self._refresh_cases_by_sku_options(
                            entry["template"], tenant, opts_list
                        )
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Check-in code : {tenant.checkin_code}")
        )
        self.stdout.write(
            f"Link          : https://client.igniteproductions.co/checkin/"
            f"{tenant.checkin_code}"
        )
        if tenant.checkin_training_url:
            self.stdout.write(f"Training link : {tenant.checkin_training_url}")
        for key, entries in (tenant.checkin_photo_buckets or {}).items():
            self.stdout.write(
                f"Photo buckets : {key} — "
                + " | ".join(b["name"] for b in entries)
            )

    # -- photo buckets -------------------------------------------------------

    def _plan_photo_buckets(self, tenant, programs: list[dict]) -> dict:
        """Report LD's CURRENT categories, then decide per bucket NAME: reuse the
        row that already plays this role, relabel it, or create a new one.

        Keyed by normalized bucket name across ALL programs, so a bucket both
        programs want ("Consumer Sampling Pictures") is planned — and created —
        exactly once and both lists point at the same row.

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
            + ("" if existing else "  (none — every bucket will be created)")
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
        plan: dict = {}
        for program in programs:
            self.stdout.write(
                f"Buckets    : {program['type'].name} — {len(program['photos'])} "
                "dropzone(s)"
            )
            for spec in program["photos"]:
                name = spec["name"]
                key = _norm(name)
                hint = (
                    f" (min {spec['min']}, {spec['helper']!r})"
                    if spec.get("min")
                    else ""
                )
                if key in plan:
                    self.stdout.write(
                        f"    ⇄ {name!r} — shared with an earlier program, "
                        f"one row{hint}"
                    )
                    continue
                match = by_norm.get(key)
                plan[key] = {**spec, "category": match}
                if match is None:
                    self.stdout.write(f"    + {name!r} — will be CREATED{hint}")
                elif match.name == name:
                    self.stdout.write(
                        f"    = {name!r} — [{match.id}] already correct{hint}"
                    )
                else:
                    self.stdout.write(
                        f"    ~ {name!r} — reusing [{match.id}] {match.name!r}, "
                        f"relabelling in place{hint}"
                    )
        return plan

    def _report_live_buckets(self, tenant, programs: list[dict]) -> None:
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
        from ambassadors.checkin_web import (
            photo_bucket_specs,
            serialize_photo_buckets,
        )
        from recaps.models import CustomRecapFile

        self.stdout.write("")
        # serialize_photo_buckets reads event.tenant + event.event_type, so a
        # stand-in carrying both is all it needs — nothing is saved. Per program,
        # because that is the grain the page is served at.
        from events.models import Event

        for program in programs:
            etype = program["type"]
            buckets = serialize_photo_buckets(Event(tenant=tenant, event_type=etype))
            self.stdout.write(
                f"Page will see ({etype.name}): {len(buckets)} bucket(s)"
            )
            for b in buckets:
                hint = f"  min {b['min']} — {b['helper']!r}" if b["min"] else ""
                self.stdout.write(f"    id={b['id']:<4} {b['name']!r}{hint}")
            configured = [
                e.get("name")
                for e in photo_bucket_specs(tenant, etype)
                if isinstance(e, dict)
            ]
            if len(buckets) != len(configured):
                self.stdout.write(
                    self.style.WARNING(
                        f"    ! {len(configured) - len(buckets)} configured "
                        "bucket(s) have no category row and are being SKIPPED"
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

    def _ensure_photo_buckets(
        self, tenant, programs: list[dict], plan: dict
    ) -> dict:
        """Create/relabel each bucket's category once, then return the config
        for ``Tenant.checkin_photo_buckets`` keyed by event type name.

        Relabelling in place (rather than creating a second row) keeps every
        photo already filed under the old label inside the bucket it belongs
        to — a fresh row would strand LD's history in an orphan category that
        still shows up in the recap PDF.

        Keyed by event type NAME rather than id so the stored config is readable
        and survives a re-seed in another environment, matching how bucket names
        already resolve to categories.
        """
        from recaps.models import FileRecapCategory

        creator = getattr(tenant, "created_by", None)
        # One pass over the deduped plan, so a bucket two programs share is
        # written once and both lists resolve to the same row.
        for entry in plan.values():
            name = entry["name"]
            cat = entry["category"]
            if cat is None:
                cat = FileRecapCategory.objects.create(
                    name=name, tenant_id=tenant.id, created_by=creator
                )
                entry["category"] = cat
                self.stdout.write(f"  created  [{cat.id}] {name!r}")
            elif cat.name != name:
                old = cat.name
                cat.name = name
                cat.save(update_fields=["name", "updated_at"])
                self.stdout.write(f"  relabelled [{cat.id}] {old!r} → {name!r}")
            else:
                self.stdout.write(f"  kept     [{cat.id}] {name!r}")

        config: dict = {}
        for program in programs:
            entries: list[dict] = []
            for spec in program["photos"]:
                item: dict = {"name": spec["name"]}
                if spec.get("helper"):
                    item["helper"] = spec["helper"]
                if spec.get("min"):
                    item["min"] = int(spec["min"])
                entries.append(item)
            config[program["type"].name] = entries
        return config

    # -- writes ------------------------------------------------------------

    def _refresh_cases_by_sku_options(
        self, template, tenant, options: list[str]
    ) -> None:
        """Refresh Product Seeding Cases Dropped by SKU option cache in place."""
        from recaps.models import CustomField
        from recaps.products_sampled import is_products_sampled_field

        options = product_options_for_tenant(tenant) or list(options)
        field = next(
            (
                f
                for f in CustomField.objects.filter(custom_recap_template=template)
                if is_products_sampled_field(f.name)
            ),
            None,
        )
        if field is None:
            self.stdout.write(
                self.style.WARNING(
                    f"  no Cases/Products Sampled field on [{template.id}] "
                    f"{template.name!r} — run seed_ld_product_seeding first"
                )
            )
            return
        field.options = options
        field.save(update_fields=["options"])
        self.stdout.write(
            f"  refreshed [{field.id}] {field.name!r} → {len(options)} options"
        )

    def _ensure_product_samples_flag(self, template) -> None:
        """Turn on structured CustomRecapProductSample capture for the template.

        BA pills still collect the SKU list; can counts ride ``productSamples``
        into ``CustomRecapProductSample`` so Spark's Product Samples grid
        aggregates the same numbers admins edit by hand.
        """
        if template.product_samples is True:
            self.stdout.write(
                f"  product_samples already on [{template.id}] {template.name!r}"
            )
            return
        template.product_samples = True
        template.save(update_fields=["product_samples", "updated_at"])
        self.stdout.write(
            f"  enabled product_samples on [{template.id}] {template.name!r}"
        )

    def _ensure_products_field(self, template, tenant, options: list[str]) -> None:
        """Add (or refresh) the Products Sampled multi-select, in place.

        Re-running rewrites the cached ``options`` from the catalog (or the
        hardcoded fallback). GraphQL prefers live Product rows at read time,
        so admin catalog adds appear on pills without this refresh — seeding
        keeps the JSON cache aligned for dumps and catalog-empty tenants.
        Never duplicates the field or disturbs recaps already filed against it.
        """
        from recaps.models import CustomField, CustomRecapFieldType, RecapSection

        creator = getattr(template, "created_by", None) or getattr(
            tenant, "created_by", None
        )
        # Prefer the live catalog at write time too (caller may have passed
        # a stale hardcoded list).
        options = product_options_for_tenant(tenant) or list(options)

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
