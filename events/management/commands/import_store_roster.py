"""Preload a client store roster as Retailer accounts on each brand it lists.

The roster CSV lives in ``events/management/commands/data`` and carries one
row per store with a ``brands`` column of pipe-separated tenant slugs
(``famus-energy|wook-water``). A store that carries several brands becomes one
Retailer per brand — Retailer is tenant-scoped — so each brand's request form
and walk-up list it, and a multi-brand activation there resolves on every
brand.

Accounts land as regional (``is_national=False``) stores linked to the city
``Location`` resolved by the shared address parser (``events.market``). A
city name the catalog spells differently ("Mt Sterling" vs "Mount Sterling")
falls back to the catalog city with the same state + zip; only a city with
neither is added. Re-runs are idempotent: a store is
matched on (tenant, normalized address) and never duplicated.

An existing account keeps its name unless that name is a placeholder — a
legal entity ("Avni Petroleum INC") or "<address> (name to confirm)" — and
the roster now carries the store's trading name. A row whose ``store_type``
is "Not a store" (an owner's home or office) removes that address's account
when nothing references it.

SAFE: dry-run by default; ``--apply`` writes.
"""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError, RestrictedError

from ambassadors.checkin_web import normalize_place
from events.market import PreloadedGeoLookup, resolve_geo_for_address
from events.models import Location, Retailer
from tenants.models import Tenant, User

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_FILE = "famus_aa_wook_store_roster.csv"
REQUIRED_COLUMNS = {"roster_no", "store_name", "address", "city", "state", "zip", "brands"}
NOT_A_STORE = "not a store"
PLACEHOLDER_NAME = re.compile(r"\b(llc|inc|corp|corporation)\b|\(name to confirm\)", re.IGNORECASE)


def is_placeholder_name(name: str) -> bool:
    return bool(PLACEHOLDER_NAME.search(name or ""))


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
        kept: list[str] = []
        shared = 0

        with transaction.atomic():
            for row in rows:
                brands = [s for s in row["brands"].split("|") if s in tenants]
                if not brands:
                    continue
                label = f"#{row['roster_no']} {row['store_name']} — {row['address']}"
                key = normalize_place(row["address"])
                if row.get("store_type", "").lower() == NOT_A_STORE:
                    for slug in brands:
                        self._retire(existing[tenants[slug].id], key, slug, label, counts, kept)
                    continue
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
                        same_zip = (
                            Location.objects.filter(state_id=resolution.state_id, zip=row["zip"])
                            .order_by("id")
                            .first()
                        )
                        if same_zip is not None:
                            w(f"  = city {city}, {row['state']} {row['zip']} -> catalog {same_zip.name} (same zip)")
                            new_locations[loc_key] = same_zip
                        else:
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
                    fields: list[str] = []
                    if found.location_id is None and location_id is not None:
                        found.location_id = location_id
                        fields.append("location")
                    name = row["store_name"][:255]
                    if is_placeholder_name(found.name) and not is_placeholder_name(name) and found.name != name:
                        w(f"  ~ [{slug}] rename {found.name!r} -> {name!r} ({row['address']})")
                        counts[slug]["renamed"] += 1
                        found.name = name
                        fields.append("name")
                    if not fields:
                        counts[slug]["unchanged"] += 1
                        continue
                    if "location" in fields:
                        counts[slug]["updated"] += 1
                    if apply:
                        found.updated_by = owner
                        found.save(update_fields=[*fields, "updated_by", "updated_at"])

            if not apply:
                transaction.set_rollback(True)

        w("")
        for slug in slugs:
            c = counts[slug]
            w(
                f"{slug}: created={c['created']} updated={c['updated']} unchanged={c['unchanged']} "
                f"renamed={c['renamed']} retired={c['retired']}"
            )
        w(f"stores on more than one brand: {shared}")
        w(f"not-a-store accounts kept (still referenced): {len(kept)}")
        for line in kept:
            w(f"  ! {line}")
        w(f"cities added to Location catalog: {counts['_locations']['created']}")
        w(f"skipped: {len(skipped)}")
        for line in skipped:
            w(f"  - {line}")
        w(f"imported with an address to confirm: {len(flagged)}")
        for line in flagged:
            w(f"  ~ {line}")
        if not apply:
            w("DRY-RUN — nothing written. Re-run with --apply.")

    def _retire(self, accounts, key, slug, label, counts, kept) -> None:
        found = accounts.get(key)
        if found is None or found.pk is None:
            return
        try:
            with transaction.atomic():
                found.delete()
        except (ProtectedError, RestrictedError):
            kept.append(f"[{slug}] {label}: account {found.name!r} has requests/events")
            return
        counts[slug]["retired"] += 1
        self.stdout.write(f"  - [{slug}] retire {found.name!r} ({label}: not a store)")
        del accounts[key]
