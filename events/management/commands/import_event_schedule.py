"""Bulk-create a client's activation schedule from a committed JSON file.

Kyle's clients hand over a season's worth of activations as a spreadsheet
(e.g. "Internal x Stone House Bread — Q2-Q3 2026.xlsx"): one row per
store/date/time. This command turns a normalized JSON snapshot of that
schedule into approved Requests + approved Events on the tenant's Master
Tracker — reusing the SAME battle-tested importer the admin Bulk Upload UI
calls (events.batch_requests.import_requests_from_excel_bytes), so we get
its row-level validation, per-store+start-time DEDUP, atomic rollback, and
retailer-account linking for free.

Why a command (not just the UI): it resolves the tenant + the "Retail
Sampling" EventType/RequestType + the Eastern TimeZone BY NAME in prod, so
nobody has to hand-copy tenant-specific IDs into a sheet. Run it through the
secret-gated cron endpoint (digest.cron_views.ImportEventScheduleView) +
the import-event-schedule GitHub workflow.

SAFE — DRY-RUN IS THE DEFAULT. Without --commit the 74 events are validated
but NOT written (the importer's dry_run). The only writes a dry-run makes
are the idempotent tenant SETUP rows (EventType / RequestType / approved
statuses) — get_or_create, so re-runs are no-ops — which must exist for the
event rows to validate at all. The report prints the resolved tenant, IDs,
the timezone (with a sample wall-clock→UTC conversion so you can eyeball
that 3 PM stays 3 PM), and per-row outcomes.

Schedules live in events/management/commands/data/<key>.json with shape:
    {"tenant_name" | "tenant_slug", "event_type", "request_type",
     "scheduling_status",
     "fuzzy_dedup" (optional — also skip rows already in Spark at the same
                    store within an hour, matched on street address or
                    name; for schedules typed by hand, whose addresses
                    won't equal what Spark stored),
     "mirror_to_sheet" (optional, default true — false skips the per-save
                        linked-Sheet mirror for big imports),
     "link_walkin_events" (optional — a row a BA already worked off a
                           standing walk-up link adopts that request-less
                           Event instead of getting a second one),
     "rows": [{name, date(mm/dd/yyyy), start_time(HH:MM), end_time(HH:MM),
               address, store_number, retailer_name, city, state,
               store_manager_phone, notes,
               timezone_code (optional — per-row override for schedules
               that span time zones; rows without it use the command-level
               timezone)}, ...]}
"""

from __future__ import annotations

import datetime
import io
import json
import re
from contextlib import nullcontext
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from openpyxl import Workbook

from ambassadors.checkin_web import (
    address_core_parts,
    addresses_fuzzy_match,
    event_calendar_date,
    normalize_place,
)
from events.batch_requests import (
    TEMPLATE_COLUMNS,
    _local_datetime_to_utc,
    _normalize_offset_minutes,
    import_requests_from_excel_bytes,
)
from events.models import (
    Event,
    EventStatus,
    EventType,
    Request,
    RequestStatus,
    RequestType,
    TimeZone,
)
from tenants.models import Tenant
from utils.sheets_mirror import suppress_sheet_mirror

User = get_user_model()

_DATA_DIR = Path(__file__).resolve().parent / "data"
# Only [a-z0-9_] schedule keys — the key maps straight to a filename, so
# this guards against path traversal (../) reaching outside data/.
_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_FUZZY_DEDUP_WINDOW = datetime.timedelta(minutes=60)


