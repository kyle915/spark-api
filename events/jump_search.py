"""Composite search endpoint for the Spark Jump command palette (Cmd+K).

Replaces firing 6 separate GraphQL HTTP queries on every keystroke with one
tenant- and role-scoped lookup returning requests, events, physical stores
(with Account Map ``storeKey`` when available), recaps, and — for Ignite
staff/admins only — ambassadors and client contacts.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
import threading
import time
import uuid as uuid_mod

import strawberry
from strawberry.relay import to_base64
from asgiref.sync import sync_to_async
from django.db.models import Count, Max, Q, Value
from django.db.models.functions import Coalesce, Concat

from ambassadors.models import Ambassador
from events.account_stores import _group_requests
from events.market import parse_address_geo
from events.models import Client, Event, Request, Retailer, RetailerAlias
from events.queries import RequestQueriesService
from events.retailer_aliases import accepted_alias_map, resolve_store_name
from recaps.models import CustomRecap, Recap
from tenants.models import Tenant
from utils.graphql.mixins import SELECTED_TENANT_HEADER, resolve_id_to_int
from utils.graphql.permissions import (
    IsClientOrSparkAdmin,
    _is_admin_access,
    resolve_request_user_access,
)

DEFAULT_LIMIT = 6
MAX_LIMIT = 12
MAX_QUERY_LEN = 120
_STORE_CACHE_MAX_TENANTS = 64
_STORE_CACHE_TTL_SECONDS = 60.0


@strawberry.type
class JumpRequestHit:
    uuid: str
    name: str
    date: str | None
    retailer_name: str | None


@strawberry.type
class JumpEventHit:
    uuid: str
    name: str
    date: str | None
    address: str | None
    retailer_name: str | None


@strawberry.type
class JumpStoreHit:
    key: str
    store_key: str | None
    name: str
    address: str | None
    city: str | None
    state: str | None


@strawberry.type
class JumpRecapHit:
    uuid: str
    kind: str
    name: str
    approved: bool
    event_date: str | None


@strawberry.type
class JumpAmbassadorHit:
    uuid: str
    name: str
    email: str | None


@strawberry.type
class JumpClientHit:
    uuid: str
    name: str
    email: str | None
    tenant_id: strawberry.ID | None
    tenant_uuid: str | None
    tenant_name: str | None
    slug: str | None
    checkin_code: str | None


@strawberry.type
class JumpSearchResult:
    requests: list[JumpRequestHit]
    events: list[JumpEventHit]
    stores: list[JumpStoreHit]
    recaps: list[JumpRecapHit]
    ambassadors: list[JumpAmbassadorHit]
    clients: list[JumpClientHit]


@dataclass(frozen=True)
class _CachedTenantStore:
    store_key: str
    name: str
    address: str | None
    city: str | None
    state: str | None
    search_blob: str


_STORE_CACHE_LOCK = threading.Lock()
_TENANT_STORE_CACHE: OrderedDict[
    int, tuple[tuple[object, ...], float, tuple[_CachedTenantStore, ...]]
] = OrderedDict()


def clear_jump_store_cache() -> None:
    """Clear the in-process Jump store directory cache (used by tests)."""
    with _STORE_CACHE_LOCK:
        _TENANT_STORE_CACHE.clear()


def _empty_result() -> JumpSearchResult:
    return JumpSearchResult(
        requests=[],
        events=[],
        stores=[],
        recaps=[],
        ambassadors=[],
        clients=[],
    )


def _iso_day(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] or None


def _first_nonempty(*values: str | None) -> str:
    for val in values:
        cleaned = (val or "").strip()
        if cleaned:
            return cleaned
    return ""


def _resolve_header_tenant_id(info: strawberry.Info) -> int | None:
    request = getattr(info.context, "request", None)
    headers = getattr(request, "headers", None) if request is not None else None
    raw_uuid = (headers.get(SELECTED_TENANT_HEADER) or "").strip() if headers else ""
    if not raw_uuid:
        return None
    try:
        parsed = uuid_mod.UUID(raw_uuid)
    except ValueError:
        return None
    return (
        Tenant.objects.filter(uuid=parsed)
        .values_list("id", flat=True)
        .first()
    )


async def _resolve_jump_scope(
    info: strawberry.Info,
    tenant_id: strawberry.ID | None,
) -> tuple[int | None, bool]:
    """Return ``(target_tenant_id, is_admin)`` with strict client isolation."""
    service = RequestQueriesService()
    user = await service.get_user(info)
    role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    is_admin = _is_admin_access(role_slug, is_staff, is_super, email)

    if not is_admin:
        try:
            resolved = await service.resolve_tenant_id(info, tenant_id=tenant_id)
        except Exception:
            try:
                resolved = await service.resolve_tenant_id(info)
            except Exception:
                return None, False
        return (int(resolved) if resolved is not None else None), False

    if tenant_id is not None and str(tenant_id).strip():
        raw = str(tenant_id).strip()
        try:
            resolved_int = resolve_id_to_int(raw)
        except Exception:
            resolved_int = None
        if resolved_int is not None:
            exists = await sync_to_async(
                Tenant.objects.filter(id=resolved_int).exists
            )()
            if exists:
                return resolved_int, True
        try:
            parsed_uuid = uuid_mod.UUID(raw)
            by_uuid = await sync_to_async(
                lambda: Tenant.objects.filter(uuid=parsed_uuid)
                .values_list("id", flat=True)
                .first()
            )()
            if by_uuid is not None:
                return int(by_uuid), True
        except ValueError:
            pass

    header_tid = await sync_to_async(_resolve_header_tenant_id)(info)
    return header_tid, True


def _search_requests(
    term: str,
    tenant_id: int | None,
    limit: int,
) -> list[JumpRequestHit]:
    qs = Request.objects.filter(deleted_at__isnull=True)
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    qs = qs.filter(
        Q(name__icontains=term)
        | Q(retailer__name__icontains=term)
        | Q(retailer_name__icontains=term)
        | Q(address__icontains=term)
    ).order_by("-date", "-id")

    rows = qs.values_list(
        "uuid",
        "name",
        "date",
        "retailer__name",
        "retailer_name",
    )[:limit]
    return [
        JumpRequestHit(
            uuid=str(uuid_val),
            name=_first_nonempty(name, ret_name, typed_ret) or "Request",
            date=_iso_day(dt),
            retailer_name=_first_nonempty(ret_name, typed_ret) or None,
        )
        for uuid_val, name, dt, ret_name, typed_ret in rows
    ]


def _search_events(
    term: str,
    tenant_id: int | None,
    limit: int,
) -> list[JumpEventHit]:
    qs = Event.objects.filter(
        Q(request__isnull=True) | Q(request__deleted_at__isnull=True)
    )
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    qs = qs.filter(
        Q(name__icontains=term)
        | Q(retailer__name__icontains=term)
        | Q(request__retailer__name__icontains=term)
        | Q(request__retailer_name__icontains=term)
        | Q(address__icontains=term)
    ).order_by("-date", "-id")

    rows = qs.values_list(
        "uuid",
        "name",
        "date",
        "start_time",
        "address",
        "retailer__name",
        "request__retailer__name",
        "request__retailer_name",
    )[:limit]
    return [
        JumpEventHit(
            uuid=str(uuid_val),
            name=_first_nonempty(name, ret_name, req_ret, req_typed) or "Event",
            date=_iso_day(dt or start_dt),
            address=(address or "").strip() or None,
            retailer_name=_first_nonempty(ret_name, req_ret, req_typed) or None,
        )
        for uuid_val, name, dt, start_dt, address, ret_name, req_ret, req_typed in rows
    ]


def _tenant_store_watermark(tenant_id: int) -> tuple[object, ...]:
    req_agg = Request.objects.filter(
        tenant_id=tenant_id, deleted_at__isnull=True
    ).aggregate(
        cnt=Count("id"),
        max_id=Max("id"),
        max_upd=Max("updated_at"),
    )
    alias_agg = RetailerAlias.objects.filter(
        tenant_id=tenant_id, status=RetailerAlias.STATUS_ACCEPTED
    ).aggregate(
        cnt=Count("id"),
        max_upd=Max("updated_at"),
    )
    return (
        int(req_agg["cnt"] or 0),
        int(req_agg["max_id"] or 0),
        req_agg["max_upd"],
        int(alias_agg["cnt"] or 0),
        alias_agg["max_upd"],
    )


def _build_tenant_store_directory(tenant_id: int) -> tuple[_CachedTenantStore, ...]:
    """Build one tenant's Account Map store directory in a single DB pass."""
    rows = list(
        Request.objects.filter(tenant_id=tenant_id, deleted_at__isnull=True)
        .order_by("pk")
        .values_list(
            "pk",
            "tenant_id",
            "retailer_id",
            "address",
            "coordinates",
            "retailer__name",
            "retailer_name",
            "name",
            "state__code",
            "date",
        )
    )
    if not rows:
        return ()

    index = _group_requests(
        (pk, t_id, r_id, addr, coords)
        for pk, t_id, r_id, addr, coords, *_rest in rows
    )
    aliases = accepted_alias_map(tenant_id)

    per_store_names: dict[str, Counter[str]] = {}
    per_store_address: dict[str, str] = {}
    per_store_city: dict[str, str] = {}
    per_store_state: dict[str, str] = {}
    per_store_terms: dict[str, set[str]] = {}
    per_store_latest: dict[str, tuple[str, int]] = {}

    for (
        pk,
        _t_id,
        _r_id,
        addr,
        _coords,
        ret_name,
        typed_ret,
        req_name,
        state_code,
        req_date,
    ) in rows:
        pk_int = int(pk)
        s_key = index.store_for_request(pk_int)
        names_counter = per_store_names.setdefault(s_key, Counter())
        terms = per_store_terms.setdefault(s_key, set())

        label = _first_nonempty(ret_name, typed_ret, req_name)
        if label:
            resolved_label = resolve_store_name(label, aliases)
            names_counter[resolved_label] += 1
            terms.add(resolved_label.casefold())
        for raw_piece in (ret_name, typed_ret, req_name, addr):
            cleaned_piece = (raw_piece or "").strip()
            if cleaned_piece:
                terms.add(cleaned_piece.casefold())

        if not per_store_address.get(s_key) and (addr or "").strip():
            clean_addr = addr.strip()
            per_store_address[s_key] = clean_addr
            geo = parse_address_geo(clean_addr)
            city = (geo.city or "").strip()
            state = (state_code or geo.state_code or "").strip().upper()
            if city:
                per_store_city[s_key] = city
                terms.add(city.casefold())
            if state:
                per_store_state[s_key] = state
                terms.add(state.casefold())
        elif not per_store_state.get(s_key) and state_code:
            state = state_code.strip().upper()
            per_store_state[s_key] = state
            terms.add(state.casefold())

        sort_candidate = (_iso_day(req_date) or "", pk_int)
        prev_latest = per_store_latest.get(s_key)
        if prev_latest is None or sort_candidate > prev_latest:
            per_store_latest[s_key] = sort_candidate

    ranked_entries: list[tuple[tuple[str, int], _CachedTenantStore]] = []
    for s_key, names_counter in per_store_names.items():
        if not names_counter:
            continue
        canonical_name = names_counter.most_common(1)[0][0]
        if not canonical_name:
            continue
        ranked_entries.append(
            (
                per_store_latest.get(s_key, ("", 0)),
                _CachedTenantStore(
                    store_key=s_key,
                    name=canonical_name,
                    address=per_store_address.get(s_key) or None,
                    city=per_store_city.get(s_key) or None,
                    state=per_store_state.get(s_key) or None,
                    search_blob="\n".join(per_store_terms.get(s_key, ())),
                ),
            )
        )

    ranked_entries.sort(key=lambda item: item[0], reverse=True)
    return tuple(entry for _sort_key, entry in ranked_entries)


