"""Store logistics note persistence, lookup, briefing injection, and
store-key resolution across requests, recaps, and named stores."""

from __future__ import annotations

import uuid as uuid_mod

from events.account_list import full_address
from events.account_stores import _address_key, _cell_key, _point
from events.map_points import address_hash, is_lookup_worthy, normalise_address
from events.market import parse_address_geo
from events.models import AccountListEntry, Event, Request, Retailer
from recaps.store_places import named_store_links, store_header, tenant_places, valid_key
from store_logistics.models import StoreLogisticsNote
from utils.graphql.mixins import resolve_id_to_int

MAX_LOGISTICS_NOTES_LEN = 4000
LOGISTICS_HEADING = "Store logistics:"


def candidate_place_keys(
    *,
    address: str | None = None,
    coordinates=None,
    store_name: str | None = None,
    store_key: str | None = None,
) -> list[str]:
    """Ordered canonical keys identifying one physical store inside a tenant."""
    keys: list[str] = []
    if is_lookup_worthy(address):
        keys.append(f"addr:{address_hash(address)}")
    pt = _point(coordinates)
    if pt is not None:
        keys.append(f"cell:{pt[0]:.4f}|{pt[1]:.4f}")
    clean_name = normalise_address(store_name or "")
    if clean_name and not keys:
        keys.append(f"name:{clean_name[:110]}")
    clean_key = (store_key or "").strip()
    if clean_key and not keys:
        keys.append(f"store:{clean_key[:110]}")
    return keys


def resolve_non_request_store(tenant_id: int, raw_key: str) -> dict | None:
    """Resolve an unvisited ``list:<uuid>``, ``list:<dedupe_key>``, or
    ``retailer:<pk>`` store key belonging to ``tenant_id``."""
    clean_key = (raw_key or "").strip()
    if not tenant_id or not clean_key:
        return None

    if clean_key.startswith("list:"):
        token = clean_key[5:].strip()
        if not token:
            return None
        entry = None
        try:
            parsed_uuid = uuid_mod.UUID(token)
            entry = AccountListEntry.objects.filter(
                tenant_id=tenant_id, uuid=parsed_uuid
            ).first()
        except ValueError:
            entry = None
        if entry is None:
            entry = AccountListEntry.objects.filter(
                tenant_id=tenant_id, dedupe_key=token
            ).first()
        if entry is None:
            return None
        composed_address = full_address(
            entry.address, entry.city, entry.state, entry.zip
        ) or (entry.address or "").strip()
        coords = (
            [float(entry.lat), float(entry.lng)]
            if entry.lat is not None and entry.lng is not None
            else None
        )
        geo = parse_address_geo(composed_address)
        raw_state = (entry.state or "").strip().upper()
        state_code = raw_state if len(raw_state) == 2 else (geo.state_code or "")
        return {
            "key": clean_key,
            "name": (entry.name or "").strip(),
            "address": composed_address,
            "city": (entry.city or "").strip() or (geo.city or "").strip(),
            "state": state_code,
            "coordinates": coords,
            "entry_id": entry.id,
            "entry_notes": (entry.notes or "").strip(),
            "entry_updated_at": (
                entry.updated_at.isoformat() if entry.updated_at else None
            ),
            "alias_keys": [
                f"list:{entry.uuid}",
                f"list:{entry.dedupe_key}",
            ],
        }

    if clean_key.startswith("retailer:"):
        token = clean_key[9:].strip()
        if not token.isdigit():
            return None
        ret = (
            Retailer.objects.select_related("location__state")
            .filter(tenant_id=tenant_id, pk=int(token))
            .first()
        )
        if ret is None:
            return None
        addr = (ret.address or "").strip()
        geo = parse_address_geo(addr)
        loc = getattr(ret, "location", None)
        st_obj = getattr(loc, "state", None) if loc else None
        return {
            "key": clean_key,
            "name": (ret.name or "").strip(),
            "address": addr,
            "city": (getattr(loc, "name", "") or geo.city or "").strip(),
            "state": (getattr(st_obj, "code", "") or geo.state_code or "").upper(),
            "coordinates": None,
            "entry_id": None,
            "entry_notes": "",
            "entry_updated_at": None,
            "alias_keys": [clean_key],
        }

    return None