class Command(BaseCommand):
    help = (
        "Bulk-create a client's activation schedule (approved events on the "
        "Master Tracker) from a committed JSON file. Dry-run by default; pass "
        "--commit to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schedule",
            default="stone_house_q2q3_2026",
            help="Schedule key → events/management/commands/data/<key>.json",
        )
        parser.add_argument(
            "--tenant-name",
            default=None,
            help="Override the tenant name in the JSON (case-insensitive).",
        )
        parser.add_argument(
            "--owner-email",
            default="kyle@igniteproductions.co",
            help="User recorded as created_by on the rows.",
        )
        parser.add_argument(
            "--timezone-code",
            default=None,
            help=(
                "Force a TimeZone code (e.g. EDT/EST). Default: auto-resolve "
                "Eastern, preferring daylight (EDT) for these summer dates."
            ),
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually write. Without this flag the import is a dry-run.",
        )
        parser.add_argument(
            "--create-tenant",
            action="store_true",
            help="If no tenant matches the name, create one (name + slug, "
            "owned by --owner-email) instead of failing. Like the EventType "
            "setup, this happens even on a dry-run — it's idempotent tenant "
            "config the rows can't validate without.",
        )

    def handle(self, *args, **opts):
        commit = bool(opts["commit"])
        schedule_key = (opts["schedule"] or "").strip().lower()
        if not _KEY_RE.match(schedule_key):
            raise CommandError(f"Invalid --schedule key: {schedule_key!r}")
        data_path = _DATA_DIR / f"{schedule_key}.json"
        if not data_path.exists():
            raise CommandError(f"Schedule file not found: {data_path}")

        spec = json.loads(data_path.read_text())
        rows = spec.get("rows") or []
        if not rows:
            raise CommandError(f"No rows in {data_path}")

        tenant_name = (opts["tenant_name"] or spec.get("tenant_name") or "").strip()
        tenant_slug = "" if opts["tenant_name"] else (spec.get("tenant_slug") or "").strip()
        event_type_name = (spec.get("event_type") or "Retail Sampling").strip()
        request_type_name = (spec.get("request_type") or "Retail Sampling").strip()
        scheduling_status = (spec.get("scheduling_status") or "already_scheduled").strip()

        w = self.stdout.write
        w("")
        w(self.style.MIGRATE_HEADING(f"Import event schedule: {schedule_key}"))
        w(f"  mode      : {'COMMIT (writing)' if commit else 'DRY-RUN (no event writes)'}")
        w(f"  rows      : {len(rows)}")
        w(f"  tenant    : {tenant_slug or tenant_name!r}")

        # ---- Resolve owner -------------------------------------------------
        # (Before the tenant: --create-tenant needs it as created_by.)
        owner = User.objects.filter(email__iexact=opts["owner_email"]).order_by("id").first()
        if not owner:
            raise CommandError(f"Owner user not found: {opts['owner_email']}")
        w(f"  owner     : {owner.id} ({owner.email})")

        # ---- Resolve tenant ------------------------------------------------
        if tenant_slug:
            tenant = (
                Tenant.objects.filter(slug=tenant_slug).order_by("id").first()
                or Tenant.objects.filter(request_url_name=tenant_slug).order_by("id").first()
            )
            if not tenant:
                raise CommandError(f"Tenant not found by slug {tenant_slug!r}.")
        else:
            tenant = (
                Tenant.objects.filter(name__iexact=tenant_name).order_by("id").first()
                if tenant_name
                else None
            )
        if not tenant and tenant_name and opts["create_tenant"]:
            tenant = Tenant.objects.create(
                name=tenant_name,
                slug=_slugify(tenant_name)[:50],
                created_by=owner,
            )
            w(
                self.style.SUCCESS(
                    f"  tenant    : CREATED {tenant.id} "
                    f"({tenant.name}, slug={tenant.slug})"
                )
            )
        if not tenant:
            candidates = list(
                Tenant.objects.order_by("name").values_list("name", flat=True)[:40]
            )
            raise CommandError(
                f"Tenant not found by name {tenant_name!r}. "
                f"Existing tenants: {', '.join(candidates) or '(none)'}"
            )
        w(f"  tenant id : {tenant.id} ({tenant.name})")

        # ---- Ensure tenant setup (idempotent get_or_create) ----------------
        # These rows MUST exist for the importer to validate the event rows.
        # They're safe, reusable tenant config — created even in dry-run so the
        # dry-run truly validates the 74 events.
        event_type, et_created = EventType.objects.get_or_create(
            tenant=tenant,
            name=event_type_name,
            defaults={
                "slug": _slugify(event_type_name),
                "is_default": not EventType.objects.filter(tenant=tenant).exists(),
                "created_by": owner,
            },
        )
        request_type, rt_created = RequestType.objects.get_or_create(
            tenant=tenant,
            name=request_type_name,
            defaults={"created_by": owner},
        )
        event_status, es_created = EventStatus.objects.get_or_create(
            tenant=tenant,
            slug="approved",
            defaults={"name": "Approved", "created_by": owner},
        )
        request_status, rs_created = RequestStatus.objects.get_or_create(
            tenant=tenant,
            slug="approved",
            defaults={"name": "Approved", "created_by": owner},
        )
        w(
            f"  event type   : {event_type.id} ({event_type.name})"
            f"{' [created]' if et_created else ''}"
        )
        w(
            f"  request type : {request_type.id} ({request_type.name})"
            f"{' [created]' if rt_created else ''}"
        )
        w(
            f"  event status : {event_status.id} approved"
            f"{' [created]' if es_created else ''}"
            f"  · request status {request_status.id}"
            f"{' [created]' if rs_created else ''}"
        )

        # ---- Resolve timezone ---------------------------------------------
        tz = self._resolve_timezone(opts["timezone_code"])
        if not tz:
            raise CommandError(
                "Could not resolve an Eastern TimeZone. Pass --timezone-code "
                "with a code from the TimeZones table."
            )
        # Sample conversion so the operator can confirm DST/wall-clock before
        # committing (these are June/July = EDT). 15:00 local should display
        # back as 15:00 regardless of offset, but the stored UTC reveals it.
        sample = rows[0]
        local = datetime.datetime.combine(
            _parse_date(sample["date"]), _parse_time(sample["start_time"])
        )
        stored_utc = _local_datetime_to_utc(local, tz.offset)
        off_min = _normalize_offset_minutes(tz.offset)
        w(
            f"  timezone     : {tz.id} {tz.code} '{tz.name}' offset={tz.offset} "
            f"({off_min} min)"
        )
        w(
            f"  sample       : {sample['date']} {sample['start_time']} {tz.code} "
            f"→ stored {stored_utc.isoformat()} → displays "
            f"{(stored_utc + datetime.timedelta(minutes=off_min)).strftime('%H:%M')}"
        )

        report: list[dict] = []
        if spec.get("fuzzy_dedup"):
            rows, matches = _match_rows_to_spark(rows, tenant.id, tz)
            w(f"  fuzzy dedup  : {len(matches)} row(s) already in Spark")
            for m in matches[:25]:
                r = m["row"]
                w(
                    f"   - {r['date']} {r['start_time']} {r.get('name')} → request "
                    f"{m['request_uuid']} ({m['tier']})"
                )
            if len(matches) > 25:
                w(f"   …and {len(matches) - 25} more.")
            report.extend(
                {
                    "source_row": m["row"].get("source_row"),
                    "outcome": "present",
                    "match": m["tier"],
                    "request_id": m["request_id"],
                    "request_uuid": m["request_uuid"],
                    "request_status": m["request_status"],
                }
                for m in matches
            )
            if not rows:
                w("")
                w(self.style.SUCCESS("Nothing to import — every row is already in Spark."))
                _write_json_rows(w, report)
                return

        link_event_ids: dict[int, int] = {}
        if spec.get("link_walkin_events"):
            link_event_ids = _walkin_events_for_rows(rows, tenant.id)
            w(f"  walk-ins     : {len(link_event_ids)} row(s) attach an existing walk-up event")

        # ---- Build the importer's XLSX in memory ---------------------------
        xlsx_bytes = _build_xlsx(
            rows=rows,
            scheduling_status=scheduling_status,
            timezone_code=tz.code,
            request_type_id=request_type.id,
            event_type_id=event_type.id,
        )

        # ---- Run the proven importer (dedup + atomic + retailer link) ------
        mirror_to_sheet = spec.get("mirror_to_sheet", True)
        with nullcontext() if mirror_to_sheet else suppress_sheet_mirror():
            result = import_requests_from_excel_bytes(
                file_bytes=xlsx_bytes,
                tenant_id=tenant.id,
                created_by_id=owner.id,
                default_timezone_id=tz.id,
                default_request_type_id=request_type.id,
                sheet_name="Requests",
                dry_run=not commit,
                rollback_on_error=True,
                link_event_ids=link_event_ids,
            )

        for res in result.rows:
            src = rows[res.row_number - 2] if 0 <= res.row_number - 2 < len(rows) else {}
            entry: dict = {"source_row": src.get("source_row")}
            if res.success:
                entry["outcome"] = "created" if commit else "would_create"
                if res.request_uuid:
                    entry["request_id"] = res.request_id
                    entry["request_uuid"] = res.request_uuid
                linked = res.linked_event_id or link_event_ids.get(res.row_number - 2)
                if linked:
                    entry["linked_event_id"] = linked
            elif res.skipped:
                entry["outcome"] = "present" if "already in Spark" in res.message else "duplicate_row"
                entry["match"] = "exact"
                entry["message"] = res.message
            else:
                entry["outcome"] = "failed"
                entry["message"] = res.message
            report.append(entry)

        w("")
        w(self.style.SUCCESS("Result"))
        w(f"  total rows : {result.total_rows}")
        w(
            f"  {'created' if commit else 'would create'} : "
            f"{result.success_count}"
        )
        w(f"  skipped (dupes/existing) : {result.skipped_count}")
        w(f"  failed     : {result.failed_count}")
        if result.rolled_back:
            w(self.style.WARNING("  ROLLED BACK — a row failed; nothing was written."))

        bad = [r for r in result.rows if not r.success and not r.skipped]
        if bad:
            w(self.style.WARNING(f"  rows with errors ({len(bad)}):"))
            for r in bad[:25]:
                w(self.style.WARNING(f"   - row {r.row_number}: {r.message}"))
            if len(bad) > 25:
                w(self.style.WARNING(f"   …and {len(bad) - 25} more."))

        if not commit:
            w("")
            w(
                self.style.MIGRATE_LABEL(
                    "DRY-RUN complete — no events written. Re-run with "
                    "--commit (execute=true) to create them."
                )
            )
        _write_json_rows(w, report)

    def _resolve_timezone(self, forced_code):
        if forced_code:
            return (
                TimeZone.objects.filter(code__iexact=forced_code.strip())
                .order_by("id")
                .first()
            )
        # Auto: Eastern. These activations are all June/July → daylight time,
        # so prefer the EDT row (offset -240 min) for true-UTC correctness;
        # fall back to EST (-300), then any "Eastern"-named row.
        eastern = list(
            TimeZone.objects.filter(code__iregex=r"^(EDT|EST)$")
            | TimeZone.objects.filter(name__icontains="eastern")
        )
        if not eastern:
            return None

        def norm(tz):
            return _normalize_offset_minutes(tz.offset)

        edt = [t for t in eastern if t.code.upper() == "EDT" or norm(t) == -240]
        if edt:
            return sorted(edt, key=lambda t: t.id)[0]
        est = [t for t in eastern if t.code.upper() == "EST" or norm(t) == -300]
        if est:
            return sorted(est, key=lambda t: t.id)[0]
        return sorted(eastern, key=lambda t: t.id)[0]


