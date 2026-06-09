"""Audit every tenant's onboarding seeds + cross-tenant recap-file leaks.

Born from the Girl Beer incident: the tenant was onboarded via a side path
(``onboard_girl_beer``) that skipped ``createTenant``'s seeding, so it had no
``FileRecapCategory`` rows and receipt uploads leaked into ANOTHER tenant's
"Table setup" via the resolver's old PK fallback. The resolver now self-heals
(and never resolves cross-tenant), but this audit shows where the data still
needs attention:

  1. SEED GAPS — per tenant, the row count for each per-tenant seed type
     (file categories, event/request types + statuses, rate types, types of
     good). A zero means that tenant was onboarded without that seed.
  2. FOREIGN-CATEGORY FILES — recap files whose ``file_recap_category`` belongs
     to a DIFFERENT tenant than the recap itself (the cross-tenant leak),
     grouped per recap tenant.
  3. DUPLICATE GLOBAL SKILLS — Skill is global with no unique name;
     ``createTenant`` used to re-create the whole default list on every run.

READ-ONLY by default. Optional writes (each requires ``--execute``):

  --seed-file-categories  create DEFAULT_FILE_RECAP_CATEGORIES for tenants
                          that have NONE (additive only).
  --seed-defaults         same, plus DEFAULT_RATE_TYPES / DEFAULT_TYPES_OF_GOOD
                          for tenants with zero of those (additive only).
  --rehome-foreign-files  move each cross-tenant recap file to the OWNER
                          tenant's same-NAME category (created if missing).
                          The category name — what the UI groups by — is
                          preserved exactly, so this is invisible to users;
                          it only fixes which tenant owns the row.

Never deletes anything. Re-filing by ROLE (e.g. Table setup -> Receipts)
stays with ``backfill_girlbeer_receipts``.

Usage:
  python manage.py audit_tenant_onboarding                       # report
  python manage.py audit_tenant_onboarding --seed-defaults --rehome-foreign-files --execute
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.db.models import Count, F

logger = logging.getLogger(__name__)

_MAX_SAMPLE = 10


class Command(BaseCommand):
    help = (
        "Report per-tenant onboarding seed gaps, cross-tenant recap-file "
        "leaks, and duplicate global skills. READ-ONLY unless "
        "--seed-file-categories --execute."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--seed-file-categories",
            action="store_true",
            help=(
                "With --execute: create the default file categories for "
                "tenants that have NONE (additive only)."
            ),
        )
        parser.add_argument(
            "--seed-defaults",
            action="store_true",
            help=(
                "With --execute: seed file categories AND rate types / types "
                "of good for tenants that have ZERO of that type (additive)."
            ),
        )
        parser.add_argument(
            "--rehome-foreign-files",
            action="store_true",
            help=(
                "With --execute: move each cross-tenant recap file to the "
                "owner tenant's same-name category (created if missing)."
            ),
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually write. Default: report only.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import Skill
        from events.models import EventStatus, EventType, RequestStatus, RequestType
        from jobs.models import RateType
        from recaps.models import (
            CustomRecapFile,
            FileRecapCategory,
            RecapFile,
            TypeOfGood,
        )
        from tenants.models import Tenant
        from tenants.mutations import (
            DEFAULT_FILE_RECAP_CATEGORIES,
            DEFAULT_RATE_TYPES,
            DEFAULT_TYPES_OF_GOOD,
        )

        seed_defaults = bool(opts.get("seed_defaults"))
        seed_categories = bool(opts.get("seed_file_categories")) or seed_defaults
        rehome = bool(opts.get("rehome_foreign_files"))
        execute = bool(opts.get("execute"))
        actions = [
            label
            for flag, label in [
                (seed_defaults, "seed defaults"),
                (seed_categories and not seed_defaults, "seed file categories"),
                (rehome, "rehome foreign files"),
            ]
            if flag
        ]
        mode = (
            f"EXECUTE ({', '.join(actions)})"
            if (actions and execute)
            else "REPORT-ONLY"
        )
        self.stdout.write(f"Tenant onboarding audit — mode={mode}.")

        tenants = list(Tenant.objects.order_by("id").values("id", "name", "slug"))

        # --- 1. Seed gaps: per-tenant counts of each per-tenant seed type. ---
        seed_models = [
            ("file_categories", FileRecapCategory),
            ("event_types", EventType),
            ("event_statuses", EventStatus),
            ("request_types", RequestType),
            ("request_statuses", RequestStatus),
            ("rate_types", RateType),
            ("types_of_good", TypeOfGood),
        ]
        counts: dict[str, dict[int, int]] = {}
        for label, model in seed_models:
            counts[label] = {
                row["tenant_id"]: row["n"]
                for row in model.objects.values("tenant_id").annotate(n=Count("id"))
            }

        self.stdout.write(f"\n[1] Seed gaps across {len(tenants)} tenant(s):")
        gap_tenants: list[dict] = []
        for t in tenants:
            zeros = [
                label for label, _ in seed_models if counts[label].get(t["id"], 0) == 0
            ]
            if zeros:
                gap_tenants.append(t)
                per = ", ".join(
                    f"{label}={counts[label].get(t['id'], 0)}"
                    for label, _ in seed_models
                )
                self.stdout.write(
                    f"  - {t['name']!r} (slug={t['slug']}, id={t['id']}): "
                    f"MISSING {', '.join(zeros)}  [{per}]"
                )
        if not gap_tenants:
            self.stdout.write("  All tenants have every seed type. ✔")

        # --- 2. Foreign-category recap files (the cross-tenant leak). ---
        self.stdout.write("\n[2] Recap files in another tenant's category:")
        foreign_custom = (
            CustomRecapFile.objects.filter(file_recap_category__isnull=False)
            .exclude(file_recap_category__tenant_id=F("custom_recap__tenant_id"))
            .annotate(owner_tenant_id=F("custom_recap__tenant_id"))
            .select_related("file_recap_category__tenant")
        )
        foreign_recap = (
            RecapFile.objects.filter(file_recap_category__isnull=False)
            .exclude(file_recap_category__tenant_id=F("recap__event__tenant_id"))
            .annotate(owner_tenant_id=F("recap__event__tenant_id"))
            .select_related("file_recap_category__tenant")
        )
        tenant_by_id = {t["id"]: t for t in tenants}
        # Materialize so the optional rehome pass below acts on exactly what
        # was reported (counts are tiny in practice — single digits).
        foreign_rows: list[tuple[str, object]] = [
            ("CustomRecapFile", f) for f in foreign_custom.order_by("id")
        ] + [("RecapFile", f) for f in foreign_recap.order_by("id")]
        per_owner: dict[tuple[str, int | None], int] = {}
        for kind, f in foreign_rows:
            key = (kind, f.owner_tenant_id)
            per_owner[key] = per_owner.get(key, 0) + 1
        for (kind, owner_id), n in sorted(per_owner.items(), key=lambda kv: -kv[1]):
            owner = tenant_by_id.get(owner_id, {})
            self.stdout.write(
                f"  - {kind}: {n} file(s) on tenant "
                f"{owner.get('name', '?')!r} (id={owner_id}) sit in a "
                "foreign category"
            )
        for kind, f in foreign_rows[:_MAX_SAMPLE]:
            cat = f.file_recap_category
            self.stdout.write(
                f"      · {kind} id={f.id} cat={cat.name!r}"
                f"(id={cat.id}, tenant={cat.tenant_id})"
            )
        if not foreign_rows:
            self.stdout.write("  No cross-tenant recap files. ✔")

        # --- 3. Duplicate global skills. ---
        self.stdout.write("\n[3] Duplicate global skills:")
        dupes = (
            Skill.objects.values("name")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
            .order_by("-n")
        )
        if dupes:
            for row in dupes:
                self.stdout.write(f"  - {row['name']!r}: {row['n']} copies")
            self.stdout.write(
                "  (Report-only: deduping needs an FK/M2M reference check "
                "before deleting rows.)"
            )
        else:
            self.stdout.write("  No duplicate skill names. ✔")

        # --- Optional write: seed defaults for tenants with ZERO of a type.
        #     RateType.created_by is NOT NULL, so those rows are attributed to
        #     the first superuser; FileRecapCategory/TypeOfGood allow null. ---
        from django.contrib.auth import get_user_model

        fallback_user = (
            get_user_model().objects.filter(is_superuser=True).order_by("id").first()
        )
        seed_plans = [
            ("file_categories", FileRecapCategory, DEFAULT_FILE_RECAP_CATEGORIES, False)
        ]
        if seed_defaults:
            seed_plans += [
                ("rate_types", RateType, DEFAULT_RATE_TYPES, True),
                ("types_of_good", TypeOfGood, DEFAULT_TYPES_OF_GOOD, False),
            ]
        if seed_categories:
            for label, model, defaults, needs_user in seed_plans:
                targets = [t for t in tenants if counts[label].get(t["id"], 0) == 0]
                if not targets:
                    continue
                if needs_user and fallback_user is None:
                    self.stdout.write(
                        f"\n[seed:{label}] SKIPPED — created_by is required "
                        "and no superuser exists to attribute it to."
                    )
                    continue
                create_kwargs = (
                    {"created_by": fallback_user} if needs_user else {}
                )
                self.stdout.write(
                    f"\n[seed:{label}] {len(targets)} tenant(s) with zero {label}."
                )
                for t in targets:
                    if execute:
                        for name in defaults:
                            model.objects.get_or_create(
                                tenant_id=t["id"],
                                name=name,
                                defaults=create_kwargs,
                            )
                        self.stdout.write(
                            f"  Seeded {t['name']!r} (id={t['id']}): "
                            f"{', '.join(defaults)}"
                        )
                    else:
                        self.stdout.write(
                            f"  Would seed {t['name']!r} (id={t['id']}): "
                            f"{', '.join(defaults)} — pass --execute to write."
                        )

        # --- Optional write: re-home foreign-category files. The category
        #     NAME (what the UI groups by) is preserved exactly; only the
        #     owning tenant of the category row changes. Never deletes. ---
        if rehome and foreign_rows:
            self.stdout.write(f"\n[rehome] {len(foreign_rows)} foreign file(s):")
            moved = 0
            for kind, f in foreign_rows:
                cat = f.file_recap_category
                owner_id = f.owner_tenant_id
                if owner_id is None:
                    self.stdout.write(
                        f"  ! {kind} id={f.id}: recap has no tenant — skipped."
                    )
                    continue
                if execute:
                    own_cat, _created = FileRecapCategory.objects.get_or_create(
                        tenant_id=owner_id, name=cat.name
                    )
                    f.file_recap_category = own_cat
                    f.save(update_fields=["file_recap_category", "updated_at"])
                    moved += 1
                    self.stdout.write(
                        f"  Moved {kind} id={f.id}: {cat.name!r}"
                        f"(tenant={cat.tenant_id}) -> same name on tenant "
                        f"{owner_id} (cat id={own_cat.id})."
                    )
                else:
                    self.stdout.write(
                        f"  Would move {kind} id={f.id}: {cat.name!r}"
                        f"(tenant={cat.tenant_id}) -> same name on tenant "
                        f"{owner_id}."
                    )
            if execute:
                self.stdout.write(f"  Re-homed {moved} file(s).")

        self.stdout.write(self.style.SUCCESS("\nAudit complete."))
