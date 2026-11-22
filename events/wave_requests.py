"""Multi-store wave request creation for Account Map wave scheduling.

Reuses batch request validation, deduplication, and retailer linking from
:mod:`events.batch_requests` while accepting structured store + date items
directly from GraphQL (no XLSX round-trip) and materializing approved
``Request`` + ``Event`` + ``Job`` rows in one atomic wave.
"""

from __future__ import annotations

import datetime
from typing import Any
from zoneinfo import ZoneInfo

import strawberry
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from graphql import GraphQLError
from strawberry import relay

from events import models, types
from events.batch_requests import (
    BatchRequestImportResult,
    BatchRequestRowResult,
    _SCHEDULING_ALIASES,
    _dedup_key,
    _ensure_retailer_account,
    _existing_duplicate_uuid,
    _local_datetime_to_utc,
    _optional_float,
    _optional_str,
    _required_date,
    _required_time,
)
from events.market import parse_address_geo
from events.mutations import RequestMutationService
from events.signals import create_pending_jobs_for_request
from recaps.recap_types import recap_type_for_name
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import IsClientOrSparkAdmin

_US_STATE_IANA_TZ: dict[str, str] = {
    "AL": "America/Chicago",
    "AK": "America/Anchorage",
    "AZ": "America/Phoenix",
    "AR": "America/Chicago",
    "CA": "America/Los_Angeles",
    "CO": "America/Denver",
    "CT": "America/New_York",
    "DC": "America/New_York",
    "DE": "America/New_York",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "HI": "Pacific/Honolulu",
    "IA": "America/Chicago",
    "ID": "America/Boise",
    "IL": "America/Chicago",
    "IN": "America/Indiana/Indianapolis",
    "KS": "America/Chicago",
    "KY": "America/New_York",
    "LA": "America/Chicago",
    "MA": "America/New_York",
    "MD": "America/New_York",
    "ME": "America/New_York",
    "MI": "America/Detroit",
    "MN": "America/Chicago",
    "MO": "America/Chicago",
    "MS": "America/Chicago",
    "MT": "America/Denver",
    "NC": "America/New_York",
    "ND": "America/Chicago",
    "NE": "America/Chicago",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NM": "America/Denver",
    "NV": "America/Los_Angeles",
    "NY": "America/New_York",
    "OH": "America/New_York",
    "OK": "America/Chicago",
    "OR": "America/Los_Angeles",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "SD": "America/Chicago",
    "TN": "America/Chicago",
    "TX": "America/Chicago",
    "UT": "America/Denver",
    "VA": "America/New_York",
    "VT": "America/New_York",
    "WA": "America/Los_Angeles",
    "WI": "America/Chicago",
    "WV": "America/New_York",
    "WY": "America/Denver",
}


@strawberry.input
class WaveRequestItemInput:
    store_name: str
    date: str
    start_time: str
    end_time: str
    address: str = ""
    city: str = ""
    state: str = ""
    store_number: str = ""
    latitude: float | None = None
    longitude: float | None = None
    timezone_id: strawberry.ID | None = None
    timezone_code: str | None = None
    name: str | None = None


@strawberry.input
class CreateRequestsWaveInput:
    request_type_id: strawberry.ID
    items: list[WaveRequestItemInput]
    tenant_id: strawberry.ID | None = None
    event_type_id: strawberry.ID | None = None
    default_timezone_id: strawberry.ID | None = None
    scheduling_status: str = "needs_scheduling"
    product_ids: list[strawberry.ID] | None = None
    notes: str = ""
    dry_run: bool = False
    rollback_on_error: bool = True
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class CreateRequestsWaveResponse:
    success: bool
    message: str
    total_rows: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    rolled_back: bool = False
    errors: list[str] | None = None
    rows: list[types.RequestBatchRowResult] | None = None
    client_mutation_id: strawberry.ID | None = None


def _resolve_event_type_id(
    tenant_id: int,
    request_type: models.RequestType,
    explicit_event_type_id: int | None,
) -> int | None:
    if explicit_event_type_id is not None:
        et = models.EventType.objects.filter(
            id=explicit_event_type_id, tenant_id=tenant_id
        ).first()
        if not et:
            raise ValueError("event_type_id does not exist for this brand.")
        return et.id

    target_kind = recap_type_for_name(request_type.name)
    tenant_event_types = list(
        models.EventType.objects.filter(tenant_id=tenant_id).order_by(
            "-is_default", "id"
        )
    )
    for et in tenant_event_types:
        if (et.name or "").strip().casefold() == (
            request_type.name or ""
        ).strip().casefold():
            return et.id
    if target_kind:
        for et in tenant_event_types:
            if recap_type_for_name(et.name) == target_kind:
                return et.id
    if tenant_event_types:
        return tenant_event_types[0].id
    return None


