"""Seed (or purge) the FAMUS · Alcohol Armor · Wook Water demo activations.

    python manage.py seed_brand_group_demo                 # dry-run seed
    python manage.py seed_brand_group_demo --apply         # create what's missing
    python manage.py seed_brand_group_demo --purge         # dry-run purge
    python manage.py seed_brand_group_demo --purge --apply # delete all demo rows

Sends nothing (no email / push / Sheets / calendar / job board). See
``tenants.brand_group_demo`` for what gets created and how it's tagged.
Purge before inviting real client contacts to these brands.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tenants.brand_group_demo import DemoSetupError, purge, seed


class Command(BaseCommand):
    help = "Seed or purge the brand-group demo recaps (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Write (default: dry-run)."
        )
        parser.add_argument(
            "--purge",
            action="store_true",
            help="Remove every demo row instead of seeding.",
        )

    def handle(self, *args, **options):
        apply = bool(options.get("apply"))
        mode = "purge" if options.get("purge") else "seed"
        self.stdout.write(f"seed_brand_group_demo mode={mode} apply={apply}")
        try:
            if mode == "purge":
                purge(apply=apply, out=self.stdout)
            else:
                seed(apply=apply, out=self.stdout)
        except DemoSetupError as exc:
            raise CommandError(str(exc)) from exc
        if not apply:
            self.stdout.write("DRY RUN — nothing written. Re-run with --apply.")
