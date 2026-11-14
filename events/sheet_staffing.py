"""Read who the staffing sheets say is working, and show it beside Spark.

Ignite's team staffs from Google Sheets: each row is a shift, with the
confirmed ambassador's name typed in. Spark's own bookings can lag behind
that. This module reads those rows and keeps a mirror (``SheetShift``) so the
dashboard can say "the sheet has Kayla Buis on this" and point out where the
sheet and Spark disagree.

It is strictly read-only on both sides: nothing is written to a sheet, and
nothing in Spark is booked, changed or created from what a sheet says. The
mirror is a cache of the sheet — rows that leave the sheet leave the mirror.

Sources are the schedule sheets Spark already knows (``SHEET_CONFIGS``) plus
any tab of the EVENT CALENDAR workbook whose name is exactly one brand's
name. Columns are found by header name, never by position.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

# The Ignite EVENT CALENDAR workbook: one tab per client.
EVENT_CALENDAR_SHEET_ID = "1eNIuGiOqkLIwgF7al5LkGvwYtmNHrKoct4qu1UnYHMc"
READ_RANGE = "A1:CZ"
# Yesterday (open punches are still being chased) through two weeks out.
DAYS_BACK = 1
DAYS_AHEAD = 14

MATCH_MATCHED = "matched"
MATCH_NO_EVENT = "no_event"
MATCH_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Source:
    """One tab to read."""

    key: str
    label: str
    sheet_id: str
    tab_title: str
    tab_gid: int
    # Fixed brand for every row; None when a Brand column routes each row.
    tenant_id: int | None


@dataclass
class ParsedRow:
    row_number: int
    day: date
    start_text: str
    end_text: str
    store_name: str
    address: str
    ba_name: str
    ba_email: str
    ba_phone: str
    rate: str
    tenant_ids: list[int]


def _key(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def name_key(name: str | None) -> str:
    """Compare people's names without case, spacing or punctuation."""
    return _key(name)


def _parse_day(raw: str, today: date) -> date | None:
    """A Date cell as a date. Yearless cells ("Oct 21", "11/13") take the
    year that puts them nearest to today."""
    from events.sheet_event_confirmations import parse_sheet_date_iso

    text = (raw or "").strip()
    if not text:
        return None
    # "Sept 26" is how people write it; the parser knows "Sep".
    text = re.sub(r"^sept\b\.?", "Sep", text, flags=re.IGNORECASE)
    try:
        return parse_sheet_date_iso(text)
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", text)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    best: date | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - today).days) < abs((best - today).days):
            best = candidate
    return best


def parse_rows(
    values: list[list[Any]],
    *,
    sheet_id: str,
    today: date,
    fixed_tenant_id: int | None,
    brand_tenant_ids,
) -> list[ParsedRow]:
    """Turn a tab's raw values into shifts inside the reading window.

    ``brand_tenant_ids(brand_cell) -> list[int]`` resolves a Brand column
    when the tab has no fixed brand. Rows with no ambassador name, no
    readable date, or a date outside the window are skipped: an unstaffed
    row says nothing about who is working.
    """
    from events.sheet_event_confirmations import _cell, payload_from_mapping

    if not values:
        return []
    header = [str(h or "").strip() for h in values[0]]
    earliest = today - timedelta(days=DAYS_BACK)
    latest = today + timedelta(days=DAYS_AHEAD)
    out: list[ParsedRow] = []
    # An event with several ambassadors is written once, then one line per
    # extra person with only their own columns filled. Those lines belong to
    # the event above them.
    carried: dict[str, str] | None = None
    for offset, raw in enumerate(values[1:], start=2):
        cells = list(raw) + [""] * (len(header) - len(raw))
        mapping = {h: cells[i] for i, h in enumerate(header) if h}
        payload = payload_from_mapping(mapping, row_number=offset, sheet_id=sheet_id)
        own_store = (payload.store_name or _cell(mapping, "EVENT", "Account")).strip()
        own_address = (payload.address or _cell(mapping, "Account Address")).strip()
        own_start = (payload.start_time or _cell(mapping, "Time Scheduled")).strip()
        if payload.date.strip() or own_store or own_address:
            carried = {
                "date": payload.date,
                "start": own_start,
                "end": payload.end_time,
                "store": own_store,
                "address": own_address,
                "brand": payload.brand,
            }
        elif carried is not None and payload.ba_name.strip():
            payload.date = carried["date"]
            own_start = own_start or carried["start"]
            payload.end_time = payload.end_time or carried["end"]
            payload.brand = payload.brand or carried["brand"]
            own_store, own_address = carried["store"], carried["address"]
        ba_name = payload.ba_name.strip()
        if not ba_name:
            continue
        day = _parse_day(payload.date, today)
        if day is None or not (earliest <= day <= latest):
            continue
        if fixed_tenant_id is not None:
            tenant_ids = [fixed_tenant_id]
        else:
            try:
                tenant_ids = list(brand_tenant_ids(payload.brand))
            except ValueError:
                tenant_ids = []
        if not tenant_ids:
            continue
        out.append(
            ParsedRow(
                row_number=offset,
                day=day,
                start_text=own_start,
                end_text=payload.end_time.strip(),
                store_name=own_store,
                address=own_address,
                ba_name=ba_name,
                ba_email=payload.ba_email.strip(),
                ba_phone=_cell(mapping, "Cell Phone #", "Phone"),
                rate=_cell(mapping, "BA Rate", "Rate"),
                tenant_ids=tenant_ids,
            )
        )
    return out