def _store_coordinates_for_key(tenant_id: int, places, store_key: str):
    req_ids = places.requests_of(store_key)
    if not req_ids:
        return None
    for coords in (
        Request.objects.filter(tenant_id=tenant_id, pk__in=req_ids)
        .values_list("coordinates", flat=True)
        .order_by("pk")
    ):
        pt = _point(coords)
        if pt is not None:
            return pt
    return None


def get_store_logistics_note(
    tenant_id: int | None,
    *,
    store_key: str = "",
    store_name: str = "",
    address: str = "",
    coordinates=None,
    event_id: int | None = None,
) -> tuple[str, str | None]:
    """Return ``(notes, updated_at_iso)`` for a store in ``tenant_id``.

    Checks ``StoreLogisticsNote`` first; if no row has been saved for this
    store yet, falls back to ``AccountListEntry.notes`` on a matching street
    address so CSV-uploaded door notes surface automatically.
    """
    if not tenant_id:
        return "", None

    clean_store_key = (store_key or "").strip()
    non_req = (
        resolve_non_request_store(tenant_id, clean_store_key)
        if clean_store_key.startswith(("list:", "retailer:"))
        else None
    )
    if non_req is not None:
        address = address or non_req["address"]
        store_name = store_name or non_req["name"]
        if coordinates is None:
            coordinates = non_req["coordinates"]

    keys = candidate_place_keys(
        address=address,
        coordinates=coordinates,
        store_name=store_name,
        store_key=clean_store_key,
    )

    qs = StoreLogisticsNote.objects.filter(tenant_id=tenant_id)
    if qs.exists():
        for key in keys:
            row = qs.filter(place_key=key).first()
            if row is not None:
                return row.notes or "", row.updated_at.isoformat() if row.updated_at else None

        hint_candidates = (
            non_req["alias_keys"]
            if non_req is not None
            else ([clean_store_key] if clean_store_key else [])
        )
        if hint_candidates:
            row = (
                qs.filter(store_key_hint__in=hint_candidates)
                .order_by("-updated_at", "-id")
                .first()
            )
            if row is not None:
                return row.notes or "", row.updated_at.isoformat() if row.updated_at else None

        if event_id or is_lookup_worthy(address) or _point(coordinates) is not None:
            places = tenant_places(tenant_id)
            resolved_key: str | None = None
            if event_id:
                resolved_key = places.for_event_ids([event_id]).get(int(event_id))
            if not resolved_key:
                for idx_key in (
                    _address_key(tenant_id, address),
                    _cell_key(tenant_id, _point(coordinates)),
                ):
                    if idx_key and places.index.by_place.get(idx_key):
                        resolved_key = places.index.by_place[idx_key]
                        break
            if resolved_key:
                row = (
                    qs.filter(store_key_hint=resolved_key)
                    .order_by("-updated_at", "-id")
                    .first()
                )
                if row is not None:
                    return row.notes or "", row.updated_at.isoformat() if row.updated_at else None

    if non_req is not None and non_req["entry_notes"]:
        return non_req["entry_notes"], non_req["entry_updated_at"]

    if is_lookup_worthy(address):
        addr_h = address_hash(address)
        entry = (
            AccountListEntry.objects.filter(tenant_id=tenant_id, address_hash=addr_h)
            .exclude(notes="")
            .order_by("-updated_at", "-id")
            .first()
        )
        if entry and (entry.notes or "").strip():
            return (
                entry.notes.strip(),
                entry.updated_at.isoformat() if entry.updated_at else None,
            )

    return "", None


def get_store_logistics_for_event(event) -> str:
    """Resolve non-empty store logistics notes for an ``Event`` instance."""
    if event is None or not getattr(event, "tenant_id", None):
        return ""
    req = getattr(event, "request", None)
    address = (
        (getattr(event, "address", None) or "").strip()
        or (getattr(req, "address", None) or "").strip()
    )
    store_name = (
        (getattr(req, "retailer_name", None) or "").strip()
        or (getattr(event, "name", None) or "").strip()
    )
    coords = getattr(event, "coordinates", None) or getattr(req, "coordinates", None)
    notes, _ = get_store_logistics_note(
        int(event.tenant_id),
        store_name=store_name,
        address=address,
        coordinates=coords,
        event_id=getattr(event, "id", None),
    )
    return (notes or "").strip()


