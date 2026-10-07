"""Re-derive request/event markets (state + city Location) from their address.

The Master Tracker "Market" column used to read ``retailer.location`` first.
Retailer accounts are banner-level and shared by every store in a chain, so
e.g. every Total Wine row showed the first Total Wine store's city ("Tucson,
AZ") no matter where the store actually was. The read side now derives the
market from the row's own address (``events.market.market_for``); this command
makes the stored ``state`` / ``location`` FKs agree with the address too.

Per live Request and Event with a parseable US address:
  * ``state`` is set to the address state;
  * ``location`` is relinked to the address city's Location when the catalog
    has one; a location in another state is cleared; a same-state location for
    a different city is cleared only when it was inherited from the shared
    retailer account (the bug's signature) — otherwise it is kept and listed.

Writes use queryset ``.update()`` — no post_save signals, no emails, no sheet
mirror, no change to times / BAs / status / RMM. Dry-run unless ``--apply``.

    python manage.py fix_tracker_markets [--tenant <slug>] [--apply]
"""

from __future__ import annotations

import base64
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from events.market import (
    PreloadedGeoLookup,
    geo_changes,
    market_for,
    resolve_geo_for_address,
)
from events.models import Event, Location, Request, State
from tenants.models import Tenant

MAX_LISTED = 400


def _resolve_tenant(raw: str) -> Tenant:
    tenant = Tenant.objects.filter(slug=raw).first()
    if tenant is None:
        tenant = Tenant.objects.filter(request_url_name=raw).first()
    if tenant is None:
        raise CommandError(f"No tenant with slug or request_url_name {raw!r}")
    return tenant


def req_code(pk: int) -> str:
    return "REQ-" + base64.b64encode(f"Request:{pk}".encode()).decode()[-4:].upper()


def _legacy_market(obj) -> str | None:
    """What the tracker showed before: retailer.location first."""
    retailer_loc = getattr(getattr(obj, "retailer", None), "location", None)
    loc = getattr(obj, "location", None)
    city = (retailer_loc.name if retailer_loc else None) or (loc.name if loc else None)
    st = getattr(getattr(retailer_loc, "state", None), "code", None) or getattr(
        getattr(obj, "state", None), "code", None
    )
    parts = [p for p in (city, st) if p]
    return ", ".join(parts) if parts else None


