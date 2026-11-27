"""Buyer-safe QBR / Line-Review dataset builders for BuyerPitchDeck snapshots.

Computes four strictly tenant-scoped, buyer-safe sections stored inside
``BuyerPitchDeck.snapshot_payload`` (no DB migrations required):

1. ``footprint`` — US state and market activation totals for the choropleth map.
2. ``posHalo`` — 2–4 week post-demo POS velocity trajectory vs pre-demo baseline.
3. ``shelfCompliance`` — On-shelf availability (OSA), price-tag compliance,
   average facings, and median retail shelf price from stored ``ShelfReading`` /
   ``ShelfReadingCorrection`` rows (staff notes never leave this module).
4. ``shopperVoice`` — AI-distilled themes and PII-scrubbed verbatim shopper
   quotes from approved recaps (no ambassador names, emails, or phone numbers).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

from pilot.imports import brand_included_visits_queryset
from pilot.models import CanonicalVisit, RetailerSalesImport, RetailerSalesRow
from pilot.pos import monday_of_week

logger = logging.getLogger(__name__)

_US_STATE_CODES = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
        "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
        "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
        "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
        "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    }
)
_STATE_SUFFIX_RE = re.compile(r"(?:,\s*|\b)([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$")

MAX_FOOTPRINT_MARKETS = 12
MAX_VOICE_THEMES = 6
MAX_VOICE_QUOTES = 6


def extract_state_code(*candidates: str | None) -> str:
    """Resolve a 2-letter US postal state code from explicit or address/market text."""
    for raw in candidates:
        text = (raw or "").strip()
        if not text:
            continue
        upper = text.upper()
        if len(upper) == 2 and upper in _US_STATE_CODES:
            return upper
        match = _STATE_SUFFIX_RE.search(upper)
        if match and match.group(1) in _US_STATE_CODES:
            return match.group(1)
    return ""


def _round1(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _round2_str(value: Decimal | float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def scoped_canonical_visits(
    tenant,
    *,
    period_start: date,
    period_end: date,
    retailer_filter: str = "",
    market_filter: str = "",
    provider_uuid: str | None = None,
) -> list[CanonicalVisit]:
    """Return included CanonicalVisits matching the exact buyer-report scope."""
    qs = brand_included_visits_queryset(tenant).filter(
        visit_date__gte=period_start,
        visit_date__lte=period_end,
    )
    if provider_uuid:
        qs = qs.filter(primary_provider__uuid=provider_uuid)
    visits = list(qs)
    if market_filter:
        mf = market_filter.strip().lower()
        visits = [
            v
            for v in visits
            if mf in (v.market or "").lower()
            or mf in (v.city or "").lower()
            or mf in (v.state_code or "").lower()
        ]
    if retailer_filter:
        rf = retailer_filter.strip().lower()
        visits = [
            v
            for v in visits
            if rf in (v.chain_label or "").lower()
            or rf in (v.store_label or "").lower()
            or rf == (v.chain_key or "").lower()
        ]
    return visits


def build_pitch_footprint(
    visits: list[CanonicalVisit],
    *,
    adj_units_by_store: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Aggregate activation footprint by US state and top market."""
    adj_map = adj_units_by_store or {}
    state_buckets: dict[str, dict[str, Any]] = {}
    market_buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for v in visits:
        st = extract_state_code(v.state_code, v.market, v.address)
        market_label = (v.market or v.city or st or "").strip()
        sk = v.store_key or f"visit:{v.id}"
        samples = int(v.samples_distributed or 0)
        rep_u = int(v.rep_observed_units or 0)

        if st:
            sb = state_buckets.setdefault(
                st,
                {
                    "state": st,
                    "storeKeys": set(),
                    "visits": 0,
                    "samples": 0,
                    "repUnits": 0,
                },
            )
            sb["storeKeys"].add(sk)
            sb["visits"] += 1
            sb["samples"] += samples
            sb["repUnits"] += rep_u

        if market_label:
            mkey = (market_label.lower(), st)
            mb = market_buckets.setdefault(
                mkey,
                {
                    "market": market_label,
                    "state": st,
                    "storeKeys": set(),
                    "visits": 0,
                    "samples": 0,
                    "repUnits": 0,
                },
            )
            mb["storeKeys"].add(sk)
            mb["visits"] += 1
            mb["samples"] += samples
            mb["repUnits"] += rep_u

    states_out: list[dict[str, Any]] = []
    for sb in state_buckets.values():
        reg_vals = [adj_map[sk] for sk in sb["storeKeys"] if sk in adj_map]
        states_out.append(
            {
                "state": sb["state"],
                "stores": len(sb["storeKeys"]),
                "visits": sb["visits"],
                "samples": sb["samples"],
                "repUnits": sb["repUnits"],
                "registerUnits": sum(reg_vals) if reg_vals else None,
            }
        )
    states_out.sort(key=lambda r: (-r["visits"], -r["samples"], r["state"]))

    markets_out: list[dict[str, Any]] = []
    for mb in market_buckets.values():
        markets_out.append(
            {
                "market": mb["market"],
                "state": mb["state"],
                "stores": len(mb["storeKeys"]),
                "visits": mb["visits"],
                "samples": mb["samples"],
                "repUnits": mb["repUnits"],
            }
        )
    markets_out.sort(key=lambda r: (-r["visits"], -r["samples"], r["market"].lower()))

    return {
        "statesCount": len(states_out),
        "marketsCount": len(markets_out),
        "states": states_out,
        "topMarkets": markets_out[:MAX_FOOTPRINT_MARKETS],
    }