def _resolve_us_iana_tz(
    state_code: str | None,
    latitude: float | None,
    longitude: float | None,
) -> str | None:
    """Resolve an IANA timezone for a US store, honoring GPS coordinates in
    states that span multiple timezones (e.g. FL Panhandle, East TN, West TX,
    Northern ID, Western KY/KS/NE/SD/ND, NW/SW IN, Upper MI, Malheur OR)."""
    clean_state = (state_code or "").strip().upper()
    has_lng = longitude is not None

    if clean_state == "AZ":
        return "America/Phoenix"
    if clean_state == "HI":
        return "Pacific/Honolulu"
    if clean_state == "AK":
        if has_lng and longitude <= -169.5:
            return "America/Adak"
        return "America/Anchorage"

    if clean_state == "FL" and has_lng:
        return "America/Chicago" if longitude < -85.0 else "America/New_York"
    if clean_state == "TN" and has_lng:
        return "America/New_York" if longitude >= -85.6 else "America/Chicago"
    if clean_state == "KY" and has_lng:
        return "America/Chicago" if longitude < -86.0 else "America/New_York"
    if clean_state == "IN" and has_lng:
        if latitude is not None:
            if (latitude >= 41.0 and longitude < -86.8) or (
                latitude < 38.5 and longitude < -87.1
            ):
                return "America/Chicago"
        elif longitude < -87.0:
            return "America/Chicago"
        return "America/Indiana/Indianapolis"
    if clean_state == "MI" and has_lng:
        if (latitude is None or latitude >= 45.1) and longitude < -87.5:
            return "America/Chicago"
        return "America/Detroit"
    if clean_state == "TX" and has_lng:
        return "America/Denver" if longitude <= -104.85 else "America/Chicago"
    if clean_state == "KS" and has_lng:
        return "America/Denver" if longitude <= -101.5 else "America/Chicago"
    if clean_state == "NE" and has_lng:
        return "America/Denver" if longitude <= -100.8 else "America/Chicago"
    if clean_state == "SD" and has_lng:
        return "America/Denver" if longitude <= -100.3 else "America/Chicago"
    if clean_state == "ND" and has_lng:
        if longitude <= -101.5 and (latitude is None or latitude < 47.5):
            return "America/Denver"
        return "America/Chicago"
    if clean_state == "ID" and latitude is not None:
        return "America/Los_Angeles" if latitude >= 45.55 else "America/Boise"
    if clean_state == "OR" and has_lng:
        if longitude >= -117.5 and (latitude is None or latitude >= 43.2):
            return "America/Boise"
        return "America/Los_Angeles"

    if clean_state in _US_STATE_IANA_TZ:
        return _US_STATE_IANA_TZ[clean_state]

    if has_lng:
        if longitude <= -150.0:
            return "Pacific/Honolulu"
        if longitude <= -130.0:
            return "America/Anchorage"
        if longitude <= -114.5:
            return "America/Los_Angeles"
        if longitude <= -102.0:
            return "America/Denver"
        if longitude <= -85.5:
            return "America/Chicago"
        return "America/New_York"

    return None


