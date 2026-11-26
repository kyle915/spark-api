"""Natural-language bulk scheduling parser (`parseBulkSchedulePrompt`).

Turns a single plain-English scheduling sentence such as:
  "Every Fri & Sat in June from 12pm to 4pm at our top 5 Austin HEBs"
into 10–25 matched Account Map store + date preview rows ready to review and
submit via `createRequestsWave`.

Reads only; creates nothing until the user confirms in the UI.
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from datetime import date, timedelta
import logging

from events.retailer_aliases import accepted_alias_map, chain_key, resolve_store_name
from utils.ai_text import AiUnavailable, generate_json

logger = logging.getLogger(__name__)

MAX_BULK_STORES = 15
DEFAULT_BULK_STORES = 5
MAX_BULK_ROWS = 25

_WEEKDAY_MAP = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
_PY_WEEKDAY_LABELS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_PY_WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}

BULK_PROMPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "retailer_query",
        "city",
        "state",
        "store_limit",
        "selection_mode",
        "month",
        "year",
        "next_n_weeks",
        "weekdays",
        "specific_dates",
        "start_text",
        "end_text",
        "products",
        "notes",
    ],
    "properties": {
        "retailer_query": _NULLABLE_STR,
        "city": _NULLABLE_STR,
        "state": _NULLABLE_STR,
        "store_limit": _NULLABLE_INT,
        "selection_mode": {
            "type": "string",
            "enum": ["top_conversion", "top_volume", "overdue", "all_matching"],
        },
        "month": _NULLABLE_INT,
        "year": _NULLABLE_INT,
        "next_n_weeks": _NULLABLE_INT,
        "weekdays": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            },
        },
        "specific_dates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["month", "day", "year"],
                "properties": {
                    "month": {"type": "integer"},
                    "day": {"type": "integer"},
                    "year": _NULLABLE_INT,
                },
            },
        },
        "start_text": _NULLABLE_STR,
        "end_text": _NULLABLE_STR,
        "products": {"type": "array", "items": {"type": "string"}},
        "notes": _NULLABLE_STR,
    },
}

BULK_PROMPT_SYSTEM = (
    "You parse a plain-English multi-store demo scheduling request into "
    "structured filters. Extract only what is requested; return null when a "
    "filter is not mentioned. "
    "retailer_query: singular banner/store name if mentioned (e.g. 'HEBs' -> 'HEB', "
    "'Whole Foods stores' -> 'Whole Foods', 'top 5 stores' -> null). "
    "city: city name if mentioned (e.g. 'Austin'), else null. "
    "state: two-letter US state code if mentioned or unambiguously implied by a "
    "well-known state name (e.g. 'Texas' -> 'TX'), else null. "
    "store_limit: how many stores requested (e.g. 'top 5' -> 5, '10 stores' -> 10), "
    "or null when unspecified. "
    "selection_mode: 'top_conversion' when 'top'/'best' stores are asked for, "
    "'top_volume' when 'highest volume'/'busiest' is asked for, 'overdue' when "
    "'overdue'/'unvisited' is asked for, else 'all_matching'. "
    "month: 1-12 if a calendar month is named (e.g. 'in June' -> 6), else null. "
    "year: 4-digit year only if explicitly typed, else null. "
    "next_n_weeks: integer 1-12 if 'next N weeks' or 'next N weekends/Saturdays' "
    "is written, else null. "
    "weekdays: list of 3-letter weekdays ('mon'..'sun') requested (e.g. 'Every Fri & Sat' -> ['fri', 'sat'], 'weekends' -> ['sat', 'sun']). "
    "specific_dates: explicit calendar dates if named (e.g. 'June 5 and June 12'), else []. "
    "start_text and end_text: shift clock times as written (e.g. '12pm', '4pm'), else null. "
    "products: product/SKU names mentioned, else []. "
    "notes: special shift instructions if written, else null."
)


def _singular_chain_key(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    # Strip trailing plural 's' on multi-letter tokens like "HEBs" -> "HEB", "Sprouts" kept via prefix check.
    key = chain_key(text)
    return key


def expand_schedule_dates(spec: dict, *, today: date) -> list[date]:
    """Deterministically expand parsed date/weekday rules into sorted future dates."""
    from events.request_intake import resolve_date

    explicit: list[date] = []
    for item in spec.get("specific_dates") or []:
        if not isinstance(item, dict):
            continue
        when, _ = resolve_date(item.get("month"), item.get("day"), item.get("year"), today=today)
        if when is not None and when >= today and when not in explicit:
            explicit.append(when)
    if explicit:
        return sorted(explicit)[:16]

    raw_days = spec.get("weekdays") or []
    py_days = sorted({_WEEKDAY_MAP[d.lower()] for d in raw_days if str(d).lower() in _WEEKDAY_MAP})
    if not py_days:
        py_days = [4, 5]  # Default Fri & Sat when a month/window is given without weekdays

    month = spec.get("month")
    year = spec.get("year")
    if isinstance(month, int) and 1 <= month <= 12:
        if isinstance(year, int) and 2020 <= year <= today.year + 5:
            target_year = year if (year, month) >= (today.year, today.month) else today.year
        else:
            target_year = today.year if month >= today.month else today.year + 1
        _, last_day = calendar.monthrange(target_year, month)
        out = [
            date(target_year, month, d)
            for d in range(1, last_day + 1)
            if date(target_year, month, d) >= today
            and date(target_year, month, d).weekday() in py_days
        ]
        if out:
            return out[:16]

    n_weeks = spec.get("next_n_weeks")
    weeks = int(n_weeks) if isinstance(n_weeks, int) and 1 <= n_weeks <= 12 else 4
    horizon = today + timedelta(days=weeks * 7)
    cursor = today + timedelta(days=1)
    out = []
    while cursor <= horizon and len(out) < 16:
        if cursor.weekday() in py_days:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def filter_and_rank_tenant_stores(
    spec: dict,
    stores: list[dict],
) -> list[dict]:
    """Match and rank tenant Account Map stores against ``spec``."""
    raw_query = (spec.get("retailer_query") or "").strip()
    q_key = _singular_chain_key(raw_query)
    q_singular = q_key[:-1] if len(q_key) >= 4 and q_key.endswith("s") else q_key
    city_q = (spec.get("city") or "").strip().casefold()
    state_q = (spec.get("state") or "").strip().upper()
    if len(state_q) != 2:
        state_q = ""

    limit_raw = spec.get("store_limit")
    limit = (
        max(1, min(int(limit_raw), MAX_BULK_STORES))
        if isinstance(limit_raw, int) and limit_raw > 0
        else DEFAULT_BULK_STORES
    )
    mode = spec.get("selection_mode") or "top_conversion"

    matched: list[dict] = []
    for st in stores:
        if not (st.get("address") or "").strip():
            continue
        if state_q and (st.get("state") or "").upper() != state_q:
            continue
        if city_q:
            st_city = (st.get("city") or "").casefold()
            st_addr = (st.get("address") or "").casefold()
            if city_q not in st_city and city_q not in st_addr:
                continue
        if q_key:
            s_key = chain_key(st.get("name") or "")
            if (
                q_key not in s_key
                and s_key not in q_key
                and (not q_singular or q_singular not in s_key)
            ):
                continue
        matched.append(st)

    def _rank_key(item: dict) -> tuple:
        conv = float(item.get("conversion_pct") or 0.0)
        vol = int(item.get("consumers") or 0)
        visits = int(item.get("recaps") or 0)
        days = int(item.get("days_since") if item.get("days_since") is not None else 365)
        if mode == "top_volume":
            return (vol, conv, visits)
        if mode == "overdue":
            return (days, conv, vol)
        if mode == "all_matching":
            return (visits > 0, vol, conv)
        # top_conversion default
        return (conv, vol, visits)

    matched.sort(key=_rank_key, reverse=True)
    return matched[:limit]


def compose_bulk_preview(
    spec: dict,
    *,
    today: date,
    stores: list[dict],
    catalog: list[tuple[str, str]],
) -> tuple[bool, str, str, bool, bool, list[tuple[str, str]], list[dict]]:
    """Pure builder returning ``(ok, message, summary, truncated, time_defaulted, products, rows)``."""
    from events.request_intake import match_products, resolve_times

    picked_stores = filter_and_rank_tenant_stores(spec, stores)
    if not picked_stores:
        where_parts = [
            p
            for p in (
                spec.get("retailer_query"),
                spec.get("city"),
                spec.get("state"),
            )
            if p
        ]
        where = " in ".join([", ".join(where_parts)]) if where_parts else "your roster"
        return (
            False,
            f"No stores with a full street address matched ({where}). Try a broader city, state, or banner name.",
            "",
            False,
            False,
            [],
            [],
        )

    dates = expand_schedule_dates(spec, today=today)
    if not dates:
        return (
            False,
            "Could not work out future visit dates from that prompt. Name a month or weekdays (for example 'Every Fri & Sat in June').",
            "",
            False,
            False,
            [],
            [],
        )

    start_hhmm, end_hhmm, _ = resolve_times(spec.get("start_text"), spec.get("end_text"))
    time_defaulted = False
    if not start_hhmm or not end_hhmm:
        start_hhmm, end_hhmm = "12:00", "16:00"
        time_defaulted = True

    matched_products, _ = match_products(spec.get("products") or [], catalog)
    notes = " ".join(str(spec.get("notes") or "").split())[:500]

    # Pair dates x stores chronologically so every store gets visits across the window.
    all_pairs: list[tuple[dict, date]] = []
    for d in dates:
        for st in picked_stores:
            all_pairs.append((st, d))

    truncated = len(all_pairs) > MAX_BULK_ROWS
    sliced = all_pairs[:MAX_BULK_ROWS]

    rows: list[dict] = []
    for idx, (st, d) in enumerate(sliced, start=1):
        rows.append(
            {
                "row_key": f"{st['key']}:{d.isoformat()}:{idx}",
                "store_key": st["key"],
                "store_name": st["name"],
                "store_number": st.get("store_number") or "",
                "address": st["address"],
                "city": st.get("city") or "",
                "state": st.get("state") or "",
                "lat": st.get("lat"),
                "lng": st.get("lng"),
                "date": d.isoformat(),
                "weekday_label": _PY_WEEKDAY_LABELS[d.weekday()],
                "start_time": start_hhmm,
                "end_time": end_hhmm,
                "notes": notes,
            }
        )

    distinct_stores = len({r["store_key"] for r in rows})
    distinct_days = sorted({d.weekday() for _, d in sliced})
    day_str = ", ".join(_PY_WEEKDAY_SHORT[w] for w in distinct_days)
    summary = (
        f"{len(rows)} {'visit' if len(rows) == 1 else 'visits'} across "
        f"{distinct_stores} {'store' if distinct_stores == 1 else 'stores'} "
        f"({day_str} · {dates[0].isoformat()} to {sliced[-1][1].isoformat()})"
    )
    return True, "", summary, truncated, time_defaulted, matched_products, rows


def _load_tenant_bulk_context(
    tenant_id: int, today: date
) -> tuple[list[dict], list[tuple[str, str]]]:
    from events.market import parse_address_geo
    from events.models import Product, Request
    from events.retailer_aliases import split_store_number
    from recaps.explain_facts import recap_facts
    from recaps.store_places import tenant_places

    places = tenant_places(tenant_id)
    aliases = accepted_alias_map(tenant_id)

    name_counts: dict[str, Counter[str]] = defaultdict(Counter)
    raw_meta: dict[str, dict] = {}

    req_rows = Request.objects.filter(tenant_id=tenant_id, deleted_at__isnull=True).values_list(
        "pk",
        "retailer__name",
        "retailer_name",
        "name",
        "store_number",
        "address",
        "state__code",
        "coordinates",
    )
    for pk, retailer, typed, req_name, s_num, addr, st_code, coords in req_rows.order_by("pk"):
        skey = places.index.store_for_request(int(pk))
        if not skey:
            continue
        label = next((s.strip() for s in (retailer, typed, req_name) if s and s.strip()), "")
        if label:
            name_counts[skey][resolve_store_name(label, aliases)] += 1
        entry = raw_meta.setdefault(
            skey,
            {
                "key": skey,
                "name": "",
                "store_number": "",
                "address": "",
                "city": "",
                "state": "",
                "lat": None,
                "lng": None,
            },
        )
        if not entry["store_number"] and (s_num or "").strip():
            entry["store_number"] = str(s_num).strip()
        if not entry["address"] and (addr or "").strip():
            entry["address"] = addr.strip()
            geo = parse_address_geo(entry["address"])
            entry["city"] = (geo.city or "").strip()
            entry["state"] = (st_code or geo.state_code or "").upper()
        if not entry["state"] and st_code:
            entry["state"] = st_code.upper()
        if entry["lat"] is None and coords is not None and hasattr(coords, "y") and hasattr(coords, "x"):
            lat, lng = float(coords.y), float(coords.x)
            if (lat, lng) != (0.0, 0.0):
                entry["lat"], entry["lng"] = lat, lng

    for skey, counts in name_counts.items():
        if counts and skey in raw_meta:
            best_name = counts.most_common(1)[0][0]
            raw_meta[skey]["name"] = best_name
            if not raw_meta[skey]["store_number"]:
                _, num, marked = split_store_number(best_name)
                if num and marked:
                    raw_meta[skey]["store_number"] = num

    recaps = recap_facts(tenant_id, today - timedelta(days=365), today)
    event_ids = {int(r["event_id"]) for r in recaps if r.get("event_id")}
    ev_to_store = places.for_event_ids(event_ids)

    perf: dict[str, dict] = defaultdict(
        lambda: {"recaps": 0, "consumers": 0, "units": 0, "base": 0, "last_day": None}
    )
    for row in recaps:
        skey = ev_to_store.get(int(row.get("event_id") or 0))
        if not skey or skey not in raw_meta:
            continue
        p = perf[skey]
        p["recaps"] += 1
        p["consumers"] += int(row.get("consumers") or 0)
        if row.get("base") is not None:
            p["base"] += int(row.get("base") or 0)
            p["units"] += int(row.get("units") or 0)
        d = row.get("day")
        if isinstance(d, date) and (p["last_day"] is None or d > p["last_day"]):
            p["last_day"] = d

    stores: list[dict] = []
    for skey, meta in raw_meta.items():
        if not meta["name"]:
            continue
        p = perf.get(skey)
        conv = round(100.0 * p["units"] / p["base"], 1) if p and p["base"] else 0.0
        last_day = p["last_day"] if p else None
        stores.append(
            {
                **meta,
                "recaps": int(p["recaps"]) if p else 0,
                "consumers": int(p["consumers"]) if p else 0,
                "conversion_pct": conv,
                "days_since": (today - last_day).days if last_day else None,
            }
        )

    catalog = [
        (str(uuid), name)
        for uuid, name in Product.objects.filter(tenant_id=tenant_id)
        .order_by("id")
        .values_list("uuid", "name")
    ]
    return stores, catalog


def parse_bulk_schedule_prompt(
    tenant_id: int, prompt: str | None, *, today: date | None = None
) -> tuple[bool, str, str, bool, bool, list[tuple[str, str]], list[dict]]:
    from events.live_board import _ops_today
    from events.request_intake import UNAVAILABLE_MESSAGE, clean_pasted_text

    cleaned, _ = clean_pasted_text(prompt)
    if len(cleaned) < 6:
        return (
            False,
            "Type a scheduling sentence first (for example 'Every Fri & Sat in June at our top 5 Austin HEBs').",
            "",
            False,
            False,
            [],
            [],
        )

    now_day = today or _ops_today()
    try:
        spec = generate_json(
            BULK_PROMPT_SYSTEM,
            f"Today is {now_day.isoformat()}.\n\nScheduling prompt:\n{cleaned[:2000]}",
            schema=BULK_PROMPT_SCHEMA,
            max_tokens=900,
            timeout=15.0,
            small=True,
        )
    except AiUnavailable:
        return False, UNAVAILABLE_MESSAGE, "", False, False, [], []
    except Exception:  # noqa: BLE001
        logger.info("bulk schedule prompt AI call failed", exc_info=True)
        return False, UNAVAILABLE_MESSAGE, "", False, False, [], []

    if not isinstance(spec, dict):
        return False, UNAVAILABLE_MESSAGE, "", False, False, [], []

    stores, catalog = _load_tenant_bulk_context(tenant_id, now_day)
    return compose_bulk_preview(spec, today=now_day, stores=stores, catalog=catalog)