def synthesize_fallback_footprint(
    top_store_wins: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a lightweight footprint for legacy decks minted before QBR enrichment."""
    state_buckets: dict[str, dict[str, Any]] = {}
    market_buckets: dict[str, dict[str, Any]] = {}
    for row in top_store_wins:
        market = str(row.get("market") or "").strip()
        st = extract_state_code(market, str(row.get("storeLabel") or ""))
        visits = int(row.get("visits") or 0)
        samples = int(row.get("samplesDistributed") or 0)
        rep_u = int(row.get("repObservedUnits") or 0)
        if st:
            sb = state_buckets.setdefault(
                st,
                {
                    "state": st,
                    "stores": 0,
                    "visits": 0,
                    "samples": 0,
                    "repUnits": 0,
                    "registerUnits": None,
                },
            )
            sb["stores"] += 1
            sb["visits"] += visits
            sb["samples"] += samples
            sb["repUnits"] += rep_u
        if market:
            mb = market_buckets.setdefault(
                market.lower(),
                {
                    "market": market,
                    "state": st,
                    "stores": 0,
                    "visits": 0,
                    "samples": 0,
                    "repUnits": 0,
                },
            )
            mb["stores"] += 1
            mb["visits"] += visits
            mb["samples"] += samples
            mb["repUnits"] += rep_u

    states = sorted(
        state_buckets.values(), key=lambda r: (-r["visits"], -r["samples"], r["state"])
    )
    markets = sorted(
        market_buckets.values(),
        key=lambda r: (-r["visits"], -r["samples"], r["market"].lower()),
    )[:MAX_FOOTPRINT_MARKETS]
    return {
        "statesCount": len(states),
        "marketsCount": len(markets),
        "states": states,
        "topMarkets": markets,
    }


def compute_pos_halo_from_rows(
    demo_weeks_by_store: dict[str, list[date]],
    sales_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pure computation of Pre-demo baseline -> Demo Week -> Weeks +1..+4 POS halo.

    Each ``sales_rows`` entry has:
      ``store_key``, ``product_id``, ``week_start``, ``register_units``,
      ``baseline_units`` (optional Decimal/float/None).
    """
    if not demo_weeks_by_store or not sales_rows:
        return {
            "available": False,
            "note": "Connect retailer POS feeds spanning demo and post-demo weeks to measure 2–4 week velocity halo.",
            "baselineAvgUnits": None,
            "demoWeekLiftPct": None,
            "postDemoHaloPct": None,
            "matchedStores": 0,
            "weeks": [],
        }

    # Deduplicate rows by (store_key, product_id, week_start)
    dedup: dict[tuple[str, int, date], dict[str, Any]] = {}
    for row in sales_rows:
        sk = row.get("store_key")
        pid = row.get("product_id")
        wk = row.get("week_start")
        units = row.get("register_units")
        if not sk or pid is None or wk is None or units is None:
            continue
        dedup[(str(sk), int(pid), wk)] = row

    bucket_units: dict[int, list[float]] = {k: [] for k in (-1, 0, 1, 2, 3, 4)}
    inline_baselines: list[float] = []
    matched_stores: set[str] = set()

    for (sk, _pid, wk), row in dedup.items():
        demo_weeks = demo_weeks_by_store.get(sk)
        if not demo_weeks:
            continue
        units_val = float(row["register_units"])
        base_raw = row.get("baseline_units")

        # Find nearest demo week on or before wk within 0..4 weeks
        best_post: int | None = None
        for dw in demo_weeks:
            diff_days = (wk - dw).days
            if 0 <= diff_days <= 28 and diff_days % 7 == 0:
                offset = diff_days // 7
                if best_post is None or offset < best_post:
                    best_post = offset
        if best_post is not None:
            bucket_units[best_post].append(units_val)
            matched_stores.add(sk)
            if best_post == 0 and base_raw is not None:
                inline_baselines.append(float(base_raw))
            continue

        # Otherwise check pre-demo baseline window (-4..-1 weeks before earliest/any demo)
        for dw in demo_weeks:
            diff_days = (wk - dw).days
            if -28 <= diff_days <= -7 and diff_days % 7 == 0:
                bucket_units[-1].append(units_val)
                matched_stores.add(sk)
                break

    has_demo_or_post = any(bucket_units[k] for k in (0, 1, 2, 3, 4))
    if not has_demo_or_post:
        return {
            "available": False,
            "note": "No matched retailer POS store-weeks found in this deck's store and date scope yet.",
            "baselineAvgUnits": None,
            "demoWeekLiftPct": None,
            "postDemoHaloPct": None,
            "matchedStores": 0,
            "weeks": [],
        }

    baseline_samples = bucket_units[-1] or inline_baselines
    baseline_avg: float | None = (
        _round1(sum(baseline_samples) / len(baseline_samples))
        if baseline_samples
        else None
    )

    labels = {
        -1: "Pre-demo baseline",
        0: "Demo week",
        1: "Week +1",
        2: "Week +2",
        3: "Week +3",
        4: "Week +4",
    }

    weeks_out: list[dict[str, Any]] = []
    if baseline_avg is not None:
        weeks_out.append(
            {
                "offset": -1,
                "label": labels[-1],
                "avgUnits": baseline_avg,
                "totalUnits": int(round(sum(baseline_samples))),
                "storeWeeks": len(baseline_samples),
                "liftPct": 0.0,
            }
        )

    for offset in (0, 1, 2, 3, 4):
        vals = bucket_units[offset]
        if not vals:
            continue
        avg_u = _round1(sum(vals) / len(vals))
        lift_pct: float | None = None
        if baseline_avg is not None and baseline_avg > 0:
            lift_pct = _round1(((avg_u - baseline_avg) / baseline_avg) * 100.0)
        weeks_out.append(
            {
                "offset": offset,
                "label": labels[offset],
                "avgUnits": avg_u,
                "totalUnits": int(round(sum(vals))),
                "storeWeeks": len(vals),
                "liftPct": lift_pct,
            }
        )

    demo_week_lift: float | None = None
    if bucket_units[0] and baseline_avg is not None and baseline_avg > 0:
        demo_avg = sum(bucket_units[0]) / len(bucket_units[0])
        demo_week_lift = _round1(((demo_avg - baseline_avg) / baseline_avg) * 100.0)

    post_vals = [v for k in (1, 2, 3, 4) for v in bucket_units[k]]
    post_halo_pct: float | None = None
    if post_vals and baseline_avg is not None and baseline_avg > 0:
        post_avg = sum(post_vals) / len(post_vals)
        post_halo_pct = _round1(((post_avg - baseline_avg) / baseline_avg) * 100.0)

    return {
        "available": True,
        "note": (
            "Average matched register units per store-SKU-week before, during, "
            "and 1–4 weeks after in-store demos."
        ),
        "baselineAvgUnits": baseline_avg,
        "demoWeekLiftPct": demo_week_lift,
        "postDemoHaloPct": post_halo_pct,
        "matchedStores": len(matched_stores),
        "weeks": weeks_out,
    }


def build_pitch_pos_halo(
    tenant,
    *,
    visits: list[CanonicalVisit],
    period_start: date,
    period_end: date,
    scoped: bool,
) -> dict[str, Any]:
    """Load active POS rows spanning 4 weeks pre-demo to 4 weeks post-demo and compute halo."""
    if not RetailerSalesImport.objects.filter(
        tenant=tenant, is_active_version=True
    ).exists():
        return compute_pos_halo_from_rows({}, [])

    demo_weeks_by_store: dict[str, list[date]] = {}
    for v in visits:
        if not v.store_key:
            continue
        wk = monday_of_week(v.visit_date)
        wks = demo_weeks_by_store.setdefault(v.store_key, [])
        if wk not in wks:
            wks.append(wk)
    if not demo_weeks_by_store:
        return compute_pos_halo_from_rows({}, [])

    window_start = monday_of_week(period_start) - timedelta(days=28)
    window_end = monday_of_week(period_end) + timedelta(days=28)

    rows_qs = RetailerSalesRow.objects.filter(
        tenant=tenant,
        sales_import__is_active_version=True,
        match_status=RetailerSalesRow.MATCH_MATCHED,
        store_key__in=list(demo_weeks_by_store.keys()),
        week_start__gte=window_start,
        week_start__lte=window_end,
    ).exclude(product_id__isnull=True).exclude(register_units__isnull=True)

    sales_rows = [
        {
            "store_key": r.store_key,
            "product_id": r.product_id,
            "week_start": r.week_start,
            "register_units": r.register_units,
            "baseline_units": r.baseline_units,
        }
        for r in rows_qs.only(
            "store_key",
            "product_id",
            "week_start",
            "register_units",
            "baseline_units",
        )
    ]
    return compute_pos_halo_from_rows(demo_weeks_by_store, sales_rows)


def build_pitch_shelf_compliance(
    tenant_id: int,
    *,
    period_start: date,
    period_end: date,
    retailer_filter: str = "",
    market_filter: str = "",
) -> dict[str, Any]:
    """Buyer-safe OSA, price-tag compliance, facings, and shelf price snapshot."""
    from recaps.models import CustomRecap, CustomRecapFile
    from recaps.shelf_check import (
        NO,
        SHELF_CATEGORY_IREGEX,
        YES,
        _image_q,
        effective_answers,
        retail_recap_q,
        visit_answer,
    )
    from recaps.tenant_overview import (
        _approved_only,
        _filter_event_window,
        _inclusive_dates_to_window,
    )

    window = _inclusive_dates_to_window(period_start, period_end)
    recaps_qs = (
        _approved_only(
            _filter_event_window(
                CustomRecap.objects.filter(tenant_id=tenant_id).filter(retail_recap_q()),
                "event__",
                window,
            ),
            "",
        )
        .filter(archived_at__isnull=True)
        .select_related(
            "event",
            "event__retailer",
            "event__request__retailer",
            "event__location",
            "event__location__state",
        )
    )

    rf = (retailer_filter or "").strip().lower()
    mf = (market_filter or "").strip().lower()
    scoped = bool(rf or mf)

    recap_ids: list[int] = []
    for rec in recaps_qs:
        ev = getattr(rec, "event", None)
        if scoped:
            ret_name = ""
            if ev is not None:
                ret = getattr(ev, "retailer", None) or getattr(
                    getattr(ev, "request", None), "retailer", None
                )
                ret_name = (
                    (getattr(ret, "name", None) or "")
                    + " "
                    + (getattr(ev, "name", None) or "")
                    + " "
                    + (getattr(rec, "name", None) or "")
                ).lower()
                loc = getattr(ev, "location", None)
                loc_text = " ".join(
                    filter(
                        None,
                        [
                            getattr(ev, "address", None),
                            getattr(loc, "name", None) if loc else None,
                            getattr(getattr(loc, "state", None), "code", None)
                            if loc
                            else None,
                        ],
                    )
                ).lower()
            else:
                loc_text = ""
            if rf and rf not in ret_name:
                continue
            if mf and mf not in loc_text:
                continue
        recap_ids.append(rec.id)

    if not recap_ids:
        return {
            "available": False,
            "auditedVisits": 0,
            "unreadVisits": 0,
            "onShelfCount": 0,
            "notFoundCount": 0,
            "unclearCount": 0,
            "osaRatePct": None,
            "priceTagVisibleCount": 0,
            "priceTagRatePct": None,
            "avgFacings": None,
            "medianPrice": None,
            "oosGapsFlagged": 0,
            "scopedToFilter": scoped,
        }

    files = (
        CustomRecapFile.objects.filter(
            custom_recap_id__in=recap_ids,
            file_recap_category__name__iregex=SHELF_CATEGORY_IREGEX,
        )
        .filter(_image_q())
        .select_related("shelf_reading", "shelf_correction")
    )

    by_recap: dict[int, list[dict[str, Any] | None]] = {}
    facings_vals: list[int] = []
    price_vals: list[float] = []
    oos_gaps = 0

    for rf_obj in files:
        ans = effective_answers(
            getattr(rf_obj, "shelf_reading", None),
            getattr(rf_obj, "shelf_correction", None),
        )
        by_recap.setdefault(rf_obj.custom_recap_id, []).append(ans)
        if ans is not None:
            if ans.get("facings") is not None:
                try:
                    facings_vals.append(int(ans["facings"]))
                except (TypeError, ValueError):
                    pass
            if ans.get("price") is not None:
                try:
                    price_vals.append(float(ans["price"]))
                except (TypeError, ValueError):
                    pass
            if ans.get("out_of_stock_gap") == YES:
                oos_gaps += 1

    audited = 0
    on_shelf = 0
    not_found = 0
    unclear = 0
    price_tag = 0
    unread = 0

    for answers in by_recap.values():
        known = [a for a in answers if a is not None]
        if not known:
            unread += 1
            continue
        visit = visit_answer(known)
        audited += 1
        if visit["product_on_shelf"] == YES:
            on_shelf += 1
        elif visit["product_on_shelf"] == NO:
            not_found += 1
        else:
            unclear += 1
        if visit["price_tag_visible"] == YES:
            price_tag += 1

    if audited == 0:
        return {
            "available": False,
            "auditedVisits": 0,
            "unreadVisits": unread,
            "onShelfCount": 0,
            "notFoundCount": 0,
            "unclearCount": 0,
            "osaRatePct": None,
            "priceTagVisibleCount": 0,
            "priceTagRatePct": None,
            "avgFacings": None,
            "medianPrice": None,
            "oosGapsFlagged": 0,
            "scopedToFilter": scoped,
        }

    return {
        "available": True,
        "auditedVisits": audited,
        "unreadVisits": unread,
        "onShelfCount": on_shelf,
        "notFoundCount": not_found,
        "unclearCount": unclear,
        "osaRatePct": _round1((on_shelf / audited) * 100.0),
        "priceTagVisibleCount": price_tag,
        "priceTagRatePct": _round1((price_tag / audited) * 100.0),
        "avgFacings": (
            _round1(sum(facings_vals) / len(facings_vals)) if facings_vals else None
        ),
        "medianPrice": _round2_str(median(price_vals)) if price_vals else None,
        "oosGapsFlagged": oos_gaps,
        "scopedToFilter": scoped,
    }


def build_pitch_shopper_voice(
    tenant_id: int,
    *,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """Assemble AI-distilled shopper themes + PII-scrubbed verbatim shopper quotes.

    Uses cached ``TenantFeedbackThemes`` when present, otherwise gathers
    PII-scrubbed approved comments and stored ``RecapMentionTag`` themes so
    minting a deck never stalls or fails when AI is unavailable.
    """
    from recaps.feedback_themes import (
        STATUS_READY,
        _short_quote,
        gather_comments,
        source_hash,
    )
    from tenants.models import TenantFeedbackThemes

    try:
        comments, _truncated = gather_comments(tenant_id, period_start, period_end)
    except Exception:  # noqa: BLE001
        logger.exception("pitch_qbr_data: gather_comments failed tenant=%s", tenant_id)
        return {
            "available": False,
            "source": "none",
            "commentCount": 0,
            "recapCount": 0,
            "highlights": [],
            "themes": [],
            "quotes": [],
        }

    recap_count = len({c.recap for c in comments})
    if not comments:
        return {
            "available": False,
            "source": "none",
            "commentCount": 0,
            "recapCount": 0,
            "highlights": [],
            "themes": [],
            "quotes": [],
        }

    # Fast path: reuse stored AI-distilled themes if present for this exact window
    # (or latest stored themes overlapping this tenant).
    digest = source_hash(comments)
    stored = (
        TenantFeedbackThemes.objects.filter(
            tenant_id=tenant_id, source_hash=digest
        ).first()
    )
    if stored is not None and isinstance(stored.payload, dict):
        raw_themes = list(stored.payload.get("themes") or [])
        raw_sentences = list(stored.payload.get("sentences") or [])
        themes_out: list[dict[str, Any]] = []
        quotes_out: list[dict[str, Any]] = []
        seen_quotes: set[str] = set()
        for t in raw_themes[:MAX_VOICE_THEMES]:
            if not isinstance(t, dict):
                continue
            label = str(t.get("label") or "").strip()
            quote = str(t.get("quote") or "").strip()
            section = str(t.get("section") or "liked").strip()
            rcount = int(t.get("recap_count") or 0)
            if not label:
                continue
            themes_out.append(
                {
                    "section": section,
                    "label": label,
                    "recapCount": rcount,
                    "quote": quote,
                }
            )
            if quote and quote.lower() not in seen_quotes and len(quotes_out) < MAX_VOICE_QUOTES:
                seen_quotes.add(quote.lower())
                quotes_out.append(
                    {"section": section, "label": label, "quote": quote}
                )
        highlights = [
            {
                "section": str(s.get("section") or "liked"),
                "text": str(s.get("text") or "").strip(),
            }
            for s in raw_sentences
            if isinstance(s, dict) and str(s.get("text") or "").strip()
        ]
        if themes_out or quotes_out:
            return {
                "available": True,
                "source": STATUS_READY,
                "commentCount": len(comments),
                "recapCount": recap_count,
                "highlights": highlights,
                "themes": themes_out,
                "quotes": quotes_out,
            }

    # Deterministic fallback: combine stored RecapMentionTag themes + clean verbatim quotes
    themes_counter: Counter[str] = Counter()
    try:
        from consumer_mentions.models import RecapMentionTag

        recap_keys = {c.recap for c in comments if c.recap}
        if recap_keys:
            for tag in RecapMentionTag.objects.filter(
                tenant_id=tenant_id, recap_key__in=recap_keys
            ):
                for item in tag.themes or []:
                    if not isinstance(item, dict):
                        continue
                    label = str(item.get("label") or item.get("theme") or "").strip()
                    if label and label.lower() != "other":
                        themes_counter[label] += 1
    except Exception:  # noqa: BLE001
        logger.debug("pitch_qbr_data: mention tags unavailable", exc_info=True)

    section_for_kind = {
        "general": "liked",
        "decline": "passed",
        "question": "asked",
    }
    quotes_out = []
    seen_q: set[str] = set()
    # Prefer rich general/positive comments first, then questions & objections
    ordered = sorted(
        comments,
        key=lambda c: (
            0 if c.kind == "general" else (1 if c.kind == "question" else 2),
            -len(c.text),
        ),
    )
    for c in ordered:
        q = _short_quote(c.text)
        if not q or len(q) < 16 or q.lower() in seen_q:
            continue
        seen_q.add(q.lower())
        quotes_out.append(
            {
                "section": section_for_kind.get(c.kind, "liked"),
                "label": (
                    "Shopper feedback"
                    if c.kind == "general"
                    else ("Shopper question" if c.kind == "question" else "Objection noted")
                ),
                "quote": q,
            }
        )
        if len(quotes_out) >= MAX_VOICE_QUOTES:
            break

    themes_out = [
        {
            "section": "liked",
            "label": label,
            "recapCount": count,
            "quote": quotes_out[idx]["quote"] if idx < len(quotes_out) else "",
        }
        for idx, (label, count) in enumerate(
            themes_counter.most_common(MAX_VOICE_THEMES)
        )
    ]

    return {
        "available": bool(quotes_out or themes_out),
        "source": "quotes",
        "commentCount": len(comments),
        "recapCount": recap_count,
        "highlights": [],
        "themes": themes_out,
        "quotes": quotes_out,
    }