def _infer_tz_for_date(
    *,
    timezones_by_id: dict[int, models.TimeZone],
    timezones_by_code: dict[str, models.TimeZone],
    explicit_tz_id: int | None,
    explicit_tz_code: str | None,
    state_code: str | None,
    latitude: float | None = None,
    longitude: float | None,
    event_date: datetime.date,
    default_tz_id: int | None,
) -> models.TimeZone:
    if explicit_tz_id is not None:
        tz = timezones_by_id.get(explicit_tz_id)
        if not tz:
            raise ValueError("timezone_id does not exist.")
        return tz

    if explicit_tz_code:
        tz = timezones_by_code.get(explicit_tz_code.strip().upper())
        if not tz:
            raise ValueError(f"Unknown timezone_code '{explicit_tz_code}'.")
        return tz

    iana_name = _resolve_us_iana_tz(state_code, latitude, longitude)

    if iana_name:
        probe = datetime.datetime(
            event_date.year,
            event_date.month,
            event_date.day,
            12,
            0,
            tzinfo=ZoneInfo(iana_name),
        )
        abbr = (probe.tzname() or "").upper()
        if abbr in timezones_by_code:
            return timezones_by_code[abbr]
        offset_hours = int(round((probe.utcoffset() or datetime.timedelta()).total_seconds() / 3600))
        for candidate in timezones_by_id.values():
            raw_off = int(candidate.offset or 0)
            cand_hours = raw_off // 60 if abs(raw_off) > 24 else raw_off
            if cand_hours == offset_hours:
                return candidate

    if default_tz_id is not None and default_tz_id in timezones_by_id:
        return timezones_by_id[default_tz_id]

    if timezones_by_id:
        return next(iter(timezones_by_id.values()))

    raise ValueError("No timezone configured in Spark.")


