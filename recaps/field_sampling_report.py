"""Field Sampling Report — a consolidated, client-facing rollup for tenants
running a guerrilla field-sampling program across metro markets (Feel
Free/Botanic Tonics being the first — and so far only — one). Kyle's ask,
verbatim: samples per hour, YTD + weekly SKU breakdown, locations hit,
what's coming up next week, and field call-outs.

Reuses the building blocks the rest of the tenant-overview family already
established rather than re-deriving any of them, so these numbers can
never quietly drift from the dashboard card or the P&L lens:
  * the metro-week grid + event-date window filter from
    :mod:`recaps.tenant_overview` (:func:`tenant_metro_breakdown`,
    :func:`_filter_event_window`)
  * the clock-in/scheduled-duration-fallback hours logic from
    :mod:`events.pnl` (:func:`event_pnl_rows`), UNCHANGED
  * the same free-text feedback field-name convention
    :mod:`recaps.tenant_sentiment` uses (duplicated here per this
    codebase's own precedent — recap_quality.py does the same rather than
    cross-importing a private module constant)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from django.db.models import Sum
from django.utils import timezone

from events.models import Event
from recaps.models import CustomFieldValue, CustomRecap, CustomRecapProductSample
from recaps.tenant_overview import _filter_event_window
from utils.gemini_text import generate_gemini_text

# Mirrors recaps.tenant_sentiment._CUSTOM_FEEDBACK_NAME_RE (also duplicated
# in recaps/recap_quality.py) — free-text fields worth surfacing as
# "things to note." A deliberately PLAIN STRING (not re.compile()'d) used
# ONLY as a Postgres `__iregex` filter, with `\y` instead of `\b` for the
# word boundary around "notes?": Postgres's regex engine does NOT treat
# `\b` as a word boundary the way Python's `re` does (confirmed live —
# `'Field Notes' ~* '\bnotes?\b'` is FALSE, silently excluding a field just
# named "Notes"/"Field Notes" from every __iregex query built on the
# original pattern; `\ynotes?\y` is Postgres's correct spelling). Can't be
# `re.compile()`d — Python's `re` rejects `\y` outright (`bad escape \y`).
# The upstream compiled constant can't just switch to `\y` either —
# recap_quality.py reuses the SAME pattern with Python's re.search(), where
# `\y` means nothing. tenant_sentiment.py now carries its own `\y` sibling
# (`_CUSTOM_FEEDBACK_NAME_IREGEX`) for its own `__iregex` use; kept duplicated
# here rather than imported, per this module's own precedent above.
_FEEDBACK_NAME_RE_IREGEX = (
    r"quote|story|stories|highlight|feedback|testimonial|comment|"
    r"reaction|sentiment|verbatim|\ynotes?\y|consumer.*sa(?:id|y)|"
    r"what.*sa(?:id|y)|said"
)

# Matches Feel Free's "Which products were sampled?" choice field (and any
# similarly-worded tenant field) — the categorical fallback used when a
# tenant hasn't turned on structured per-product quantities
# (CustomRecapTemplate.product_samples).
_PRODUCT_CHOICE_NAME_RE = re.compile(r"which.*product|product.*sampl", re.IGNORECASE)

MAX_CALLOUTS = 20
MAX_LOCATIONS = 100
MAX_UPCOMING = 20


def _parse_event_name(name: str | None) -> tuple[str | None, str | None]:
    """(market, corridor) from "<Market> — <Corridor> · <date>", e.g.
    "Miami — Wynwood · 9/24" -> ("Miami", "Wynwood").

    See :func:`recaps.tenant_overview._metro_from_event_name` for the
    market-only sibling the metro-week breakdown uses — duplicated (not
    imported) here since this additionally splits out the corridor for
    "locations hit" / call-out reporting, and this module already follows
    this file's regex-duplication convention above.
    """
    if not name:
        return None, None
    parts = name.split(" — ", 1)
    if len(parts) != 2:
        return None, None
    market = parts[0].strip() or None
    corridor = parts[1].split(" · ", 1)[0].strip() or None
    return market, corridor


def _ytd_window() -> tuple[datetime, datetime]:
    """[Jan 1 this year, now) — a FIXED year-to-date window, independent of
    whatever custom range the caller's date selector is set to.
    """
    now = timezone.now()
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, now


def sku_breakdown(
    tenant_id: int,
    start: datetime,
    end: datetime,
    event_type_id: int | None = None,
    market: str | None = None,
) -> dict:
    """Per-SKU sample totals for one tenant/window, with an honest fallback.

    Two possible ``mode``s, since not every tenant's template captures the
    same granularity:

    * ``"quantity"`` — real summed :class:`CustomRecapProductSample`
      quantities per product (the tenant's template has
      ``product_samples=True`` and BAs log per-SKU counts). ``items`` are
      ``{"product": name, "total": int}`` — a TRUE unit count.
    * ``"sessions"`` — fallback when no structured quantity rows exist in
      window: counts how many DISTINCT recap sessions selected each
      product via a "which products were sampled" choice field
      (:data:`_PRODUCT_CHOICE_NAME_RE`). Same item shape, but ``total`` is
      a SESSION count, not a unit count — deliberately kept in a
      different ``mode`` so a reader can never mistake one for the other.
    * ``"none"`` — neither mechanism has any data in window.

    ``market`` optionally restricts to one metro label (parsed the same
    way :func:`recaps.tenant_overview.tenant_metro_breakdown` does).
    """
    base = CustomRecap.objects.filter(tenant_id=tenant_id)
    if event_type_id is not None:
        base = base.filter(event__event_type_id=event_type_id)
    windowed = _filter_event_window(base, "event__", (start, end))

    if market is not None:
        recap_ids = [
            rid
            for rid, name in windowed.values_list("id", "event__name")
            if _parse_event_name(name)[0] == market
        ]
        windowed = CustomRecap.objects.filter(id__in=recap_ids)

    structured = (
        CustomRecapProductSample.objects.filter(custom_recap__in=windowed)
        .values("product__name")
        .annotate(total=Sum("quantity"))
        .order_by("-total")
    )
    structured_items = [
        {"product": row["product__name"], "total": int(row["total"] or 0)}
        for row in structured
        if row["product__name"]
    ]
    if structured_items:
        return {"mode": "quantity", "items": structured_items}

    # Fallback: categorical "which products" choice field. Values are a
    # single option string ("select") or a JSON array of option strings
    # ("multiselect") — see recaps.models.CustomField.options docstring.
    sessions: dict[str, set[int]] = {}
    for recap_id, value in CustomFieldValue.objects.filter(
        custom_recap__in=windowed,
        custom_field__name__iregex=_PRODUCT_CHOICE_NAME_RE.pattern,
    ).values_list("custom_recap_id", "value"):
        names: list[str] = []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                names = [str(n) for n in parsed if n]
            elif parsed:
                names = [str(parsed)]
        except (TypeError, ValueError):
            if value:
                names = [str(value)]
        for n in names:
            cleaned = n.strip()
            if cleaned:
                sessions.setdefault(cleaned, set()).add(recap_id)

    if sessions:
        items = [
            {"product": name, "total": len(ids)}
            for name, ids in sorted(sessions.items(), key=lambda kv: -len(kv[1]))
        ]
        return {"mode": "sessions", "items": items}

    return {"mode": "none", "items": []}


def samples_per_hour(
    tenant_id: int,
    start: datetime,
    end: datetime,
    event_type_id: int | None = None,
    market: str | None = None,
) -> dict:
    """Total samples ÷ total labor hours for one tenant/window.

    Samples reuse :func:`recaps.tenant_overview.tenant_metro_breakdown`'s
    already-computed per-(metro, week) ``consumers_reached`` cells (summed
    across the window / market filter) rather than re-deriving the KPI
    parse rules a third time. Hours reuse
    :func:`events.pnl.event_pnl_rows` VERBATIM — the same clock-in/clock-
    out-with-scheduled-duration-fallback logic the P&L lens uses — scoped
    down to this window's qualifying event ids afterward (that function
    isn't event-type/metro aware, and it's shared/proven code we don't
    want to fork or modify for this one caller).

    Returns ``{"samples": int, "hours": float, "per_hour": float | None,
    "estimated": bool}`` — ``estimated`` is True when ANY contributing
    event had no real clock pair and fell back to its scheduled duration
    (rolled up from ``event_pnl_rows``' per-event flag).
    """
    from events.pnl import event_pnl_rows
    from recaps.tenant_overview import tenant_metro_breakdown

    breakdown = tenant_metro_breakdown(tenant_id, start, end, event_type_id)
    samples = 0
    for week in breakdown["weeks"]:
        for metro, cell in week["cells"].items():
            if market is not None and metro != market:
                continue
            samples += cell["consumers_reached"]

    events_qs = Event.objects.filter(tenant_id=tenant_id)
    if event_type_id is not None:
        events_qs = events_qs.filter(event_type_id=event_type_id)
    events_qs = _filter_event_window(events_qs, "", (start, end))
    if market is not None:
        qualifying_ids = {
            eid
            for eid, name in events_qs.values_list("id", "name")
            if _parse_event_name(name)[0] == market
        }
    else:
        qualifying_ids = set(events_qs.values_list("id", flat=True))

    hours = 0.0
    estimated = False
    for row in event_pnl_rows(tenant_id, start.date(), end.date()):
        if row["event_id"] not in qualifying_ids:
            continue
        hours += row["hours"]
        estimated = estimated or row["estimated"]

    per_hour = round(samples / hours, 2) if hours > 0 else None
    return {
        "samples": samples,
        "hours": round(hours, 2),
        "per_hour": per_hour,
        "estimated": estimated,
    }


def locations_hit(
    tenant_id: int,
    start: datetime,
    end: datetime,
    event_type_id: int | None = None,
    market: str | None = None,
) -> list[dict]:
    """Distinct stops actually run in [start, end) — one row per event
    whose name follows the "<Market> — <Corridor> · <date>" convention;
    events that don't are skipped (same posture as the metro breakdown —
    nothing to show rather than a guess). Sorted oldest-to-newest, capped
    at :data:`MAX_LOCATIONS`.
    """
    qs = Event.objects.filter(tenant_id=tenant_id)
    if event_type_id is not None:
        qs = qs.filter(event_type_id=event_type_id)
    qs = _filter_event_window(qs, "", (start, end))

    rows = []
    for name, evtdate, address in qs.values_list("name", "_evtdate", "address"):
        m, corridor = _parse_event_name(name)
        if not m or not corridor:
            continue
        if market is not None and m != market:
            continue
        rows.append(
            {
                "market": m,
                "corridor": corridor,
                "date": evtdate.date().isoformat() if evtdate else None,
                "address": address or None,
            }
        )
    rows.sort(key=lambda r: r["date"] or "")
    return rows[:MAX_LOCATIONS]


def upcoming_shifts(
    tenant_id: int,
    event_type_id: int | None = None,
    market: str | None = None,
    days: int = 7,
) -> dict:
    """This tenant's next ``days`` days of scheduled shifts — ALWAYS
    relative to right now, independent of whatever historical window the
    report's date selector is showing. Mirrors
    :func:`recaps.weekly_digest.build_weekly_digest`'s "Coming up" section
    (duplicated rather than imported: that one is a private builder for
    the weekly-digest EMAIL, not exposed for reuse, and isn't
    metro/event-type aware).

    Returns ``{"total": int, "items": [...]}`` — ``items`` capped at
    :data:`MAX_UPCOMING`; ``total`` is the real count so the frontend can
    show "+N more" honestly instead of silently truncating.
    """
    now = timezone.now()
    horizon = now + timedelta(days=days)
    qs = Event.objects.filter(
        tenant_id=tenant_id, start_time__gte=now, start_time__lt=horizon
    )
    if event_type_id is not None:
        qs = qs.filter(event_type_id=event_type_id)
    qs = qs.order_by("start_time")

    items = []
    for name, start_time, address in qs.values_list("name", "start_time", "address"):
        m, corridor = _parse_event_name(name)
        if market is not None and m != market:
            continue
        items.append(
            {
                "market": m,
                "corridor": corridor,
                "name": name,
                "start_time": start_time.isoformat() if start_time else None,
                "address": address or None,
            }
        )
    return {"total": len(items), "items": items[:MAX_UPCOMING]}


def field_callouts(
    tenant_id: int,
    start: datetime,
    end: datetime,
    event_type_id: int | None = None,
    market: str | None = None,
) -> list[dict]:
    """Free-text BA notes/feedback from recaps in [start, end), tagged with
    their event's date/market/corridor — the deterministic, no-AI "things
    to note" feed. See :func:`generate_ai_callout_summary` for the
    optional on-demand Gemini narrative built FROM this same data.

    Hard-capped at :data:`MAX_CALLOUTS`, most-recent-first, deduped on
    cleaned text (mirrors :func:`recaps.tenant_sentiment.
    gather_consumer_feedback`'s dedup posture).
    """
    base = CustomRecap.objects.filter(tenant_id=tenant_id)
    if event_type_id is not None:
        base = base.filter(event__event_type_id=event_type_id)
    windowed = _filter_event_window(base, "event__", (start, end))

    placement: dict[int, tuple[str | None, str | None, object]] = {}
    for recap_id, name, evtdate in windowed.values_list(
        "id", "event__name", "_evtdate"
    ):
        m, corridor = _parse_event_name(name)
        if market is not None and m != market:
            continue
        placement[recap_id] = (m, corridor, evtdate)

    if not placement:
        return []

    seen_text: set[str] = set()
    items: list[dict] = []
    rows = (
        CustomFieldValue.objects.filter(
            custom_recap_id__in=placement.keys(),
            custom_field__name__iregex=_FEEDBACK_NAME_RE_IREGEX,
        )
        .exclude(value="")
        .values_list("custom_recap_id", "value")
    )
    for recap_id, value in rows:
        text = re.sub(r"\s+", " ", (value or "")).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        m, corridor, evtdate = placement[recap_id]
        items.append(
            {
                "market": m,
                "corridor": corridor,
                "date": evtdate.date().isoformat() if evtdate else None,
                "text": text[:280],
            }
        )

    items.sort(key=lambda r: r["date"] or "", reverse=True)
    return items[:MAX_CALLOUTS]


def build_field_sampling_report(
    tenant_id: int,
    start: datetime,
    end: datetime,
    event_type_id: int | None = None,
    market: str | None = None,
) -> dict:
    """Everything the Field Sampling Report page needs, in one call:
    samples/hour, YTD + selected-window SKU breakdowns, locations hit,
    next-7-days upcoming shifts, and the deterministic call-outs feed.

    ``start``/``end`` scope the "this week" figures (SKU breakdown,
    samples/hour, locations, call-outs) — the caller's date-range
    selector. YTD is ALWAYS Jan 1 of the current year through now,
    regardless of that selection; ``upcoming`` is ALWAYS the real next 7
    days from now, regardless of both.
    """
    ytd_start, ytd_end = _ytd_window()
    return {
        "samples_per_hour": samples_per_hour(
            tenant_id, start, end, event_type_id, market
        ),
        "ytd_sku_breakdown": sku_breakdown(
            tenant_id, ytd_start, ytd_end, event_type_id, market
        ),
        "week_sku_breakdown": sku_breakdown(
            tenant_id, start, end, event_type_id, market
        ),
        "locations_hit": locations_hit(tenant_id, start, end, event_type_id, market),
        "upcoming": upcoming_shifts(tenant_id, event_type_id, market),
        "callouts": field_callouts(tenant_id, start, end, event_type_id, market),
    }


def generate_ai_callout_summary(
    tenant_name: str,
    callouts: list[dict],
    context: dict,
) -> str | None:
    """On-demand Gemini narrative over the deterministic call-outs feed —
    NEVER called automatically; only from an explicit "Summarize with AI"
    action, so a client-facing report's numbers are never gated behind an
    AI call. Matches this codebase's own precedent
    (recaps/tenant_insights.py replaced free-form AI themes with fixed
    deterministic buckets for exactly this reliability reason).

    ``callouts`` is :func:`field_callouts`'s output; ``context`` is this
    same window's headline numbers (samples/hours/per_hour) so the model
    references real figures instead of inventing its own math. Returns
    None (never raises) if Gemini is unavailable or the call fails — the
    caller always has the deterministic feed to show either way.
    """
    if not callouts:
        return None
    lines = [
        f"- {c['market'] or 'Unknown market'} · {c['corridor'] or ''} · "
        f"{c['date'] or ''}: {c['text']}"
        for c in callouts
    ]
    prompt = (
        f"You are summarizing one period of field sampling activity for "
        f"{tenant_name}, a guerrilla field-marketing program, for an "
        "internal report a client will read. This period: "
        f"{context.get('samples', 0)} samples across "
        f"{context.get('hours', 0)} labor hours "
        f"({context.get('per_hour')} samples/hour). "
        "Below are raw field notes/feedback submitted by brand "
        "ambassadors. Write a SHORT plain-text summary (3-5 bullet "
        "points, no markdown headers) of what's actually worth the "
        "client knowing: notable wins, problems, weather/turnout issues, "
        "anything unusual. Do not invent facts not present in the notes. "
        "If the notes are mundane, say so briefly rather than padding.\n\n"
        + "\n".join(lines)
    )
    return generate_gemini_text(prompt, max_output_tokens=400)