def _get_tenant_store_directory(tenant_id: int) -> tuple[_CachedTenantStore, ...]:
    watermark = _tenant_store_watermark(tenant_id)
    if watermark[0] == 0:
        return ()

    now_mono = time.monotonic()
    with _STORE_CACHE_LOCK:
        cached = _TENANT_STORE_CACHE.get(tenant_id)
        if (
            cached is not None
            and cached[0] == watermark
            and (now_mono - cached[1]) < _STORE_CACHE_TTL_SECONDS
        ):
            _TENANT_STORE_CACHE.move_to_end(tenant_id)
            return cached[2]

    directory = _build_tenant_store_directory(tenant_id)
    with _STORE_CACHE_LOCK:
        _TENANT_STORE_CACHE[tenant_id] = (watermark, now_mono, directory)
        _TENANT_STORE_CACHE.move_to_end(tenant_id)
        while len(_TENANT_STORE_CACHE) > _STORE_CACHE_MAX_TENANTS:
            _TENANT_STORE_CACHE.popitem(last=False)
    return directory


def _search_stores(
    term: str,
    tenant_id: int | None,
    limit: int,
) -> list[JumpStoreHit]:
    hits: list[JumpStoreHit] = []
    seen_signatures: set[tuple[str, str]] = set()
    needle = term.casefold()

    if tenant_id is not None:
        for store in _get_tenant_store_directory(tenant_id):
            if needle not in store.search_blob:
                continue
            sig = (store.name.casefold(), (store.address or "").casefold())
            seen_signatures.add(sig)
            hits.append(
                JumpStoreHit(
                    key=store.store_key,
                    store_key=store.store_key,
                    name=store.name,
                    address=store.address,
                    city=store.city,
                    state=store.state,
                )
            )
            if len(hits) >= limit:
                return hits

    if len(hits) < limit:
        ret_qs = Retailer.objects.all()
        if tenant_id is not None:
            ret_qs = ret_qs.filter(tenant_id=tenant_id)
        ret_rows = (
            ret_qs.filter(
                Q(name__icontains=term) | Q(address__icontains=term)
            )
            .order_by("name", "id")
            .values_list(
                "uuid",
                "name",
                "address",
                "location__name",
                "location__state__code",
            )[: limit * 2]
        )
        for r_uuid, r_name, r_addr, loc_name, state_code in ret_rows:
            clean_name = (r_name or "").strip()
            if not clean_name:
                continue
            clean_addr = (r_addr or "").strip()
            sig = (clean_name.casefold(), clean_addr.casefold())
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            hits.append(
                JumpStoreHit(
                    key=f"r:{r_uuid}",
                    store_key=None,
                    name=clean_name,
                    address=clean_addr or None,
                    city=(loc_name or "").strip() or None,
                    state=(state_code or "").strip().upper() or None,
                )
            )
            if len(hits) >= limit:
                break

    return hits


