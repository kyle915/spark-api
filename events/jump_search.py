"""Composite search endpoint for the Spark Jump command palette (Cmd+K).

Replaces firing 6 separate GraphQL HTTP queries on every keystroke with one
tenant- and role-scoped lookup returning requests, events, physical stores
(with Account Map ``storeKey`` when available), recaps, and — for Ignite
staff/admins only — ambassadors and client contacts.
"""

from __future__ import annotations

from datetime import date, datetime
import uuid as uuid_mod

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import Q, Value
from django.db.models.functions import Coalesce, Concat

from ambassadors.models import Ambassador
from events.models import Client, Event, Request, Retailer
from events.queries import RequestQueriesService
from recaps.models import CustomRecap, Recap
from recaps.store_places import store_header, tenant_places
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
_STORE_SCAN_CAP = 200


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
    tenant_uuid: str | None
    tenant_name: str | None


@strawberry.type
class JumpSearchResult:
    requests: list[JumpRequestHit]
    events: list[JumpEventHit]
    stores: list[JumpStoreHit]
    recaps: list[JumpRecapHit]
    ambassadors: list[JumpAmbassadorHit]
    clients: list[JumpClientHit]


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
            resolved = await service.resolve_tenant_id(info)
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


def _search_stores(
    term: str,
    tenant_id: int | None,
    limit: int,
) -> list[JumpStoreHit]:
    hits: list[JumpStoreHit] = []
    seen_signatures: set[tuple[str, str]] = set()

    if tenant_id is not None:
        matched_pks = list(
            Request.objects.filter(tenant_id=tenant_id, deleted_at__isnull=True)
            .filter(
                Q(retailer__name__icontains=term)
                | Q(retailer_name__icontains=term)
                | Q(name__icontains=term)
                | Q(address__icontains=term)
            )
            .order_by("-date", "-id")
            .values_list("pk", flat=True)[:_STORE_SCAN_CAP]
        )
        if matched_pks:
            places = tenant_places(tenant_id)
            seen_keys: set[str] = set()
            for pk in matched_pks:
                s_key = places.index.store_for_request(int(pk))
                if not s_key or s_key in seen_keys:
                    continue
                seen_keys.add(s_key)
                hdr = store_header(tenant_id, places, s_key)
                store_name = (hdr.get("name") or "").strip()
                if not store_name:
                    continue
                addr = (hdr.get("address") or "").strip()
                sig = (store_name.casefold(), addr.casefold())
                seen_signatures.add(sig)
                hits.append(
                    JumpStoreHit(
                        key=s_key,
                        store_key=s_key,
                        name=store_name,
                        address=addr or None,
                        city=(hdr.get("city") or "").strip() or None,
                        state=(hdr.get("state") or "").strip() or None,
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


def _search_clients(
    term: str,
    tenant_id: int | None,
    limit: int,
) -> list[JumpClientHit]:
    qs = Client.objects.filter(
        Q(name__icontains=term)
        | Q(email__icontains=term)
        | Q(tenant__name__icontains=term)
    ).order_by("name", "id")
    if tenant_id is not None:
        # Prioritize active-tenant matches first, then global matches.
        local_rows = list(
            qs.filter(tenant_id=tenant_id).values_list(
                "uuid", "name", "email", "tenant__uuid", "tenant__name"
            )[:limit]
        )
        remaining = limit - len(local_rows)
        other_rows = (
            list(
                qs.exclude(tenant_id=tenant_id).values_list(
                    "uuid", "name", "email", "tenant__uuid", "tenant__name"
                )[:remaining]
            )
            if remaining > 0
            else []
        )
        rows = local_rows + other_rows
    else:
        rows = list(
            qs.values_list(
                "uuid", "name", "email", "tenant__uuid", "tenant__name"
            )[:limit]
        )

    return [
        JumpClientHit(
            uuid=str(c_uuid),
            name=(c_name or "").strip() or (c_email or "").strip() or "Client",
            email=(c_email or "").strip() or None,
            tenant_uuid=str(t_uuid) if t_uuid else None,
            tenant_name=(t_name or "").strip() or None,
        )
        for c_uuid, c_name, c_email, t_uuid, t_name in rows
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
