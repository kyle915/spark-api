"""A brand's uploaded account list, saved per tenant (Account Map).

``accountListEntries(tenantId)`` reads it, ``saveAccountList`` adds an
uploaded file's rows to it, ``placeAccountListEntries`` looks up map positions
for rows that have an address and no position yet.

Who may use it: Ignite staff for any brand; a brand's own users for that
brand only. Another brand's id is refused, and being logged in is not enough
(same rule as #1386). Nothing here deletes a row.

Positions: the lookup reuses ``events.geocoding`` (street-level results only,
key never logged). It runs in small batches the page asks for, with a time
limit, so one 500-row upload can never hold a request open.
"""

from __future__ import annotations

import hashlib
import time

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from events import geocoding
from events.map_points import address_hash, is_lookup_worthy, normalise_address
from events.staffing_board import _accessible_tenants
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import IsClientOrSparkAdmin

# One upload, and one brand's whole list.
MAX_ROWS_PER_SAVE = 2000
MAX_ROWS_PER_TENANT = 10000

# One placing call: at most this many lookups, and stop starting new ones
# after this long.
PLACE_BATCH_MAX = 40
PLACE_TIME_BUDGET_SECONDS = 8.0
MAX_CONSECUTIVE_FAILURES = 3


@strawberry.type
class AccountListEntryType:
    uuid: strawberry.ID
    name: str
    address: str
    city: str
    state: str
    zip: str
    chain: str
    notes: str
    lat: float | None
    lng: float | None
    # "pending" | "ok" | "provided" | "not_found" | "no_address"
    position_status: str


@strawberry.input
class AccountListEntryInput:
    name: str
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    chain: str = ""
    notes: str = ""
    lat: float | None = None
    lng: float | None = None


@strawberry.type
class AccountListSaveResult:
    added: int
    duplicates: int
    # Rows with no name, which cannot be an account.
    skipped: int
    total: int
    # Rows that have an address and still need a map position.
    awaiting_position: int


@strawberry.type
class AccountListPlaceResult:
    placed: int
    not_found: int
    # Rows that still need a position after this call.
    remaining: int
    # False when the address lookup is off or failing right now.
    lookup_available: bool


def _clean(value, limit: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] if limit else text


def full_address(address: str, city: str, state: str, zip_code: str) -> str:
    """One line to look up: street, city, "ST 12345"."""
    tail = " ".join(part for part in (_clean(state), _clean(zip_code)) if part)
    return ", ".join(part for part in (_clean(address), _clean(city), tail) if part)


def dedupe_key(name: str, address: str, city: str, state: str, zip_code: str) -> str:
    raw = f"{normalise_address(name)}|{normalise_address(full_address(address, city, state, zip_code))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _resolve_tenant(info: strawberry.Info, tenant_id) -> int:
    """The tenant id, after checking this user may use that brand's list."""
    try:
        resolved = resolve_id_to_int(tenant_id)
    except Exception as exc:  # noqa: BLE001
        raise GraphQLError("Invalid tenant id.") from exc
    is_admin, allowed = await _accessible_tenants(info.context.request.user)
    if not is_admin and resolved not in (allowed or set()):
        raise GraphQLError("Not allowed for this tenant.")
    return resolved


def _to_type(row) -> AccountListEntryType:
    return AccountListEntryType(
        uuid=str(row.uuid),
        name=row.name,
        address=row.address,
        city=row.city,
        state=row.state,
        zip=row.zip,
        chain=row.chain,
        notes=row.notes,
        lat=row.lat,
        lng=row.lng,
        position_status=row.position_status,
    )


def list_entries(tenant_id: int) -> list:
    from events.models import AccountListEntry

    return list(AccountListEntry.objects.filter(tenant_id=tenant_id).order_by("name", "id"))


def save_entries(tenant_id: int, rows, user) -> AccountListSaveResult:
    """Add ``rows`` to the tenant's list. A store already on the list (same
    name and address) is left exactly as it is."""
    from events.models import AccountListEntry
    from tenants.models import Tenant

    if not Tenant.objects.filter(pk=tenant_id).exists():
        raise GraphQLError("Tenant not found.")
    rows = list(rows or [])
    if len(rows) > MAX_ROWS_PER_SAVE:
        raise GraphQLError(
            f"That file has {len(rows):,} rows. Upload at most {MAX_ROWS_PER_SAVE:,} at a time."
        )
    existing = set(
        AccountListEntry.objects.filter(tenant_id=tenant_id).values_list(
            "dedupe_key", flat=True
        )
    )
    to_create = []
    duplicates = skipped = 0
    for row in rows:
        name = _clean(row.name, 255)
        if not name:
            skipped += 1
            continue
        address = _clean(row.address)
        city = _clean(row.city, 120)
        state = _clean(row.state, 64)
        zip_code = _clean(row.zip, 20)
        key = dedupe_key(name, address, city, state, zip_code)
        if key in existing:
            duplicates += 1
            continue
        existing.add(key)
        lookup = full_address(address, city, state, zip_code)
        entry = AccountListEntry(
            tenant_id=tenant_id,
            name=name,
            address=address,
            city=city,
            state=state,
            zip=zip_code,
            chain=_clean(row.chain, 255),
            notes=_clean(row.notes),
            dedupe_key=key,
            address_hash=address_hash(lookup) if lookup else "",
            created_by=user if getattr(user, "pk", None) else None,
        )
        if geocoding.valid_lat_lng(row.lat, row.lng):
            entry.lat, entry.lng = float(row.lat), float(row.lng)
            entry.position_status = AccountListEntry.POSITION_PROVIDED
        elif is_lookup_worthy(lookup):
            entry.position_status = AccountListEntry.POSITION_PENDING
        else:
            entry.position_status = AccountListEntry.POSITION_NO_ADDRESS
        to_create.append(entry)

    if len(existing) > MAX_ROWS_PER_TENANT:
        raise GraphQLError(
            f"An account list holds at most {MAX_ROWS_PER_TENANT:,} stores."
        )
    if to_create:
        # ignore_conflicts: two people uploading the same file at once must
        # not fail the second one on the unique (tenant, store) constraint.
        AccountListEntry.objects.bulk_create(
            to_create, batch_size=500, ignore_conflicts=True
        )
    base = AccountListEntry.objects.filter(tenant_id=tenant_id)
    return AccountListSaveResult(
        added=len(to_create),
        duplicates=duplicates,
        skipped=skipped,
        total=base.count(),
        awaiting_position=base.filter(
            position_status=AccountListEntry.POSITION_PENDING
        ).count(),
    )