def create_requests_from_wave(
    *,
    tenant_id: int,
    created_by,
    request_type_id: int,
    event_type_id: int | None,
    default_timezone_id: int | None,
    scheduling_status_raw: str,
    product_ids: list[int],
    notes: str,
    items: list[dict[str, Any]],
    dry_run: bool = False,
    rollback_on_error: bool = True,
) -> BatchRequestImportResult:
    if not items:
        raise ValueError("Select at least one store visit to schedule.")

    tenant = models.Tenant.objects.filter(id=tenant_id).first()
    if not tenant:
        raise ValueError("Selected tenant does not exist.")

    request_type = models.RequestType.objects.filter(
        id=request_type_id, tenant_id=tenant_id
    ).first()
    if not request_type:
        raise ValueError("Selected activation type does not belong to this brand.")

    resolved_event_type_id = _resolve_event_type_id(
        tenant_id=tenant_id,
        request_type=request_type,
        explicit_event_type_id=event_type_id,
    )

    sched_key = (scheduling_status_raw or "needs_scheduling").strip().lower()
    scheduling_status = _SCHEDULING_ALIASES.get(sched_key)
    if not scheduling_status:
        raise ValueError(
            "scheduling_status must be 'already_scheduled' or 'needs_scheduling'."
        )

    valid_products: list[int] = []
    for pid in product_ids:
        if not models.Product.objects.filter(id=pid, tenant_id=tenant_id).exists():
            raise ValueError(f"Product {pid} does not belong to this brand.")
        if pid not in valid_products:
            valid_products.append(pid)

    approved_status = models.RequestStatus.objects.get_by_slug(
        slug="approved", tenant=tenant_id
    )
    if not approved_status:
        approved_status = (
            models.RequestStatus.objects.filter(tenant_id=tenant_id)
            .order_by("-is_default", "id")
            .first()
        )
    if not approved_status:
        raise ValueError("Approved request status is not configured for this brand.")

    approved_event_status = models.EventStatus.objects.get_by_slug(
        slug="approved", tenant=tenant_id
    )
    if not approved_event_status:
        approved_event_status = (
            models.EventStatus.objects.filter(tenant_id=tenant_id)
            .order_by("-is_default", "id")
            .first()
        )
    if not approved_event_status:
        raise ValueError("Approved event status is not configured for this brand.")

    all_timezones = list(models.TimeZone.objects.order_by("id"))
    timezones_by_id = {tz.id: tz for tz in all_timezones}
    timezones_by_code = {
        (tz.code or "").strip().upper(): tz
        for tz in all_timezones
        if (tz.code or "").strip()
    }

    clean_notes = _optional_str(notes)
    requestor_email = _optional_str(getattr(created_by, "email", None))

    results: list[BatchRequestRowResult] = []
    success_count = 0
    failed_count = 0
    seen_keys: set[tuple[str, str]] = set()
    created_requests: list[models.Request] = []

    def _process() -> None:
        nonlocal success_count, failed_count

        for idx, raw in enumerate(items):
            row_number = idx + 1
            try:
                store_name = (_optional_str(raw.get("store_name")) or "").strip()
                address = (_optional_str(raw.get("address")) or "").strip()
                if not store_name and not address:
                    raise ValueError("Store name or street address is required.")
                if not address:
                    address = store_name

                event_date = _required_date(raw, "date")
                start_clock = _required_time(raw, "start_time")
                end_clock = _required_time(raw, "end_time")

                latitude = _optional_float(raw.get("latitude"), "latitude")
                longitude = _optional_float(raw.get("longitude"), "longitude")
                if latitude is not None and not (-90 <= latitude <= 90):
                    raise ValueError("latitude must be between -90 and 90.")
                if longitude is not None and not (-180 <= longitude <= 180):
                    raise ValueError("longitude must be between -180 and 180.")
                if (latitude is None) != (longitude is None):
                    latitude = None
                    longitude = None

                geo = parse_address_geo(address)
                city_text = (
                    _optional_str(raw.get("city")) or (geo.city or "").strip() or None
                )
                state_text = (
                    _optional_str(raw.get("state"))
                    or (geo.state_code or "").strip()
                    or None
                )

                state_id: int | None = None
                resolved_state_code: str | None = None
                if state_text:
                    state_obj = models.State.objects.filter(
                        Q(code__iexact=state_text) | Q(name__iexact=state_text)
                    ).first()
                    if state_obj:
                        state_id = state_obj.id
                        resolved_state_code = (state_obj.code or "").upper()
                    elif len(state_text.strip()) == 2:
                        resolved_state_code = state_text.strip().upper()

                location_id: int | None = None
                if city_text:
                    loc_qs = models.Location.objects.filter(name__iexact=city_text)
                    if state_id:
                        loc_qs = loc_qs.filter(state_id=state_id)
                    if loc_qs.count() == 1:
                        loc_obj = loc_qs.first()
                        location_id = loc_obj.id
                        if not state_id and loc_obj.state_id:
                            state_id = loc_obj.state_id

                item_tz_id = raw.get("timezone_id")
                tz = _infer_tz_for_date(
                    timezones_by_id=timezones_by_id,
                    timezones_by_code=timezones_by_code,
                    explicit_tz_id=int(item_tz_id) if item_tz_id is not None else None,
                    explicit_tz_code=_optional_str(raw.get("timezone_code")),
                    state_code=resolved_state_code,
                    latitude=latitude,
                    longitude=longitude,
                    event_date=event_date,
                    default_tz_id=default_timezone_id,
                )

                local_date_dt = datetime.datetime.combine(
                    event_date, datetime.time(0, 0)
                )
                local_start_dt = datetime.datetime.combine(event_date, start_clock)
                end_date = (
                    event_date + datetime.timedelta(days=1)
                    if end_clock <= start_clock
                    else event_date
                )
                local_end_dt = datetime.datetime.combine(end_date, end_clock)

                utc_date = _local_datetime_to_utc(local_date_dt, tz.offset)
                utc_start = _local_datetime_to_utc(local_start_dt, tz.offset)
                utc_end = _local_datetime_to_utc(local_end_dt, tz.offset)

                store_number = _optional_str(raw.get("store_number")) or ""
                custom_name = _optional_str(raw.get("name"))
                if custom_name:
                    request_name = custom_name[:200]
                else:
                    parts = [request_type.name or "Visit"]
                    if store_name:
                        parts.append(store_name)
                    parts.append(event_date.strftime("%m/%d/%Y"))
                    request_name = " · ".join(parts)[:200]

                parsed_for_dedup = {
                    "store_number": store_number,
                    "address": address,
                    "start_time": utc_start,
                }
                key = _dedup_key(parsed_for_dedup)
                if key is not None and key in seen_keys:
                    results.append(
                        BatchRequestRowResult(
                            row_number=row_number,
                            success=False,
                            skipped=True,
                            message="Skipped — duplicate store and start time in this wave.",
                        )
                    )
                    continue
                existing_uuid = _existing_duplicate_uuid(tenant_id, parsed_for_dedup)
                if existing_uuid:
                    if key is not None:
                        seen_keys.add(key)
                    results.append(
                        BatchRequestRowResult(
                            row_number=row_number,
                            success=False,
                            skipped=True,
                            message=f"Skipped — already scheduled in Spark (request {existing_uuid}).",
                        )
                    )
                    continue
                if key is not None:
                    seen_keys.add(key)

                coords_req = (
                    [latitude, longitude]
                    if latitude is not None and longitude is not None
                    else []
                )
                coords_ev = (
                    [latitude, longitude]
                    if latitude is not None and longitude is not None
                    else None
                )

                request_obj = models.Request(
                    tenant_id=tenant_id,
                    created_by=created_by,
                    approved_by=created_by,
                    status=approved_status,
                    name=request_name,
                    date=utc_date,
                    start_time=utc_start,
                    end_time=utc_end,
                    address=address,
                    store_number=store_number,
                    scheduling_status=scheduling_status,
                    notes=clean_notes,
                    coordinates=coords_req,
                    requestor_email=requestor_email,
                    retailer_name=store_name or None,
                    retailer_address=address or None,
                    timezone_id=tz.id,
                    request_type_id=request_type.id,
                    location_id=location_id,
                    state_id=state_id,
                )

                if not dry_run:
                    if store_name:
                        request_obj.retailer_id = _ensure_retailer_account(
                            tenant_id,
                            store_name,
                            location_id,
                            created_by,
                        )
                    request_obj.save()

                    for pid in valid_products:
                        models.RequestProduct.objects.create(
                            request=request_obj,
                            product_id=pid,
                            tenant_id=tenant_id,
                            created_by=created_by,
                        )

                    models.Event.objects.create(
                        tenant_id=tenant_id,
                        request=request_obj,
                        event_type_id=resolved_event_type_id,
                        status=approved_event_status,
                        timezone_id=tz.id,
                        name=request_name,
                        date=utc_date,
                        start_time=utc_start,
                        end_time=utc_end,
                        address=address,
                        notes=clean_notes,
                        coordinates=coords_ev,
                        retailer_id=request_obj.retailer_id,
                        location_id=location_id,
                        state_id=state_id,
                        created_by=created_by,
                    )

                    models.RequestActivityLog.objects.create(
                        tenant_id=tenant_id,
                        request=request_obj,
                        kind=models.RequestActivityLog.KIND_CREATED,
                        actor_user=(
                            created_by
                            if getattr(created_by, "id", None)
                            else None
                        ),
                        summary=(
                            f"Scheduled via Account Map wave · {store_name or address}"
                        )[:255],
                        metadata={"source": "account_map_wave"},
                    )
                    created_requests.append(request_obj)

                success_count += 1
                results.append(
                    BatchRequestRowResult(
                        row_number=row_number,
                        success=True,
                        message="Validated (dry-run)." if dry_run else "Scheduled.",
                        request_id=request_obj.id if not dry_run else None,
                        request_uuid=str(request_obj.uuid) if not dry_run else None,
                    )
                )
            except Exception as exc:
                failed_count += 1
                results.append(
                    BatchRequestRowResult(
                        row_number=row_number,
                        success=False,
                        message=str(exc),
                    )
                )

    rolled_back = False
    if dry_run or not rollback_on_error:
        _process()
    else:
        with transaction.atomic():
            _process()
            if failed_count > 0:
                rolled_back = True
                transaction.set_rollback(True)

    if rolled_back:
        success_count = 0
        created_requests.clear()
        for row in results:
            if row.success:
                row.success = False
                row.request_id = None
                row.request_uuid = None
                row.message = "Rolled back because another store row failed."
        failed_count = sum(1 for r in results if not r.success and not r.skipped)

    if not dry_run and not rolled_back:
        for req in created_requests:
            create_pending_jobs_for_request(req)

    skipped_count = sum(1 for r in results if r.skipped)
    return BatchRequestImportResult(
        total_rows=len(items),
        success_count=success_count,
        failed_count=failed_count,
        rows=results,
        rolled_back=rolled_back,
        skipped_count=skipped_count,
    )