def _search_recaps(
    term: str,
    tenant_id: int | None,
    *,
    is_admin: bool,
    limit: int,
) -> list[JumpRecapHit]:
    place_q = (
        Q(event__name__icontains=term)
        | Q(event__retailer__name__icontains=term)
        | Q(event__request__retailer__name__icontains=term)
        | Q(event__request__retailer_name__icontains=term)
        | Q(event__address__icontains=term)
    )
    if is_admin:
        place_q = (
            place_q
            | Q(ambassador__user__first_name__icontains=term)
            | Q(ambassador__user__last_name__icontains=term)
        )

    candidates: list[tuple[str, int, JumpRecapHit]] = []
    for kind, model, tenant_field in (
        ("custom", CustomRecap, "tenant_id"),
        ("legacy", Recap, "event__tenant_id"),
    ):
        qs = model.objects.filter(
            Q(event__request__isnull=True)
            | Q(event__request__deleted_at__isnull=True)
        )
        if tenant_id is not None:
            qs = qs.filter(**{tenant_field: tenant_id})
        if not is_admin:
            qs = qs.filter(approved=True, archived_at__isnull=True)
        else:
            qs = qs.filter(archived_at__isnull=True)

        qs = (
            qs.filter(place_q)
            .annotate(
                _day=Coalesce(
                    "event__date",
                    "event__start_time",
                    "event__request__date",
                    "created_at",
                )
            )
            .order_by("-_day", "-id")
        )
        rows = qs.values_list(
            "id",
            "uuid",
            "approved",
            "_day",
            "event__name",
            "event__retailer__name",
            "event__request__retailer__name",
            "event__request__retailer_name",
        )[:limit]
        for pk, r_uuid, approved, day_val, ev_name, ret_name, req_ret, req_typed in rows:
            day_iso = _iso_day(day_val)
            candidates.append(
                (
                    day_iso or "",
                    int(pk),
                    JumpRecapHit(
                        uuid=str(r_uuid),
                        kind=kind,
                        name=_first_nonempty(ev_name, ret_name, req_ret, req_typed)
                        or "Recap",
                        approved=bool(approved),
                        event_date=day_iso,
                    ),
                )
            )

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [hit for _day_str, _pk, hit in candidates[:limit]]