def format_briefing_with_logistics(
    title: str, body: str, logistics_notes: str
) -> tuple[str, str]:
    """Merge ``logistics_notes`` into ``(title, body)`` without clobbering
    existing custom briefing text or duplicating notes."""
    clean_notes = (logistics_notes or "").strip()
    if not clean_notes:
        return title, body

    clean_body = (body or "").strip()
    if clean_notes in clean_body:
        return title or "Shift briefing", body

    block = f"{LOGISTICS_HEADING}\n{clean_notes}"
    if not clean_body:
        return (title or "Store logistics"), block
    return title, f"{body.rstrip()}\n\n{block}"


def strip_dynamic_logistics_from_briefing(
    job, title: str | None, body: str | None
) -> tuple[str | None, str | None]:
    """Strip the dynamically injected ``Store logistics:`` block if a client
    round-trips an enriched briefing back into ``updateJobBriefing``, so the
    dynamic store note is never frozen into ``Job.briefing_body``."""
    if body is None or job is None or not getattr(job, "tenant_id", None):
        return title, body
    event = getattr(job, "event", None)
    if event is None and getattr(job, "event_id", None):
        event = (
            Event.objects.select_related("request")
            .filter(id=job.event_id, tenant_id=job.tenant_id)
            .first()
        )
    notes = get_store_logistics_for_event(event)
    if not notes and (getattr(job, "address", None) or "").strip():
        notes, _ = get_store_logistics_note(
            int(job.tenant_id),
            address=job.address,
            store_name=job.name or "",
        )
    clean_notes = (notes or "").strip()
    if not clean_notes:
        return title, body
    block = f"{LOGISTICS_HEADING}\n{clean_notes}"
    trimmed = body.rstrip()
    if trimmed == block:
        next_title = "" if (title or "").strip() == "Store logistics" else title
        return next_title, ""
    suffix = f"\n\n{block}"
    if trimmed.endswith(suffix):
        return title, trimmed[: -len(suffix)].rstrip()
    return title, body


def enrich_job_briefing(job, title: str, body: str) -> tuple[str, str]:
    """Enrich a ``Job``'s ``(title, body)`` with its store's logistics notes."""
    if job is None or not getattr(job, "tenant_id", None):
        return title, body
    event = getattr(job, "event", None)
    if event is None and getattr(job, "event_id", None):
        event = (
            Event.objects.select_related("request")
            .filter(id=job.event_id, tenant_id=job.tenant_id)
            .first()
        )
    notes = get_store_logistics_for_event(event)
    if not notes and (getattr(job, "address", None) or "").strip():
        notes, _ = get_store_logistics_note(
            int(job.tenant_id),
            address=job.address,
            store_name=job.name or "",
        )
    return format_briefing_with_logistics(title, body, notes)


def save_store_logistics_note(
    tenant_id: int,
    *,
    store_key: str,
    notes: str,
    user=None,
) -> tuple[StoreLogisticsNote | None, str | None]:
    """Persist ``notes`` for ``store_key`` in ``tenant_id``.

    Validates that ``store_key`` belongs to ``tenant_id`` (either a live
    request-backed ``s<pk>`` store or an ``AccountListEntry`` ``list:<uuid>``).
    Also syncs ``AccountListEntry.notes`` if an entry already exists at that
    address.
    """
    raw_key = (store_key or "").strip()
    clean_notes = (notes or "").strip()[:MAX_LOGISTICS_NOTES_LEN]

    name = ""
    address = ""
    coords = None
    canonical_hint = ""
    entry_id: int | None = None
    alias_hints: list[str] = []

    checked_key = valid_key(raw_key)
    if checked_key:
        places = tenant_places(tenant_id)
        if not places.has(checked_key):
            return None, "Store not found for this brand."
        header = store_header(tenant_id, places, checked_key)
        name = header["name"]
        address = header["address"]
        coords = _store_coordinates_for_key(tenant_id, places, checked_key)
        canonical_hint = checked_key
        alias_hints = [checked_key]
    elif raw_key.startswith(("list:", "retailer:")):
        non_req = resolve_non_request_store(tenant_id, raw_key)
        if non_req is None:
            return None, "Store not found for this brand."
        name = non_req["name"]
        address = non_req["address"]
        coords = non_req["coordinates"]
        canonical_hint = raw_key
        entry_id = non_req["entry_id"]
        alias_hints = non_req["alias_keys"]
    else:
        return None, "Invalid store key."

    keys = candidate_place_keys(
        address=address,
        coordinates=coords,
        store_name=name,
        store_key=canonical_hint,
    )
    if not keys:
        return None, "Store has no identifiable location."

    primary_key = keys[0]
    row = StoreLogisticsNote.objects.filter(
        tenant_id=tenant_id, place_key__in=keys
    ).first()
    if row is None and alias_hints:
        row = StoreLogisticsNote.objects.filter(
            tenant_id=tenant_id, store_key_hint__in=alias_hints
        ).first()

    user_obj = user if getattr(user, "is_authenticated", False) else None
    safe_hint = canonical_hint[:128]
    if row is None:
        row = StoreLogisticsNote.objects.create(
            tenant_id=tenant_id,
            place_key=primary_key[:128],
            store_key_hint=safe_hint,
            store_name=name[:255],
            address=address,
            notes=clean_notes,
            updated_by=user_obj,
        )
    else:
        row.store_key_hint = (safe_hint or row.store_key_hint)[:128]
        row.store_name = (name or row.store_name)[:255]
        row.address = address or row.address
        row.notes = clean_notes
        row.updated_by = user_obj
        row.save(
            update_fields=[
                "store_key_hint",
                "store_name",
                "address",
                "notes",
                "updated_by",
                "updated_at",
            ]
        )

    if entry_id is not None:
        AccountListEntry.objects.filter(tenant_id=tenant_id, pk=entry_id).update(
            notes=clean_notes
        )
    if is_lookup_worthy(address):
        AccountListEntry.objects.filter(
            tenant_id=tenant_id, address_hash=address_hash(address)
        ).update(notes=clean_notes)

    return row, None