def _street_number(address: str | None) -> str:
    """The first house number in an address, wherever it sits: "Hard Rock
    Stadium / 347 Don Shula Dr" is 347. A number is a house number when a
    street word follows it, which rules out suite numbers and ZIP codes."""
    match = re.search(r"(?<![\w#])(\d{2,6})\s+(?:[NSEW]\.?\s+)?[A-Za-z]{2,}", address or "")
    return match.group(1) if match else ""


def match_event(row: ParsedRow, events: list) -> tuple[Any | None, str]:
    """The Spark event this sheet row is about, among same-brand same-day
    events. A match needs the place to agree — the same street number, or
    one store name contained in the other — not just the day."""
    store = _key(row.store_name)
    number = _street_number(row.address)
    # "Total Wine, 7400 Carson Blvd" / "Hard Rock Stadium / 347 Don Shula Dr":
    # the place name people put in front of the street address.
    lead = re.split(r"[,/]", row.address or "", maxsplit=1)[0].strip()
    venue = _key(lead) if lead and not lead[0].isdigit() else ""
    hits = []
    for event in events:
        haystack = _key(f"{getattr(event, 'name', '')} {getattr(event, 'address', '')}")
        event_number = _street_number(getattr(event, "address", ""))
        if number and event_number and number == event_number:
            hits.append(event)
        elif store and len(store) >= 4 and store in haystack:
            hits.append(event)
        elif venue and len(venue) >= 5 and venue in haystack:
            hits.append(event)
    if len(hits) == 1:
        return hits[0], MATCH_MATCHED
    if len(hits) > 1:
        return None, MATCH_AMBIGUOUS
    return None, MATCH_NO_EVENT