def _search_ambassadors(term: str, limit: int) -> list[JumpAmbassadorHit]:
    qs = (
        Ambassador.objects.filter(user__is_active=True)
        .annotate(
            _full_name=Concat(
                "user__first_name",
                Value(" "),
                "user__last_name",
            )
        )
        .filter(
            Q(user__first_name__icontains=term)
            | Q(user__last_name__icontains=term)
            | Q(_full_name__icontains=term)
            | Q(user__email__icontains=term)
        )
        .order_by("user__first_name", "user__last_name", "id")
    )
    rows = qs.values_list(
        "uuid",
        "user__first_name",
        "user__last_name",
        "user__email",
    )[:limit]
    out: list[JumpAmbassadorHit] = []
    for a_uuid, first_name, last_name, email in rows:
        full_name = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
        out.append(
            JumpAmbassadorHit(
                uuid=str(a_uuid),
                name=full_name or (email or "").strip() or "Ambassador",
                email=(email or "").strip() or None,
            )
        )
    return out


_CLIENT_VALUE_FIELDS = (
    "uuid",
    "name",
    "email",
    "tenant_id",
    "tenant__uuid",
    "tenant__name",
    "tenant__slug",
    "tenant__checkin_code",
)


def _search_clients(
    term: str,
    tenant_id: int | None,
    limit: int,
) -> list[JumpClientHit]:
    qs = Client.objects.filter(
        Q(name__icontains=term)
        | Q(email__icontains=term)
        | Q(tenant__name__icontains=term)
        | Q(tenant__slug__icontains=term)
    ).order_by("name", "id")
    if tenant_id is not None:
        # Prioritize active-tenant matches first, then global matches.
        local_rows = list(
            qs.filter(tenant_id=tenant_id).values_list(*_CLIENT_VALUE_FIELDS)[:limit]
        )
        remaining = limit - len(local_rows)
        other_rows = (
            list(
                qs.exclude(tenant_id=tenant_id).values_list(*_CLIENT_VALUE_FIELDS)[
                    :remaining
                ]
            )
            if remaining > 0
            else []
        )
        rows = local_rows + other_rows
    else:
        rows = list(qs.values_list(*_CLIENT_VALUE_FIELDS)[:limit])

    return [
        JumpClientHit(
            uuid=str(c_uuid),
            name=(c_name or "").strip() or (c_email or "").strip() or "Client",
            email=(c_email or "").strip() or None,
            tenant_id=(
                strawberry.ID(to_base64("Tenant", int(t_pk)))
                if t_pk is not None
                else None
            ),
            tenant_uuid=str(t_uuid) if t_uuid else None,
            tenant_name=(t_name or "").strip() or None,
            slug=(t_slug or "").strip() or None,
            checkin_code=(t_checkin or "").strip() or None,
        )
        for c_uuid, c_name, c_email, t_pk, t_uuid, t_name, t_slug, t_checkin in rows
    ]