def resolve_store_key_for_target(
    tenant_id: int,
    *,
    staff: bool,
    store_key: str | None = None,
    request_id: str | None = None,
    recap_id: str | None = None,
    store_name: str | None = None,
    address: str | None = None,
) -> str | None:
    """Resolve any store reference (storeKey, requestId, recapId, address, or
    unambiguous storeName) to a canonical ``s<pk>`` storeKey in ``tenant_id``."""
    # Circular dependency: recaps.store_profile imports store_logistics.service.
    from recaps.store_profile import recap_store_key

    places = tenant_places(tenant_id)

    checked = valid_key(store_key)
    if checked and places.has(checked):
        return checked

    if store_key and str(store_key).strip().startswith(("list:", "retailer:")):
        non_req = resolve_non_request_store(tenant_id, str(store_key).strip())
        if non_req is not None:
            for idx_key in (
                _address_key(tenant_id, non_req["address"]),
                _cell_key(tenant_id, _point(non_req["coordinates"])),
            ):
                if idx_key and places.index.by_place.get(idx_key):
                    existing = places.index.by_place[idx_key]
                    if places.has(existing):
                        return existing
            return non_req["key"]

    if request_id:
        raw_req = str(request_id).strip()
        req_pk: int | None = None
        try:
            candidate_pk = resolve_id_to_int(raw_req)
            if Request.objects.filter(
                tenant_id=tenant_id, pk=candidate_pk, deleted_at__isnull=True
            ).exists():
                req_pk = int(candidate_pk)
        except Exception:
            req_pk = None

        if req_pk is None:
            try:
                parsed_uuid = uuid_mod.UUID(raw_req)
                req_pk = (
                    Request.objects.filter(
                        tenant_id=tenant_id, uuid=parsed_uuid, deleted_at__isnull=True
                    )
                    .values_list("pk", flat=True)
                    .first()
                )
            except (ValueError, TypeError):
                req_pk = None

        if req_pk is not None:
            resolved = places.index.store_for_request(int(req_pk))
            if places.has(resolved):
                return resolved

    if recap_id:
        resolved = recap_store_key(tenant_id, str(recap_id), staff=staff)
        if resolved and places.has(resolved):
            return resolved

    if is_lookup_worthy(address):
        addr_idx_key = _address_key(tenant_id, address)
        if addr_idx_key:
            by_addr = places.index.by_place.get(addr_idx_key)
            if by_addr and places.has(by_addr):
                return by_addr

    clean_name = (store_name or "").strip()
    if clean_name:
        links = named_store_links(tenant_id)
        by_fold = {row["name"].casefold(): row["store_key"] for row in links}
        direct = by_fold.get(clean_name.casefold())
        if direct and places.has(direct):
            return direct
        for sep in (" — ", " – ", " - ", " · "):
            if sep in clean_name:
                head = clean_name.split(sep, 1)[0].strip()
                alt = by_fold.get(head.casefold())
                if alt and places.has(alt):
                    return alt

    return None
