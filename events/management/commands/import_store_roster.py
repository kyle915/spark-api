"""Preload a client store roster as Retailer accounts on each brand it lists.

The roster CSV lives in ``events/management/commands/data`` and carries one
row per store with a ``brands`` column of pipe-separated tenant slugs
(``famus-energy|wook-water``). A store that carries several brands becomes one
Retailer per brand — Retailer is tenant-scoped — so each brand's request form
and walk-up list it, and a multi-brand activation there resolves on every
brand.

Accounts land as regional (``is_national=False``) stores linked to the city
``Location`` resolved by the shared address parser (``events.market``); a
city missing from the catalog is added. Re-runs are idempotent: a store is
matched on (tenant, normalized address) and never duplicated.

SAFE: dry-run by default; ``--apply`` writes.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ambassadors.checkin_web import normalize_place
from events.market import PreloadedGeoLookup, resolve_geo_for_address
from events.models import Location, Retailer
from tenants.models import Tenant, User

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_FILE = "famus_aa_wook_store_roster.csv"
REQUIRED_COLUMNS = {"roster_no", "store_name", "address", "city", "state", "zip", "brands"}


def resolve_tenant(slug: str) -> Tenant | None:
    return (
        Tenant.objects.filter(slug=slug).order_by("id").first()
        or Tenant.objects.filter(request_url_name=slug).order_by("id").first()
    )


def read_roster(filename: str) -> list[dict[str, str]]:
    if Path(filename).name != filename:
        raise CommandError("--file must be a file name inside events/management/commands/data")
    path = DATA_DIR / filename
    if not path.exists():
        raise CommandError(f"roster not found: {filename}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise CommandError(f"roster is missing columns: {', '.join(sorted(missing))}")
        return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


class Command(BaseCommand):
    help = (
        "Preload a store roster CSV as Retailer accounts on each listed brand. "
        "Idempotent; dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=DEFAULT_FILE, help="Roster CSV name in events/management/commands/data.")
        parser.add_argument("--owner-email", required=True, help="Spark admin recorded as created_by.")
        parser.add_argument("--tenant", default="", help="Only import this brand slug (default: every brand in the file).")
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        w = self.stdout.write

        owner = User.objects.filter(email__iexact=opts["owner_email"].strip()).first()
        if owner is None:
            raise CommandError(f"owner not found: {opts['owner_email']}")

        rows = read_roster(opts["file"])
        only = opts["tenant"].strip()
        slugs = sorted({s for row in rows for s in row["brands"].split("|") if s})
        if only:
            if only not in slugs:
                raise CommandError(f"{only!r} is not a brand in {opts['file']} ({', '.join(slugs)})")
            slugs = [only]

        tenants: dict[str, Tenant] = {}
        for slug in slugs:
            tenant = resolve_tenant(slug)
            if tenant is None:
                raise CommandError(f"tenant-not-found: {slug}")
            tenants[slug] = tenant

        w(f"{'APPLY' if apply else 'DRY-RUN'} import {opts['file']}: {len(rows)} stores")
        for slug, tenant in tenants.items():
            w(f"  {slug} -> {tenant.name} (id={tenant.id})")

        lookup = PreloadedGeoLookup()
        new_locations: dict[tuple[str, int], Location | None] = {}
        existing: dict[int, dict[str, Retailer]] = {
            t.id: {
                normalize_place(r.address): r
                for r in Retailer.objects.filter(tenant=t).exclude(address__isnull=True).exclude(address="")
            }
            for t in tenants.values()
        }
        counts: dict[str, Counter] = defaultdict(Counter)
        skipped: list[str] = []
        flagged: list[str] = []
        shared = 0

        with transaction.atomic():
            for row in rows:
                brands = [s for s in row["brands"].split("|") if s in tenants]
                if not brands:
                    continue
                label = f"#{row['roster_no']} {row['store_name']} — {row['address']}"
                key = normalize_place(row["address"])
                resolution = resolve_geo_for_address(row["address"], lookup)
                if not row["store_name"] or not key or resolution is None:
                    skipped.append(f"{label}: no parseable US address")
                    continue
                if len(brands) > 1:
                    shared += 1
                if row.get("maps_match") and row["maps_match"] != "Verified":
                    flagged.append(f"{label}: Google Maps {row['maps_match'].lower()}")

                location_id = resolution.location_id
                if location_id is None:
                    city = resolution.geo.city or row["city"]
                    loc_key = (city.lower(), resolution.state_id)
                    if loc_key not in new_locations:
                        counts["_locations"]["created"] += 1
                        w(f"  + city {city}, {row['state']} {row['zip']} (not in Location catalog)")
                        new_locations[loc_key] = (
                            Location.objects.create(
                                name=city[:50],
                                code=row["state"],
                                zip=row["zip"],
                                state_id=resolution.state_id,
                                created_by=owner,
                            )
                            if apply
                            else None
                        )
                    loc = new_locations[loc_key]
                    location_id = loc.id if loc is not None else None

                for slug in brands:
                    tenant = tenants[slug]
                    found = existing[tenant.id].get(key)
                    if found is None:
                        counts[slug]["created"] += 1
                        retailer = Retailer(
                            tenant=tenant,
                            name=row["store_name"][:255],
                            address=row["address"][:100],
                            is_national=False,
                            location_id=location_id,
                            created_by=owner,
                        )
                        if apply:
                            retailer.save()
                        existing[tenant.id][key] = retailer
                        continue
                    if found.location_id is None and location_id is not None:
                        counts[slug]["updated"] += 1
                        if apply:
                            found.location_id = location_id
                            found.updated_by = owner
                            found.save(update_fields=["location", "updated_by", "updated_at"])
                    else:
                        counts[slug]["unchanged"] += 1

            if not apply:
                transaction.set_rollback(True)

        w("")
        for slug in slugs:
            c = counts[slug]
            w(f"{slug}: created={c['created']} updated={c['updated']} unchanged={c['unchanged']}")
        w(f"stores on more than one brand: {shared}")
        w(f"cities added to Location catalog: {counts['_locations']['created']}")
        w(f"skipped: {len(skipped)}")
        for line in skipped:
            w(f"  - {line}")
        w(f"imported with an address to confirm: {len(flagged)}")
        for line in flagged:
            w(f"  ~ {line}")
        if not apply:
            w("DRY-RUN — nothing written. Re-run with --apply.")