def _sources(svc) -> list[Source]:
    from events.sheet_event_confirmations import (
        SHEET_AUTOMATION_LABELS,
        SHEET_CONFIGS,
        _resolve_tab_title,
        _tenant_for,
    )
    from tenants.models import Tenant

    sources: list[Source] = []
    calendar_tabs_taken: set[int] = set()
    for config in SHEET_CONFIGS:
        try:
            title = _resolve_tab_title(config, svc)
            tenant_id = None if config.routes_by_brand else _tenant_for(config).id
        except Exception:  # noqa: BLE001 — one unreadable sheet must not stop the rest
            logger.warning("sheet staffing: skipping %s", config.key, exc_info=True)
            continue
        if config.sheet_id == EVENT_CALENDAR_SHEET_ID:
            calendar_tabs_taken.add(config.tab_gid)
        sources.append(
            Source(
                key=config.key,
                label=SHEET_AUTOMATION_LABELS.get(config.key, config.key),
                sheet_id=config.sheet_id,
                tab_title=title,
                tab_gid=config.tab_gid,
                tenant_id=tenant_id,
            )
        )

    # EVENT CALENDAR: a tab is read only when its name is exactly one brand.
    by_name: dict[str, list[int]] = {}
    for tenant_id, name in Tenant.objects.values_list("id", "name"):
        by_name.setdefault(_key(name), []).append(tenant_id)
    try:
        meta = (
            svc.spreadsheets()
            .get(
                spreadsheetId=EVENT_CALENDAR_SHEET_ID,
                fields="sheets.properties(sheetId,title)",
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.warning("sheet staffing: EVENT CALENDAR unreadable", exc_info=True)
        meta = {}
    for sheet in meta.get("sheets") or []:
        props = sheet.get("properties") or {}
        gid, title = props.get("sheetId"), props.get("title") or ""
        if gid in calendar_tabs_taken:
            continue
        tenant_ids = by_name.get(_key(title)) or []
        if len(tenant_ids) != 1:
            continue
        sources.append(
            Source(
                key=f"calendar:{gid}",
                label=f"EVENT CALENDAR · {title.strip()}",
                sheet_id=EVENT_CALENDAR_SHEET_ID,
                tab_title=title,
                tab_gid=int(gid),
                tenant_id=tenant_ids[0],
            )
        )
    return sources


def _brand_tenant_ids(brand_cell: str) -> list[int]:
    from events.brand_group_sheet import parse_brand_slugs, tenants_for_slugs

    return [t.id for t in tenants_for_slugs(parse_brand_slugs(brand_cell))]


def _read_values(svc, source: Source) -> list[list[Any]]:
    from utils.torch_public_form_sheet import _qualify

    response = (
        svc.spreadsheets()
        .values()
        .get(
            spreadsheetId=source.sheet_id,
            range=_qualify(source.tab_title, READ_RANGE),
        )
        .execute()
    )
    return list(response.get("values") or [])


def sync_sheet_staffing(*, dry_run: bool = False, svc=None, today: date | None = None) -> dict:
    """Refresh the mirror from every source. Returns counts and one line per
    source; ``dry_run`` reads and matches but stores nothing."""
    from events.live_board import _ops_today
    from events.models import Event, SheetShift
    from utils.torch_public_form_sheet import _service

    svc = svc or _service()
    today = today or _ops_today()
    earliest = today - timedelta(days=DAYS_BACK)
    latest = today + timedelta(days=DAYS_AHEAD)
    totals = {"sources": 0, "rows": 0, "matched": 0, "no_event": 0, "ambiguous": 0, "failed": 0}
    lines: list[str] = []

    for source in _sources(svc):
        try:
            values = _read_values(svc, source)
            rows = parse_rows(
                values,
                sheet_id=source.sheet_id,
                today=today,
                fixed_tenant_id=source.tenant_id,
                brand_tenant_ids=_brand_tenant_ids,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sheet staffing: %s failed", source.key, exc_info=True)
            totals["failed"] += 1
            lines.append(f"{source.label}: could not be read ({type(exc).__name__})")
            continue
        totals["sources"] += 1

        tenant_ids = {tid for row in rows for tid in row.tenant_ids}
        events_by_day: dict[tuple[int, date], list] = {}
        if rows:
            for event in Event.objects.filter(
                tenant_id__in=tenant_ids,
                date__date__gte=earliest,
                date__date__lte=latest,
            ).only("id", "name", "address", "date", "tenant_id"):
                events_by_day.setdefault(
                    (event.tenant_id, event.date.date()), []
                ).append(event)

        seen: set[tuple[int, int]] = set()
        counts = {MATCH_MATCHED: 0, MATCH_NO_EVENT: 0, MATCH_AMBIGUOUS: 0}
        for row in rows:
            for tenant_id in row.tenant_ids:
                event, match = match_event(
                    row, events_by_day.get((tenant_id, row.day), [])
                )
                counts[match] += 1
                seen.add((row.row_number, tenant_id))
                if dry_run:
                    continue
                SheetShift.objects.update_or_create(
                    sheet_id=source.sheet_id,
                    tab_gid=source.tab_gid,
                    row_number=row.row_number,
                    tenant_id=tenant_id,
                    defaults={
                        "source_label": source.label,
                        "day": row.day,
                        "start_text": row.start_text[:40],
                        "end_text": row.end_text[:40],
                        "store_name": row.store_name[:255],
                        "address": row.address[:500],
                        "ba_name": row.ba_name[:255],
                        "ba_email": row.ba_email[:254],
                        "ba_phone": row.ba_phone[:40],
                        "rate": row.rate[:40],
                        "event": event,
                        "match": match,
                    },
                )
        if not dry_run:
            # The mirror follows the sheet: a row cleared or moved out of the
            # window is no longer something the sheet says.
            stale = SheetShift.objects.filter(
                sheet_id=source.sheet_id, tab_gid=source.tab_gid
            )
            for shift in stale.only("id", "row_number", "tenant_id"):
                if (shift.row_number, shift.tenant_id) not in seen:
                    shift.delete()

        totals["rows"] += sum(counts.values())
        totals["matched"] += counts[MATCH_MATCHED]
        totals["no_event"] += counts[MATCH_NO_EVENT]
        totals["ambiguous"] += counts[MATCH_AMBIGUOUS]
        lines.append(
            f"{source.label}: {sum(counts.values())} staffed rows in window — "
            f"{counts[MATCH_MATCHED]} matched to a Spark event, "
            f"{counts[MATCH_NO_EVENT]} with no Spark event, "
            f"{counts[MATCH_AMBIGUOUS]} matching more than one"
        )

    return {**totals, "lines": lines, "dry_run": dry_run, "synced_at": timezone.now().isoformat()}