def _known_position(address_hash_value: str) -> tuple[float, float] | None:
    """A position already looked up for this exact address, by anyone."""
    from events.models import AccountListEntry, EventMapPoint

    if not address_hash_value:
        return None
    for row in (
        AccountListEntry.objects.filter(
            address_hash=address_hash_value,
            position_status=AccountListEntry.POSITION_OK,
        ).values_list("lat", "lng")[:1]
    ):
        if geocoding.valid_lat_lng(*row):
            return float(row[0]), float(row[1])
    for row in (
        EventMapPoint.objects.filter(
            address_hash=address_hash_value, status=EventMapPoint.STATUS_OK
        ).values_list("lat", "lng")[:1]
    ):
        if geocoding.valid_lat_lng(*row):
            return float(row[0]), float(row[1])
    return None


def place_entries(tenant_id: int, limit: int = PLACE_BATCH_MAX) -> AccountListPlaceResult:
    """Look up positions for up to ``limit`` pending rows of one tenant.

    Each distinct address costs one lookup, ever: a found position is copied
    to every pending row with that address, and an address with no
    street-level spot is remembered as ``not_found``. A failed call (no key,
    quota, network) stores nothing, so the row is tried again next time.
    """
    from events.models import AccountListEntry

    limit = max(1, min(int(limit or PLACE_BATCH_MAX), PLACE_BATCH_MAX))
    pending = AccountListEntry.objects.filter(
        tenant_id=tenant_id, position_status=AccountListEntry.POSITION_PENDING
    )
    placed = not_found = failures = 0
    lookup_available = True
    started = time.monotonic()
    for row in list(pending.order_by("id")[:limit]):
        if time.monotonic() - started > PLACE_TIME_BUDGET_SECONDS:
            break
        same_address = pending.filter(address_hash=row.address_hash)
        known = _known_position(row.address_hash)
        if known is not None:
            placed += same_address.update(
                lat=known[0], lng=known[1], position_status=AccountListEntry.POSITION_OK
            )
            continue
        outcome, point, _detail = geocoding.lookup_address(
            full_address(row.address, row.city, row.state, row.zip)
        )
        if outcome == geocoding.OK and point is not None:
            failures = 0
            placed += same_address.update(
                lat=point[0], lng=point[1], position_status=AccountListEntry.POSITION_OK
            )
        elif outcome == geocoding.NOT_FOUND:
            failures = 0
            not_found += same_address.update(
                position_status=AccountListEntry.POSITION_NOT_FOUND
            )
        else:
            failures += 1
            if outcome == geocoding.NO_KEY or failures >= MAX_CONSECUTIVE_FAILURES:
                lookup_available = False
                break
    return AccountListPlaceResult(
        placed=placed,
        not_found=not_found,
        remaining=pending.count(),
        lookup_available=lookup_available,
    )


@strawberry.type
class AccountListQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def account_list_entries(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> list[AccountListEntryType]:
        """The uploaded account list of one brand."""
        resolved = await _resolve_tenant(info, tenant_id)
        rows = await sync_to_async(list_entries)(resolved)
        return [_to_type(row) for row in rows]


@strawberry.type
class AccountListMutations:
    @strawberry.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def save_account_list(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        accounts: list[AccountListEntryInput],
    ) -> AccountListSaveResult:
        """Add uploaded rows to a brand's account list. Never removes any."""
        resolved = await _resolve_tenant(info, tenant_id)
        user = info.context.request.user
        return await sync_to_async(save_entries)(resolved, accounts, user)

    @strawberry.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def place_account_list_entries(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        limit: int | None = None,
    ) -> AccountListPlaceResult:
        """Look up map positions for a batch of rows that still need one."""
        resolved = await _resolve_tenant(info, tenant_id)
        return await sync_to_async(place_entries)(resolved, limit or PLACE_BATCH_MAX)