@strawberry.type
class WaveRequestMutations:
    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def create_requests_wave(
        self,
        info: strawberry.Info,
        input: CreateRequestsWaveInput,
    ) -> CreateRequestsWaveResponse:
        """Schedule a multi-store wave of approved requests from the Account Map."""
        try:
            service = RequestMutationService()
            user = info.context.request.user
            if not user or not user.is_authenticated:
                raise GraphQLError("Authentication required.")

            is_admin = await service.is_admin_request(info)
            if is_admin and input.tenant_id:
                resolved_tenant_id = resolve_id_to_int(input.tenant_id)
                tenant_exists = await sync_to_async(
                    models.Tenant.objects.filter(id=resolved_tenant_id).exists
                )()
                if not tenant_exists:
                    raise GraphQLError("Selected tenant does not exist.")
            else:
                resolved_tenant_id = (
                    resolve_id_to_int(input.tenant_id)
                    if input.tenant_id
                    else await service.get_user_tenant(user, info)
                )
                await service.get_tenant(user, resolved_tenant_id)

            max_rows = int(
                getattr(
                    settings,
                    "ABUSE_IMPORT_MAX_ROWS",
                    500,
                )
            )
            if not is_admin and len(input.items or []) > max_rows:
                return CreateRequestsWaveResponse(
                    success=False,
                    message=(
                        f"Wave contains too many store visits ({len(input.items)}); "
                        f"maximum per wave is {max_rows}."
                    ),
                    errors=[f"Maximum {max_rows} store visits per wave."],
                    client_mutation_id=input.client_mutation_id,
                )

            req_type_id = resolve_id_to_int(input.request_type_id)
            ev_type_id = (
                resolve_id_to_int(input.event_type_id)
                if input.event_type_id
                else None
            )
            def_tz_id = (
                resolve_id_to_int(input.default_timezone_id)
                if input.default_timezone_id
                else None
            )
            prod_ids = [
                resolve_id_to_int(pid) for pid in (input.product_ids or []) if pid
            ]

            serialized_items: list[dict[str, Any]] = []
            for item in input.items or []:
                serialized_items.append(
                    {
                        "store_name": item.store_name,
                        "address": item.address,
                        "city": item.city,
                        "state": item.state,
                        "store_number": item.store_number,
                        "latitude": item.latitude,
                        "longitude": item.longitude,
                        "date": item.date,
                        "start_time": item.start_time,
                        "end_time": item.end_time,
                        "timezone_id": (
                            resolve_id_to_int(item.timezone_id)
                            if item.timezone_id
                            else None
                        ),
                        "timezone_code": item.timezone_code,
                        "name": item.name,
                    }
                )

            result = await sync_to_async(
                create_requests_from_wave, thread_sensitive=True
            )(
                tenant_id=resolved_tenant_id,
                created_by=user,
                request_type_id=req_type_id,
                event_type_id=ev_type_id,
                default_timezone_id=def_tz_id,
                scheduling_status_raw=input.scheduling_status,
                product_ids=prod_ids,
                notes=input.notes,
                items=serialized_items,
                dry_run=input.dry_run,
                rollback_on_error=input.rollback_on_error,
            )

            row_results = [
                types.RequestBatchRowResult(
                    row_number=row.row_number,
                    success=row.success,
                    skipped=row.skipped,
                    message=row.message,
                    request_id=(
                        strawberry.ID(str(row.request_id))
                        if row.request_id is not None
                        else None
                    ),
                    request_uuid=row.request_uuid,
                )
                for row in result.rows
            ]

            success = result.failed_count == 0
            skip_note = (
                f" ({result.skipped_count} duplicate{'s' if result.skipped_count != 1 else ''} skipped)"
                if result.skipped_count
                else ""
            )
            if input.dry_run:
                msg = (
                    f"Wave preview validated: {result.success_count} ready{skip_note}."
                    if success
                    else f"Wave preview found {result.failed_count} error(s)."
                )
            elif result.rolled_back:
                msg = (
                    "Wave rolled back due to validation errors. No requests were created."
                )
            elif success:
                msg = (
                    f"Scheduled {result.success_count} store visit"
                    f"{'s' if result.success_count != 1 else ''}{skip_note}."
                )
            else:
                msg = (
                    f"Wave completed with errors ({result.success_count} scheduled, "
                    f"{result.failed_count} failed{skip_note})."
                )

            errors = [
                f"Row {r.row_number}: {r.message}"
                for r in result.rows
                if not r.success and not r.skipped
            ]
            return CreateRequestsWaveResponse(
                success=success,
                message=msg,
                total_rows=result.total_rows,
                success_count=result.success_count,
                failed_count=result.failed_count,
                skipped_count=result.skipped_count,
                rolled_back=result.rolled_back,
                errors=errors or None,
                rows=row_results,
                client_mutation_id=input.client_mutation_id,
            )
        except (GraphQLError, ValueError) as exc:
            err_text = exc.message if isinstance(exc, GraphQLError) else str(exc)
            return CreateRequestsWaveResponse(
                success=False,
                message=err_text,
                errors=[err_text],
                client_mutation_id=input.client_mutation_id,
            )
        except Exception as exc:  # noqa: BLE001
            err_text = f"Unexpected error scheduling wave: {exc}"
            return CreateRequestsWaveResponse(
                success=False,
                message=err_text,
                errors=[err_text],
                client_mutation_id=input.client_mutation_id,
            )