def execute_jump_search(
    *,
    term: str,
    tenant_id: int | None,
    is_admin: bool,
    limit: int,
) -> JumpSearchResult:
    if not is_admin and tenant_id is None:
        return _empty_result()

    return JumpSearchResult(
        requests=_search_requests(term, tenant_id, limit),
        events=_search_events(term, tenant_id, limit),
        stores=_search_stores(term, tenant_id, limit),
        recaps=_search_recaps(term, tenant_id, is_admin=is_admin, limit=limit),
        ambassadors=_search_ambassadors(term, limit) if is_admin else [],
        clients=_search_clients(term, tenant_id, limit) if is_admin else [],
    )


@strawberry.type
class JumpSearchQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def jump_search(
        self,
        info: strawberry.Info,
        q: str,
        tenant_id: strawberry.ID | None = None,
        first: int | None = None,
    ) -> JumpSearchResult:
        """Composite Jump (Cmd+K) search in a single HTTP round-trip."""
        cleaned = " ".join((q or "").split()).strip()[:MAX_QUERY_LEN]
        if len(cleaned) < 2:
            return _empty_result()

        limit = max(1, min(int(first or DEFAULT_LIMIT), MAX_LIMIT))
        target_tenant_id, is_admin = await _resolve_jump_scope(info, tenant_id)
        if not is_admin and target_tenant_id is None:
            return _empty_result()

        return await sync_to_async(
            lambda: execute_jump_search(
                term=cleaned,
                tenant_id=target_tenant_id,
                is_admin=is_admin,
                limit=limit,
            ),
            thread_sensitive=True,
        )()
