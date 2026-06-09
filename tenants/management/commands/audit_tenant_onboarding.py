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

READ-ONLY by default. ``--seed-file-categories --execute`` additionally creates
the missing DEFAULT_FILE_RECAP_CATEGORIES for tenants that have NONE (additive
only — it never touches existing rows or moves files; re-filing stays with
``backfill_girlbeer_receipts``).

Usage:
  python manage.py audit_tenant_onboarding                       # report
  python manage.py audit_tenant_onboarding --seed-file-categories --execute
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
            "--execute",
            action="store_true",
            help="Actually write the seeds. Default: report only.",
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
        from tenants.mutations import DEFAULT_FILE_RECAP_CATEGORIES

        seed_categories = bool(opts.get("seed_file_categories"))
        execute = bool(opts.get("execute"))
        mode = "EXECUTE (seed file categories)" if (seed_categories and execute) else "REPORT-ONLY"
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
            .select_related("file_recap_category__tenant")
        )
        foreign_recap = (
            RecapFile.objects.filter(file_recap_category__isnull=False)
            .exclude(file_recap_category__tenant_id=F("recap__event__tenant_id"))
            .select_related("file_recap_category__tenant")
        )
        tenant_by_id = {t["id"]: t for t in tenants}
        total_foreign = 0
        for kind, qs, tenant_attr in [
            ("CustomRecapFile", foreign_custom, "custom_recap__tenant_id"),
            ("RecapFile", foreign_recap, "recap__event__tenant_id"),
        ]:
            grouped = (
                qs.values(owner=F(tenant_attr))
                .annotate(n=Count("id"))
                .order_by("-n")
            )
            for row in grouped:
                total_foreign += row["n"]
                owner = tenant_by_id.get(row["owner"], {})
                self.stdout.write(
                    f"  - {kind}: {row['n']} file(s) on tenant "
                    f"{owner.get('name', '?')!r} (id={row['owner']}) sit in a "
                    "foreign category"
                )
            for f in qs.order_by("id")[:_MAX_SAMPLE]:
                cat = f.file_recap_category
                self.stdout.write(
                    f"      · {kind} id={f.id} cat={cat.name!r}"
                    f"(id={cat.id}, tenant={cat.tenant_id})"
                )
        if total_foreign == 0:
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

        # --- Optional write: seed file categories for tenants with NONE. ---
        if seed_categories:
            targets = [
                t for t in tenants if counts["file_categories"].get(t["id"], 0) == 0
            ]
            self.stdout.write(
                f"\n[seed] {len(targets)} tenant(s) with zero file categories."
            )
            for t in targets:
                if execute:
                    for name in DEFAULT_FILE_RECAP_CATEGORIES:
                        FileRecapCategory.objects.get_or_create(
                            tenant_id=t["id"], name=name
                        )
                    self.stdout.write(
                        f"  Seeded {t['name']!r} (id={t['id']}): "
                        f"{', '.join(DEFAULT_FILE_RECAP_CATEGORIES)}"
                    )
                else:
                    self.stdout.write(
                        f"  Would seed {t['name']!r} (id={t['id']}) — pass "
                        "--execute to write."
                    )

        self.stdout.write(self.style.SUCCESS("\nAudit complete."))