def _same_place(row: dict, name: str, address: str) -> bool:
    """Same street address, or — only when either side has no street number —
    the same store name. Generic banners ("Total Wine & More") repeat across
    cities, so names only decide when an address can't."""
    row_addr = row.get("address") or ""
    if addresses_fuzzy_match(row_addr, address or ""):
        return True
    name_key = normalize_place(row.get("name") or "")
    has_street = bool(address_core_parts(row_addr)[0])
    return bool(
        name_key
        and not (has_street and address_core_parts(address or "")[0])
        and normalize_place(name or "") == name_key
    )


def _row_window(row: dict, tz: TimeZone) -> tuple[datetime.datetime, datetime.datetime, datetime.datetime]:
    """(start, local-day start, local-day end) as UTC instants."""
    day = _parse_date(row["date"])
    start = _local_datetime_to_utc(
        datetime.datetime.combine(day, _parse_time(row["start_time"])), tz.offset
    )
    day_lo = _local_datetime_to_utc(datetime.datetime.combine(day, datetime.time(0, 0)), tz.offset)
    return start, day_lo, day_lo + datetime.timedelta(days=1)


def _match_rows_to_spark(
    rows: list, tenant_id: int, default_tz: TimeZone
) -> tuple[list, list[dict]]:
    """Split rows into (to_import, matches).

    A row is already in Spark when a live request for the tenant is at the
    same place (see _same_place) and either starts within an hour of it, or —
    second pass, for rows the sheet re-timed after Spark booked them — on the
    same local day. Each request satisfies at most one row, so two shifts at
    one store on one day never collapse onto a single request.
    """
    tz_by_code: dict[str, TimeZone | None] = {}

    def tz_for(r: dict) -> TimeZone | None:
        code = (r.get("timezone_code") or default_tz.code).strip().upper()
        if code not in tz_by_code:
            tz_by_code[code] = TimeZone.objects.filter(code__iexact=code).order_by("id").first()
        return tz_by_code[code]

    windows: dict[int, tuple] = {}
    for i, r in enumerate(rows):
        tz = tz_for(r)
        if tz is not None:
            windows[i] = _row_window(r, tz)
    if not windows:
        return list(rows), []

    lo = min(w[1] for w in windows.values())
    hi = max(w[2] for w in windows.values())
    candidates = list(
        Request.objects.filter(
            tenant_id=tenant_id,
            deleted_at__isnull=True,
            start_time__gte=lo - _FUZZY_DEDUP_WINDOW,
            start_time__lt=hi + _FUZZY_DEDUP_WINDOW,
        )
        .select_related("status")
        .order_by("start_time", "id")
    )

    claimed: set[int] = set()
    matched: dict[int, dict] = {}
    passes = (
        ("time", lambda req, w: abs(req.start_time - w[0]) <= _FUZZY_DEDUP_WINDOW),
        ("day", lambda req, w: w[1] <= req.start_time < w[2]),
    )
    for tier, fits in passes:
        for i, w in windows.items():
            if i in matched:
                continue
            row = rows[i]
            hit = next(
                (
                    req
                    for req in candidates
                    if req.id not in claimed
                    and fits(req, w)
                    and _same_place(row, req.name, req.address)
                ),
                None,
            )
            if hit is None:
                continue
            claimed.add(hit.id)
            matched[i] = {
                "row": row,
                "tier": tier,
                "request_id": hit.id,
                "request_uuid": str(hit.uuid),
                "request_status": getattr(hit.status, "slug", None) or "",
            }

    kept = [r for i, r in enumerate(rows) if i not in matched]
    return kept, [matched[i] for i in sorted(matched)]


