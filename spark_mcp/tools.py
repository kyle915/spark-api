"""The read-only tools the Spark MCP server exposes.

Every tool runs for exactly one brand: the tenant pinned on the OAuth
connection. Numbers come from the same services the brand dashboard's
resolvers call once they have resolved the tenant, so the visibility rules
are identical:

* program KPIs → ``recaps.tenant_overview`` (``tenantKpis`` / Insights);
* consumer splits → ``recaps.consumer_breakdown`` (``tenantConsumerBreakdown``);
* products → ``recaps.comment_insights.product_lines`` (``tenantProductRegions``);
* weekly summary → ``recaps.weekly_summary.latest_summary`` (reviewed only,
  as ``tenantWeeklySummary`` gives a brand user).

Recaps are client-visible only: approved, not archived, not on a deleted
request, not kept off the dashboard, not an agency filing kept out of
aggregates (``recaps.feedback_themes._client_visible``).

Brand ambassador privacy: no tool selects an ambassador's name, contact,
address, payment, document or location columns, and there is no ambassador
tool. Events report a count of booked brand ambassadors; recaps report none.
``spark_mcp.privacy`` scrubs every returned string as a backstop.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Count, Q

from . import conf

MAX_RANGE_DAYS = 366
DEFAULT_RANGE_DAYS = 30
MAX_PAGE = 50
DEFAULT_PAGE = 25
MAX_UPCOMING_DAYS = 30
MAX_UPCOMING_ROWS = 100
MAX_PENDING_ROWS = 50
MAX_PRODUCT_ROWS = 50


class ToolInputError(ValueError):
    """The arguments were invalid. The message is shown to the AI app."""


@dataclass
class ToolContext:
    tenant_id: int
    tenant_name: str
    user_id: int
    connection: object


# ─── Argument helpers ──────────────────────────────────────────────


def _ops_today() -> date:
    from events.live_board import _ops_today as today

    return today()


def _parse_day(raw, field: str) -> date | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ToolInputError(f"{field} must be a date written YYYY-MM-DD.")
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError as exc:
        raise ToolInputError(f"{field} must be a date written YYYY-MM-DD.") from exc


def date_range(args: dict, *, default_days: int = DEFAULT_RANGE_DAYS) -> tuple[date, date]:
    """Inclusive (start, end). Defaults to the last ``default_days`` days
    ending today (Pacific). Ranges longer than a year are refused."""
    today = _ops_today()
    end = _parse_day(args.get("end_date"), "end_date") or today
    start = _parse_day(args.get("start_date"), "start_date") or (
        end - timedelta(days=default_days - 1)
    )
    if start > end:
        raise ToolInputError("start_date must be on or before end_date.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ToolInputError(
            f"The date range is limited to {MAX_RANGE_DAYS} days. Ask for a shorter range."
        )
    return start, end


def _int_arg(args: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = args.get(name, default)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ToolInputError(f"{name} must be a whole number.")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolInputError(f"{name} must be a whole number.") from exc
    if value < lo or value > hi:
        raise ToolInputError(f"{name} must be between {lo} and {hi}.")
    return value


def _encode_cursor(offset: int, start: date, end: date) -> str:
    raw = json.dumps({"o": offset, "s": start.isoformat(), "e": end.isoformat()})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, start: date, end: date) -> int:
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
        offset = int(data["o"])
    except Exception as exc:  # noqa: BLE001
        raise ToolInputError("cursor is not valid. Start again without one.") from exc
    if data.get("s") != start.isoformat() or data.get("e") != end.isoformat():
        raise ToolInputError("cursor belongs to a different date range. Start again without one.")
    return max(0, offset)


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _recap_url(kind: str, uuid) -> str:
    base = conf.frontend_url()
    path = f"/recap/view-custom/{uuid}" if kind == "custom" else f"/recap/view/{uuid}"
    return f"{base}{path}"


# ─── Tools ─────────────────────────────────────────────────────────


def _kpi_block(tenant_id: int, start: date, end: date) -> dict:
    from recaps.tenant_overview import (
        _inclusive_dates_to_window,
        _tenant_event_recap_counts_window,
        _tenant_kpi_totals_window,
    )

    window = _inclusive_dates_to_window(start, end)
    totals = _tenant_kpi_totals_window(tenant_id, window)
    events, recaps = _tenant_event_recap_counts_window(tenant_id, window)
    sampled = totals.consumers_reached
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "events_with_recaps": events,
        "approved_recaps": recaps,
        "consumers_sampled": sampled,
        "samples_distributed": totals.samples_distributed,
        "products_sold": totals.products_sold,
        "single_cans_sold": totals.cans_sold,
        "packs_sold": totals.packs_sold,
        "total_engagements": totals.total_engagements,
        "first_time_consumers": totals.first_time_consumers,
        "brand_aware_consumers": totals.brand_aware_consumers,
        "willing_to_purchase": totals.willing_to_purchase,
        "brand_aware_pct_of_sampled": _pct(totals.brand_aware_consumers, sampled),
        "willing_to_purchase_pct_of_sampled": _pct(totals.willing_to_purchase, sampled),
        "first_time_pct_of_sampled": _pct(totals.first_time_consumers, sampled),
    }


def get_program_kpis(ctx: ToolContext, args: dict) -> tuple[dict, int]:
    start, end = date_range(args)
    current = _kpi_block(ctx.tenant_id, start, end)
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    previous = _kpi_block(ctx.tenant_id, prev_end - timedelta(days=span - 1), prev_end)
    return (
        {
            "brand": ctx.tenant_name,
            "basis": (
                "Approved recaps only, dated by the event date. Same source as the "
                "Program KPIs on the Spark dashboard."
            ),
            "current_period": current,
            "previous_period": previous,
        },
        1,
    )


def _store_label(aliases, *names):
    from recaps.consumer_breakdown import _store

    return _store(*names, aliases=aliases) or None


def list_recaps(ctx: ToolContext, args: dict) -> tuple[dict, int]:
    from events.retailer_aliases import accepted_alias_map
    from events.routing import extract_state_code
    from recaps.consumer_breakdown import _numbered, consumer_rows
    from recaps.feedback_themes import _client_visible
    from recaps.models import CustomRecap, Recap
    from recaps.tenant_overview import _event_date_expr, _inclusive_dates_to_window

    start, end = date_range(args)
    limit = _int_arg(args, "limit", DEFAULT_PAGE, 1, MAX_PAGE)
    cursor = args.get("cursor")
    offset = _decode_cursor(cursor, start, end) if cursor else 0
    tid = ctx.tenant_id
    window = _inclusive_dates_to_window(start, end)

    # (day, kind, id) for every client-visible recap in the window; the
    # window is capped at a year so this stays small.
    keys: list[tuple] = []
    custom_qs = (
        _client_visible(CustomRecap.objects.filter(tenant_id=tid), "event__", "", window)
        .annotate(_day=_event_date_expr("event__"))
        .filter(_day__isnull=False)
    )
    legacy_qs = (
        _client_visible(Recap.objects.filter(event__tenant_id=tid), "event__", "", window)
        .annotate(_day=_event_date_expr("event__"))
        .filter(_day__isnull=False)
    )
    keys += [(d, "custom", i) for i, d in custom_qs.values_list("id", "_day")]
    keys += [(d, "legacy", i) for i, d in legacy_qs.values_list("id", "_day")]
    keys.sort(key=lambda k: (k[0], k[2]), reverse=True)
    total = len(keys)
    page = keys[offset : offset + limit]

    custom_ids = [k[2] for k in page if k[1] == "custom"]
    legacy_ids = [k[2] for k in page if k[1] == "legacy"]
    aliases = accepted_alias_map(tid)
    consumers = {
        (r["kind"], r["id"]): r["consumers"]
        for r in consumer_rows(tid, start, end, include_archived=False)
        if (r["kind"] == "custom" and r["id"] in custom_ids)
        or (r["kind"] == "legacy" and r["id"] in legacy_ids)
    }

    rows: dict[tuple, dict] = {}
    for r in custom_qs.filter(id__in=custom_ids).values(
        "id", "uuid", "_day", "total_engagements", "retailer__name", "store_number",
        "event__retailer__name", "event__request__retailer__name",
        "event__request__retailer_name", "event__state__code", "state__code",
        "event__address", "event__event_type__name",
    ):
        rows[("custom", r["id"])] = {
            "recap_id": str(r["uuid"]),
            "event_date": r["_day"].date().isoformat(),
            "store": _store_label(
                aliases,
                _numbered(r["retailer__name"], r["store_number"]),
                r["event__retailer__name"],
                r["event__request__retailer__name"],
                r["event__request__retailer_name"],
            ),
            "state": (
                r["event__state__code"] or r["state__code"]
                or extract_state_code(r["event__address"]) or None
            ),
            "activation_type": r["event__event_type__name"] or None,
            "consumers_sampled": consumers.get(("custom", r["id"])),
            "total_engagements": r["total_engagements"],
            "staffed_by": "Brand ambassador",
            "url": _recap_url("custom", r["uuid"]),
        }
    for r in legacy_qs.filter(id__in=legacy_ids).values(
        "id", "uuid", "_day", "total_engagements", "products_sold",
        "event__retailer__name", "event__request__retailer__name",
        "event__request__retailer_name", "event__state__code", "event__address",
        "event__event_type__name",
    ):
        rows[("legacy", r["id"])] = {
            "recap_id": str(r["uuid"]),
            "event_date": r["_day"].date().isoformat(),
            "store": _store_label(
                aliases,
                r["event__retailer__name"],
                r["event__request__retailer__name"],
                r["event__request__retailer_name"],
            ),
            "state": r["event__state__code"] or extract_state_code(r["event__address"]) or None,
            "activation_type": r["event__event_type__name"] or None,
            "consumers_sampled": consumers.get(("legacy", r["id"])),
            "total_engagements": r["total_engagements"],
            "products_sold": r["products_sold"],
            "staffed_by": "Brand ambassador",
            "url": _recap_url("legacy", r["uuid"]),
        }
    items = [rows[(k[1], k[2])] for k in page if (k[1], k[2]) in rows]
    next_offset = offset + len(page)
    return (
        {
            "brand": ctx.tenant_name,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "total_recaps": total,
            "returned": len(items),
            "next_cursor": _encode_cursor(next_offset, start, end) if next_offset < total else None,
            "recaps": items,
        },
        len(items),
    )


BREAKDOWN_DIMENSIONS = ("store", "state", "week", "product")


def get_consumer_breakdown(ctx: ToolContext, args: dict) -> tuple[dict, int]:
    by = (args.get("by") or "store").strip().lower() if isinstance(args.get("by"), str) else "store"
    if by not in BREAKDOWN_DIMENSIONS:
        raise ToolInputError("by must be one of: store, state, week, product.")
    start, end = date_range(args)
    base = {
        "brand": ctx.tenant_name,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "by": by,
    }
    if by == "product":
        from recaps.comment_insights import product_lines

        totals: dict[str, dict] = {}
        for line in product_lines(ctx.tenant_id, start, end):
            row = totals.setdefault(
                line["product"],
                {"product": line["product"], "units_sold": 0, "units_sampled": 0, "_recaps": set()},
            )
            row["units_sold" if line["measure"] == "sold" else "units_sampled"] += line["units"]
            row["_recaps"].add(line["recap"])
        rows = sorted(
            totals.values(), key=lambda r: (r["units_sold"], r["units_sampled"]), reverse=True
        )
        out = [
            {
                "product": r["product"],
                "units_sold": r["units_sold"],
                "units_sampled": r["units_sampled"],
                "recaps": len(r["_recaps"]),
            }
            for r in rows[:MAX_PRODUCT_ROWS]
        ]
        base.update(
            {
                "basis": (
                    "Per-product units from approved recaps: sold from per-product sold "
                    "lines, sampled from product sample rows. Templates that record only "
                    "one sold total have no per-product sold figure."
                ),
                "product_count": len(rows),
                "rows": out,
            }
        )
        return base, len(out)

    from recaps.consumer_breakdown import tenant_consumer_breakdown

    data = tenant_consumer_breakdown(ctx.tenant_id, start, end)
    key = {"store": "by_store", "state": "by_state", "week": "by_week"}[by]
    rows = data[key]
    base.update(
        {
            "basis": (
                "Consumers sampled on approved recaps, dated by the event date. Each "
                "split adds up to total_consumers_sampled. kind 'missing' = recaps whose "
                "event has no store/state recorded; kind 'other' = the smaller stores folded together."
            ),
            "total_consumers_sampled": data["total"],
            "recaps": data["recaps"],
            "store_count": data["store_count"],
            "state_count": data["state_count"],
            "rows": rows,
        }
    )
    return base, len(rows)


def _hhmm(value, zone) -> str | None:
    if value is None:
        return None
    from datetime import timezone as dt_tz

    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_tz.utc)
    return value.astimezone(zone).strftime("%H:%M")


def _is_cancelled(status) -> bool:
    text = f"{getattr(status, 'slug', '') or ''} {getattr(status, 'name', '') or ''}".lower()
    return "cancel" in text


def list_upcoming_events(ctx: ToolContext, args: dict) -> tuple[dict, int]:
    from ambassadors.models import AmbassadorEvent
    from events.models import Event, Request
    from events.routing import extract_state_code
    from events.venue_day import venue_local_date, venue_zone

    days = _int_arg(args, "days", 7, 1, MAX_UPCOMING_DAYS)
    today = _ops_today()
    last = today + timedelta(days=days)
    lo, hi = today - timedelta(days=1), last + timedelta(days=1)
    in_window = Q(start_time__date__range=(lo, hi)) | Q(
        start_time__isnull=True, date__date__range=(lo, hi)
    )

    events = (
        Event.objects.filter(tenant_id=ctx.tenant_id)
        .filter(in_window)
        .filter(Q(request__isnull=True) | Q(request__deleted_at__isnull=True))
        .select_related("timezone", "status", "retailer", "state", "event_type", "request__retailer")
        .order_by("start_time", "date", "id")
    )
    rows: list[dict] = []
    event_ids: list[int] = []
    for ev in events[:500]:
        day = venue_local_date(ev)
        if day is None or day < today or day > last or _is_cancelled(ev.status):
            continue
        zone = venue_zone(ev)
        event_ids.append(ev.id)
        rows.append(
            {
                "_id": ev.id,
                "date": day.isoformat(),
                "start_time": _hhmm(ev.start_time, zone),
                "end_time": _hhmm(ev.end_time, zone),
                "store": (ev.retailer.name if ev.retailer_id else None)
                or (ev.request.retailer.name if ev.request_id and ev.request.retailer_id else None)
                or None,
                "address": ev.address or None,
                "state": (ev.state.code if ev.state_id else None) or extract_state_code(ev.address) or None,
                "activation_type": ev.event_type.name if ev.event_type_id else None,
                "status": ev.status.name if ev.status_id else "Scheduled",
                "source": "event",
            }
        )
    booked = dict(
        AmbassadorEvent.objects.filter(event_id__in=event_ids, is_approved=True)
        .values("event_id")
        .annotate(n=Count("id"))
        .values_list("event_id", "n")
    )
    for row in rows:
        row["brand_ambassadors_booked"] = booked.get(row.pop("_id"), 0)

    # Requests not yet turned into an event (as on the /upcoming page).
    requests = (
        Request.objects.filter(tenant_id=ctx.tenant_id, deleted_at__isnull=True, event__isnull=True)
        .filter(in_window)
        .select_related("timezone", "status", "retailer", "state", "request_type")
        .order_by("start_time", "date", "id")
    )
    for req in requests[:500]:
        day = venue_local_date(req)
        slug = f"{getattr(req.status, 'slug', '') or ''} {getattr(req.status, 'name', '') or ''}".lower()
        if day is None or day < today or day > last:
            continue
        if any(word in slug for word in ("done", "completed", "declined", "cancel")):
            continue
        zone = venue_zone(req)
        rows.append(
            {
                "date": day.isoformat(),
                "start_time": _hhmm(req.start_time, zone),
                "end_time": _hhmm(req.end_time, zone),
                "store": (req.retailer.name if req.retailer_id else None) or req.retailer_name or None,
                "address": req.address or None,
                "state": (req.state.code if req.state_id else None) or extract_state_code(req.address) or None,
                "activation_type": req.request_type.name if req.request_type_id else None,
                "status": req.status.name if req.status_id else "Pending",
                "source": "request",
                "brand_ambassadors_booked": 0,
            }
        )
    rows.sort(key=lambda r: (r["date"], r["start_time"] or ""))
    total = len(rows)
    rows = rows[:MAX_UPCOMING_ROWS]
    return (
        {
            "brand": ctx.tenant_name,
            "from_date": today.isoformat(),
            "to_date": last.isoformat(),
            "total": total,
            "truncated": total > len(rows),
            "note": "Times are local to each venue. Brand ambassadors are given as a count only.",
            "events": rows,
        },
        len(rows),
    )


def list_pending_approvals(ctx: ToolContext, args: dict) -> tuple[dict, int]:
    from events.models import Request
    from events.routing import extract_state_code
    from recaps.models import CustomRecap, Recap
    from recaps.tenant_overview import _event_date_expr, _filter_event_window

    base = conf.frontend_url()
    reqs = (
        Request.objects.filter(tenant_id=ctx.tenant_id, deleted_at__isnull=True)
        .filter(Q(status__slug__iexact="pending") | Q(status__name__iexact="pending"))
        .select_related("retailer", "state", "request_type")
        .order_by("-created_at")
    )
    request_total = reqs.count()
    request_rows = [
        {
            "submitted_on": r.created_at.date().isoformat(),
            "event_date": (r.start_time or r.date).date().isoformat() if (r.start_time or r.date) else None,
            "store": (r.retailer.name if r.retailer_id else None) or r.retailer_name or None,
            "state": (r.state.code if r.state_id else None) or extract_state_code(r.address) or None,
            "activation_type": r.request_type.name if r.request_type_id else None,
            "url": f"{base}/request/view/{r.uuid}",
        }
        for r in reqs[:MAX_PENDING_ROWS]
    ]

    recap_rows: list[dict] = []
    recap_total = 0
    for kind, qs in (
        ("custom", CustomRecap.objects.filter(tenant_id=ctx.tenant_id)),
        ("legacy", Recap.objects.filter(event__tenant_id=ctx.tenant_id)),
    ):
        pending = (
            _filter_event_window(qs, "event__", None)
            .filter(approved=False, archived_at__isnull=True)
            .annotate(_day=_event_date_expr("event__"))
        )
        recap_total += pending.count()
        for r in pending.order_by("-_day", "-id").values(
            "uuid", "_day", "event__retailer__name", "event__request__retailer_name",
            "event__state__code", "event__address",
        )[:MAX_PENDING_ROWS]:
            recap_rows.append(
                {
                    "event_date": r["_day"].date().isoformat() if r["_day"] else None,
                    "store": r["event__retailer__name"] or r["event__request__retailer_name"] or None,
                    "state": r["event__state__code"] or extract_state_code(r["event__address"]) or None,
                    "url": _recap_url(kind, r["uuid"]),
                }
            )
    recap_rows.sort(key=lambda r: r["event_date"] or "", reverse=True)
    recap_rows = recap_rows[:MAX_PENDING_ROWS]
    return (
        {
            "brand": ctx.tenant_name,
            "note": "Read only: approve or decline these in Spark (links included).",
            "pending_requests_total": request_total,
            "pending_requests": request_rows,
            "recaps_awaiting_approval_total": recap_total,
            "recaps_awaiting_approval": recap_rows,
        },
        len(request_rows) + len(recap_rows),
    )


def get_weekly_summary(ctx: ToolContext, args: dict) -> tuple[dict, int]:
    from recaps.weekly_summary import PERIOD_WEEK
    from tenants.models import TenantWeeklySummary as Row

    week = _parse_day(args.get("week_start"), "week_start")
    rows = Row.objects.filter(
        tenant_id=ctx.tenant_id, period=PERIOD_WEEK, status=Row.STATUS_REVIEWED
    )
    if week is not None:
        rows = rows.filter(week_start=week)
    row = rows.order_by("-week_start").first()
    available = [
        d.isoformat()
        for d in Row.objects.filter(
            tenant_id=ctx.tenant_id, period=PERIOD_WEEK, status=Row.STATUS_REVIEWED
        )
        .order_by("-week_start")
        .values_list("week_start", flat=True)[:8]
    ]
    if row is None:
        return (
            {
                "brand": ctx.tenant_name,
                "summary": None,
                "message": "No published weekly summary for that week.",
                "published_weeks": available,
            },
            0,
        )
    return (
        {
            "brand": ctx.tenant_name,
            "week_start": row.week_start.isoformat(),
            "week_end": row.week_end.isoformat(),
            "published_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "summary": (row.edited_body or "").strip() or row.body,
            "published_weeks": available,
        },
        1,
    )


def get_connection_info(ctx: ToolContext, args: dict) -> tuple[dict, int]:
    from . import quota

    daily = conf.daily_calls_per_tenant()
    used = quota.used_today(ctx.tenant_id)
    return (
        {
            "brand": ctx.tenant_name,
            "access": "Read only. This connection can only see this brand's data.",
            "daily_call_limit_for_brand": daily,
            "calls_used_today": used,
            "calls_left_today": max(0, daily - used),
            "per_minute_limit": conf.calls_per_minute_per_connection(),
            "spark_url": conf.frontend_url(),
        },
        1,
    )


# ─── Registry ──────────────────────────────────────────────────────

_DATE_PROPS = {
    "start_date": {
        "type": "string",
        "description": "First day, YYYY-MM-DD (inclusive). Defaults to 29 days before end_date.",
    },
    "end_date": {
        "type": "string",
        "description": "Last day, YYYY-MM-DD (inclusive). Defaults to today (Pacific).",
    },
}

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    handler: object

    def definition(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"title": self.title, **_READ_ONLY},
        }


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "get_program_kpis",
            "Program KPIs",
            "Headline field-marketing results for the connected brand over a date range "
            "(max 366 days; default the last 30 days): events with recaps, approved "
            "recaps, consumers sampled, samples handed out, products sold, first-time, "
            "brand-aware and willing-to-purchase consumers, plus the same figures for "
            "the previous period of equal length. Approved recaps only, dated by event "
            "date — the same numbers as the Spark dashboard.",
            {"type": "object", "properties": dict(_DATE_PROPS), "additionalProperties": False},
            get_program_kpis,
        ),
        Tool(
            "list_recaps",
            "Recaps",
            "Approved event recaps for the connected brand in a date range (max 366 "
            "days; default the last 30 days), newest first, up to 50 per call. Each "
            "recap has the event date, store, state, activation type, consumers "
            "sampled, engagements and a link to open it in Spark. Brand ambassadors "
            "are not identified. Pass next_cursor back as cursor for the next page.",
            {
                "type": "object",
                "properties": {
                    **_DATE_PROPS,
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_PAGE,
                        "description": f"Recaps per page (1-{MAX_PAGE}, default {DEFAULT_PAGE}).",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "next_cursor from the previous call, same date range.",
                    },
                },
                "additionalProperties": False,
            },
            list_recaps,
        ),
        Tool(
            "get_consumer_breakdown",
            "Consumer breakdown",
            "Consumers sampled for the connected brand split by store, state or week, "
            "or units sold and sampled by product, over a date range (max 366 days; "
            "default the last 30 days). Store and state splits add up to the total. "
            "Large store lists fold their tail into one 'other' row.",
            {
                "type": "object",
                "properties": {
                    "by": {
                        "type": "string",
                        "enum": list(BREAKDOWN_DIMENSIONS),
                        "description": "How to split: store (default), state, week or product.",
                    },
                    **_DATE_PROPS,
                },
                "additionalProperties": False,
            },
            get_consumer_breakdown,
        ),
        Tool(
            "list_upcoming_events",
            "Upcoming events",
            "Scheduled events and not-yet-scheduled requests for the connected brand "
            "from today through the next N days (1-30, default 7), like the Upcoming "
            "page in Spark: date, local start and end time, store, address, activation "
            "type, status and how many brand ambassadors are booked. Up to 100 rows.",
            {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_UPCOMING_DAYS,
                        "description": "How many days ahead to look (default 7).",
                    }
                },
                "additionalProperties": False,
            },
            list_upcoming_events,
        ),
        Tool(
            "list_pending_approvals",
            "Pending approvals",
            "What is waiting on the connected brand in Spark: event requests pending "
            "approval and recaps awaiting approval, with totals and links (up to 50 of "
            "each). Read only — approving or declining happens in Spark.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            list_pending_approvals,
        ),
        Tool(
            "get_weekly_summary",
            "Weekly summary",
            "The latest published weekly program summary written for the connected "
            "brand (or the one for a given week), plus the weeks available. Only "
            "summaries Ignite has reviewed and published are returned.",
            {
                "type": "object",
                "properties": {
                    "week_start": {
                        "type": "string",
                        "description": "Monday of the week, YYYY-MM-DD. Omit for the latest.",
                    }
                },
                "additionalProperties": False,
            },
            get_weekly_summary,
        ),
        Tool(
            "get_connection_info",
            "Connection info",
            "Which brand this connection can read, and how many tool calls are left "
            "today under the brand's daily quota.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            get_connection_info,
        ),
    ]
}


def summarize_args(args: dict) -> dict:
    """What gets written to the call log: scalar arguments, trimmed."""
    out = {}
    for key, value in list((args or {}).items())[:10]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)[:40]] = value[:80] if isinstance(value, str) else value
        else:
            out[str(key)[:40]] = f"<{type(value).__name__}>"
    return out
