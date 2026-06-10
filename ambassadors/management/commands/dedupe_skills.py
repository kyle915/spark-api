"""Dedupe the global Skill table (case-insensitive by name).

``createTenant`` used to re-create the whole DEFAULT_SKILLS list on every
tenant creation (Skill is global with no unique name), leaving ~9 copies of
each default. The create-side guard shipped in #772; this removes the
existing duplicates:

  1. Group skills by lower(name). In each group the LOWEST id is the keeper.
  2. Repoint every AmbassadorSkill row from a duplicate to the keeper —
     unless that ambassador already has the keeper, in which case the
     duplicate LINK row is deleted instead (no double-links).
  3. Delete the duplicate Skill rows (now reference-free).

AmbassadorSkill is the ONLY relation pointing at Skill (verified via
``Skill._meta.related_objects``); a runtime assertion below re-checks that so
a future FK can't be silently orphaned by this command.

DRY-RUN by default — prints the full plan. ``--execute`` applies it inside a
transaction. Idempotent: a clean table is a no-op.

Usage:
  python manage.py dedupe_skills             # report the plan
  python manage.py dedupe_skills --execute   # apply
"""

from __future__ import annotations

import logging
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Merge duplicate global Skill rows (case-insensitive name): repoint "
        "AmbassadorSkill links to the lowest-id keeper, then delete the "
        "duplicates. DRY-RUN by default; pass --execute to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually repoint links and delete duplicate skills.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorSkill, Skill

        execute = bool(opts.get("execute"))
        self.stdout.write(
            f"Skill dedupe — mode={'EXECUTE' if execute else 'DRY-RUN'}."
        )

        # Safety: this command only knows how to repoint AmbassadorSkill. If
        # a new relation to Skill ever appears, refuse to run rather than
        # delete rows something still points at.
        rels = {
            rel.related_model.__name__ for rel in Skill._meta.related_objects
        }
        if rels != {"AmbassadorSkill"}:
            self.stderr.write(
                f"Unexpected relations to Skill: {sorted(rels)} — update "
                "dedupe_skills to handle them before running."
            )
            return

        groups: dict[str, list] = defaultdict(list)
        for skill in Skill.objects.order_by("id"):
            groups[(skill.name or "").strip().lower()].append(skill)

        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
        if not dup_groups:
            self.stdout.write("No duplicate skill names. Nothing to do. ✔")
            return

        total_dups = sum(len(v) - 1 for v in dup_groups.values())
        self.stdout.write(
            f"{len(dup_groups)} name(s) with duplicates — "
            f"{total_dups} row(s) to remove:"
        )

        repointed = relinked_dropped = deleted = 0
        with transaction.atomic():
            for name, skills in sorted(dup_groups.items()):
                keeper, dups = skills[0], skills[1:]
                links = AmbassadorSkill.objects.filter(
                    skill_id__in=[d.id for d in dups]
                )
                link_count = links.count()
                self.stdout.write(
                    f"  - {keeper.name!r}: keep id={keeper.id}, remove "
                    f"{len(dups)} dup(s) "
                    f"(ids={[d.id for d in dups]}, {link_count} BA link(s) "
                    "to migrate)"
                )
                if not execute:
                    continue
                for link in links.select_related(None):
                    already = AmbassadorSkill.objects.filter(
                        ambassador_id=link.ambassador_id, skill_id=keeper.id
                    ).exists()
                    if already:
                        link.delete()
                        relinked_dropped += 1
                    else:
                        link.skill_id = keeper.id
                        link.save(update_fields=["skill_id", "updated_at"])
                        repointed += 1
                for d in dups:
                    d.delete()
                    deleted += 1

        if execute:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Repointed {repointed} link(s), dropped "
                    f"{relinked_dropped} redundant link(s), deleted "
                    f"{deleted} duplicate skill row(s)."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"DRY-RUN: would remove {total_dups} duplicate row(s). "
                    "Re-run with --execute."
                )
            )