def _drop_rows_already_in_spark(
    rows: list, tenant_id: int, default_tz: TimeZone
) -> tuple[list, list[tuple[str, str]]]:
    kept, matches = _match_rows_to_spark(rows, tenant_id, default_tz)
    return kept, [
        (f"{m['row']['date']} {m['row']['start_time']} {m['row'].get('name')}", m["request_uuid"])
        for m in matches
    ]


def _walkin_events_for_rows(rows: list, tenant_id: int) -> dict[int, int]:
    """{row index: event id} for rows a BA already worked off a standing
    walk-up link before the shift was on the tracker.

    Those walk-ins opened request-less Events (same place, same calendar
    day). Creating a fresh Request+Event for the row would leave the recap on
    the orphan and a second, unworked-looking event on the tracker — so the
    importer attaches the orphan to the new Request instead. Each event is
    claimed by one row at most.
    """
    days = {_parse_date(r["date"]) for r in rows}
    if not days:
        return {}
    lo = datetime.datetime.combine(min(days), datetime.time(0, 0), tzinfo=datetime.timezone.utc)
    hi = datetime.datetime.combine(max(days), datetime.time(23, 59), tzinfo=datetime.timezone.utc)
    orphans = list(
        Event.objects.filter(
            tenant_id=tenant_id, request__isnull=True, date__gte=lo, date__lte=hi
        ).order_by("id")
    )
    claimed: set[int] = set()
    out: dict[int, int] = {}
    for i, r in enumerate(rows):
        day = _parse_date(r["date"])
        hit = next(
            (
                ev
                for ev in orphans
                if ev.id not in claimed
                and event_calendar_date(ev) == day
                and _same_place(r, ev.name, ev.address)
            ),
            None,
        )
        if hit is not None:
            claimed.add(hit.id)
            out[i] = hit.id
    return out