class Command(BaseCommand):
    help = "Re-derive request/event market (state + city) from the address."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="", help="Tenant slug or request_url_name")
        parser.add_argument("--apply", action="store_true", help="Write (default: dry-run)")

    def handle(self, *args, **opts):
        apply: bool = opts["apply"]
        tenant = _resolve_tenant(opts["tenant"]) if opts["tenant"] else None
        w = self.stdout.write
        w(
            f"fix_tracker_markets apply={apply} scope="
            f"{(tenant.slug if tenant else 'ALL TENANTS')}"
        )

        lookup = PreloadedGeoLookup()
        state_codes = dict(State.objects.values_list("id", "code"))
        loc_names = dict(Location.objects.values_list("id", "name"))

        def fk_label(state_id, location_id) -> str:
            city = loc_names.get(location_id) if location_id else None
            st = state_codes.get(state_id) if state_id else None
            return f"location={city or '∅'} state={st or '∅'}"

        stats: dict[str, Counter] = defaultdict(Counter)
        flagged: list[str] = []
        changes_log: list[str] = []

        def scan(model, label: str):
            qs = model.objects.select_related(
                "tenant", "retailer__location__state", "location__state", "state"
            ).order_by("tenant_id", "id")
            if model is Request:
                qs = qs.filter(deleted_at__isnull=True)
            if tenant is not None:
                qs = qs.filter(tenant=tenant)
            pending: list[tuple[int, dict]] = []
            for obj in qs.iterator(chunk_size=500):
                tname = obj.tenant.slug if obj.tenant_id else "?"
                key = f"{tname} {label}"
                stats[key]["scanned"] += 1
                ident = req_code(obj.id) if model is Request else f"event#{obj.id}"
                ident = f"{ident} (#{obj.id})"
                res = resolve_geo_for_address(obj.address, lookup)
                if res is None:
                    stats[key]["no_state_in_address"] += 1
                    if obj.location_id or obj.state_id or obj.retailer_id:
                        flagged.append(
                            f"  [{tname}] {label} {ident}: address has no US state "
                            f"→ left as-is ({fk_label(obj.state_id, obj.location_id)}) "
                            f"address={obj.address!r}"
                        )
                    continue
                inherited = bool(
                    obj.location_id
                    and obj.retailer_id
                    and obj.retailer.location_id == obj.location_id
                )
                changes = geo_changes(
                    obj, res, clear_city_mismatch=inherited, lookup=lookup
                )
                before_display = _legacy_market(obj)
                after_state = changes.get("state_id", obj.state_id)
                after_loc = changes.get("location_id", obj.location_id)
                after_display = market_for(obj).label
                if (before_display or "").lower() != (after_display or "").lower():
                    stats[key]["display_market_fixed"] += 1
                if res.geo.city is None:
                    stats[key]["no_city_in_address"] += 1
                    flagged.append(
                        f"  [{tname}] {label} {ident}: address has state "
                        f"{res.geo.state_code} but no city segment → market shows "
                        f"{after_display!r}; address={obj.address!r}"
                    )
                if res.location_ambiguous:
                    stats[key]["ambiguous_location"] += 1
                    flagged.append(
                        f"  [{tname}] {label} {ident}: several '{res.geo.city}, "
                        f"{res.geo.state_code}' Locations, zip didn't decide → "
                        f"linked lowest id; address={obj.address!r}"
                    )
                if (
                    after_loc
                    and res.geo.city
                    and (loc_names.get(after_loc) or "").strip().lower()
                    != res.geo.city.lower()
                ):
                    stats[key]["kept_location_city_differs"] += 1
                    flagged.append(
                        f"  [{tname}] {label} {ident}: kept location "
                        f"{loc_names.get(after_loc)!r} (set by hand, no "
                        f"'{res.geo.city}' Location) — market shows "
                        f"{after_display!r}; address={obj.address!r}"
                    )
                if not changes:
                    continue
                stats[key]["fk_updated"] += 1
                if "state_id" in changes:
                    stats[key]["state_changed"] += 1
                if "location_id" in changes:
                    stats[key]["location_changed"] += 1
                changes_log.append(
                    f"  [{tname}] {label} {ident} {obj.address!r}\n"
                    f"      tracker market: {before_display or '∅'} → {after_display or '∅'}\n"
                    f"      FKs: {fk_label(obj.state_id, obj.location_id)} → "
                    f"{fk_label(after_state, after_loc)}"
                    + ("  (location inherited from retailer)" if inherited else "")
                )
                pending.append((obj.id, changes))
            if apply and pending:
                with transaction.atomic():
                    for pk, changes in pending:
                        model.objects.filter(pk=pk).update(**changes)

        scan(Request, "request")
        scan(Event, "event")

        w("")
        w(f"CHANGES ({len(changes_log)}){'' if apply else ' — dry run, nothing written'}:")
        for line in changes_log:
            w(line)
        w("")
        w(f"FLAGGED FOR REVIEW ({len(flagged)}; first {MAX_LISTED} shown):")
        for line in flagged[:MAX_LISTED]:
            w(line)
        w("")
        w("SUMMARY (per tenant):")
        for key in sorted(stats):
            c = stats[key]
            w(
                f"  {key}: scanned={c['scanned']} display_market_fixed="
                f"{c['display_market_fixed']} fk_updated={c['fk_updated']} "
                f"(state={c['state_changed']} location={c['location_changed']}) "
                f"no_state_in_address={c['no_state_in_address']} "
                f"no_city_in_address={c['no_city_in_address']} "
                f"ambiguous_location={c['ambiguous_location']} "
                f"kept_location_city_differs={c['kept_location_city_differs']}"
            )
        w(f"DONE apply={apply}")