def _write_json_rows(w, report: list[dict]) -> None:
    # One short line per row: CI log viewers drop a single huge line.
    for entry in report:
        w("JSON_ROW:" + json.dumps(entry))


def _slugify(name: str) -> str:
    from django.utils.text import slugify

    return slugify(name)


def _parse_date(s: str) -> datetime.date:
    return datetime.datetime.strptime(s.strip(), "%m/%d/%Y").date()


def _parse_time(s: str) -> datetime.time:
    return datetime.datetime.strptime(s.strip(), "%H:%M").time()


def _build_xlsx(
    *,
    rows: list,
    scheduling_status: str,
    timezone_code: str,
    request_type_id: int,
    event_type_id: int,
) -> bytes:
    """One row per activation, in the importer's TEMPLATE_COLUMNS shape."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Requests"
    ws.append(TEMPLATE_COLUMNS)
    for r in rows:
        # NOTE: city/state are deliberately OMITTED. The importer hard-fails a
        # row when `city` has no matching global Location (or `state` no State)
        # — and small-town MI cities aren't all seeded. The full address text
        # ("…, Grand Blanc, MI 48439") carries the location for display, and
        # retailer_name links every row to one "Kroger" account, so we lose
        # nothing important while guaranteeing the geography can't fail a row.
        # `state_code` (distinct from the display-only `state`) opts a
        # schedule into State linking — Torch routes recap mail by state.
        cell = {
            "name": r.get("name"),
            "date": r.get("date"),
            "start_time": r.get("start_time"),
            "end_time": r.get("end_time"),
            "address": r.get("address"),
            "store_number": r.get("store_number"),
            "scheduling_status": scheduling_status,
            "notes": r.get("notes"),
            "retailer_name": r.get("retailer_name"),
            "store_manager_phone": r.get("store_manager_phone"),
            "state": r.get("state_code"),
            # A row may carry its own timezone (multi-market schedules span
            # ET/CT); the command-level code is the fallback for the rest.
            "timezone_code": r.get("timezone_code") or timezone_code,
            "request_type_id": request_type_id,
            "event_type_id": event_type_id,
        }
        ws.append([cell.get(c, "") if cell.get(c) is not None else "" for c in TEMPLATE_COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
