"""Shelf Compliance, Pricing & Competitor Voice Hub service layer.

Read-only, strictly tenant-scoped aggregator over data Spark already persists:
- ``recaps.models.ShelfReading`` / ``ShelfReadingCorrection`` on approved retail
  recaps (staff notes and ambassador identities are never returned)
- ``recaps.models.CustomRecapFile`` shelf photos (freshly signed via
  ``recaps.photo_gallery._image_url``)
- ``recaps.models.CustomRecap.store_signals`` (stockout risks & competitor promos)
- ``recaps.comment_insights.get_competitor_mentions`` & PII-scrubbed comments
  joined to event state/market with current-vs-prior window trend comparison.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_STORES = 120
MAX_PHOTOS = 60
MAX_COMPETITOR_MARKETS = 30
MAX_COMPETITOR_PROMOS = 30


def _round1(val: float) -> float:
    return float(Decimal(str(val)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _round2_str(val: Decimal | float) -> str:
    return str(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def classify_price_drift_pct(
    price: float | None,
    baseline_price: float | None,
) -> float | None:
    """Return signed % delta between observed shelf price and baseline/MSRP."""
    if price is None or baseline_price is None or baseline_price <= 0:
        return None
    return _round1(((price - baseline_price) / baseline_price) * 100.0)


def classify_mention_trend(current: int, prior: int) -> str:
    """Classify competitor mention trajectory vs prior equal-length window."""
    if current <= 0:
        return "steady"
    if prior <= 0:
        return "new"
    if current > prior:
        return "rising"
    if current < prior:
        return "cooling"
    return "steady"


def compute_before_after_pairs(
    photos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair earliest & latest shelf photos per store when a store has 2+ audited shots."""
    by_store: dict[str, list[dict[str, Any]]] = {}
    for p in photos:
        key = p.get("store_key") or p.get("store_name") or ""
        if not key:
            continue
        by_store.setdefault(key, []).append(p)

    pairs: list[dict[str, Any]] = []
    for _key, items in by_store.items():
        if len(items) < 2:
            continue
        ordered = sorted(
            items,
            key=lambda x: (x.get("event_date") or "", str(x.get("file_id") or "")),
        )
        earliest = ordered[0]
        latest = ordered[-1]
        if earliest.get("file_id") == latest.get("file_id"):
            continue
        pairs.append(
            {
                "store_key": latest.get("store_key") or "",
                "store_name": latest.get("store_name") or "Store",
                "market": latest.get("market") or "",
                "state_code": latest.get("state_code") or "",
                "before_photo": earliest,
                "after_photo": latest,
                "photo_count": len(ordered),
            }
        )
    pairs.sort(
        key=lambda item: (
            item["after_photo"].get("event_date") or "",
            item["photo_count"],
        ),
        reverse=True,
    )
    return pairs[:12]


def _extract_event_geo(
    ev,
    recap,
    *,
    header: dict[str, Any],
    aliases: dict[str, str],
) -> tuple[str, str, str, str]:
    from events.market import parse_address_geo
    from events.retailer_aliases import resolve_store_name

    req = getattr(ev, "request", None) if ev else None
    raw_name = (
        header.get("name")
        or getattr(getattr(ev, "retailer", None), "name", None)
        or getattr(getattr(req, "retailer", None), "name", None)
        or getattr(req, "retailer_name", None)
        or getattr(getattr(recap, "retailer", None), "name", None)
        or getattr(recap, "name", None)
        or ""
    ).strip()
    store_name = resolve_store_name(raw_name, aliases) if raw_name else "Retail Store"

    address = (
        header.get("address")
        or (getattr(ev, "address", None) or "")
        or (getattr(req, "address", None) or "")
    ).strip()
    geo = parse_address_geo(address) if address else None
    loc = getattr(ev, "location", None) if ev else None
    city = (
        header.get("city")
        or (getattr(loc, "name", None) if loc else "")
        or (geo.city if geo else "")
        or ""
    ).strip()
    state_code = (
        header.get("state")
        or getattr(getattr(loc, "state", None), "code", None)
        or getattr(getattr(ev, "state", None), "code", None)
        or getattr(getattr(recap, "state", None), "code", None)
        or (geo.state_code if geo else "")
        or ""
    ).strip().upper()
    market = (
        f"{city}, {state_code}"
        if city and state_code
        else (city or state_code or "Unknown market")
    )
    return store_name, market, state_code, address


def _event_day_iso(ev, recap) -> str:
    ev_dt = (
        getattr(ev, "date", None)
        or getattr(ev, "start_time", None)
        or getattr(recap, "submitted_at", None)
        or getattr(recap, "created_at", None)
    )
    if ev_dt is None:
        return ""
    day = ev_dt.date() if hasattr(ev_dt, "date") else ev_dt
    return day.isoformat() if isinstance(day, date) else str(day)[:10]


def build_shelf_voice_hub(
    tenant_id: int,
    start: date | None,
    end: date | None,
) -> dict[str, Any]:
    """Assemble full Shelf Compliance, Pricing & Competitor Voice Hub payload."""
    from events.retailer_aliases import accepted_alias_map
    from recaps.comment_insights import (
        brand_key,
        normalise,
        written_in,
    )
    from recaps.feedback_themes import _short_quote, gather_comments
    from recaps.models import CustomRecap, CustomRecapFile, Recap
    from recaps.photo_gallery import _image_url
    from recaps.shelf_check import (
        NO,
        SHELF_CATEGORY_IREGEX,
        YES,
        _image_q,
        effective_answers,
        retail_recap_q,
        visit_answer,
    )
    from recaps.store_places import store_header, tenant_places
    from recaps.store_signals import normalize_store_signals
    from recaps.tenant_overview import (
        _approved_only,
        _filter_event_window,
        _inclusive_dates_to_window,
    )
    from tenants.models import TenantCommentInsight

    is_all_time = start is None and end is None
    if is_all_time:
        eff_start: date | None = None
        eff_end: date | None = None
        today = timezone.now().date()
        trend_cur_start = today - timedelta(days=89)
        trend_cur_end = today
        prior_end = trend_cur_start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=89)
    else:
        eff_end = end or timezone.now().date()
        eff_start = start or (eff_end - timedelta(days=89))
        window_days = max(1, (eff_end - eff_start).days + 1)
        trend_cur_start = eff_start
        trend_cur_end = eff_end
        prior_end = eff_start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=window_days - 1)

    window = _inclusive_dates_to_window(eff_start, eff_end)
    recaps = list(
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
            "event__request",
            "event__request__retailer",
            "event__location",
            "event__location__state",
            "event__state",
            "retailer",
            "state",
        )
        .order_by("-event__date", "-id")
    )

    places = tenant_places(tenant_id)
    aliases = accepted_alias_map(tenant_id)
    event_ids = {int(r.event_id) for r in recaps if r.event_id}
    event_store_map = places.for_event_ids(event_ids) if event_ids else {}

    recap_meta: dict[int, dict[str, Any]] = {}
    legacy_meta: dict[int, dict[str, Any]] = {}
    header_by_skey: dict[str, dict[str, Any]] = {}
    competitor_promos: list[dict[str, Any]] = []
    stockout_by_store: dict[str, str] = {}
    promo_by_store: dict[str, str] = {}

    def _cached_header(skey: str | None) -> dict[str, Any]:
        if not skey:
            return {}
        if skey not in header_by_skey:
            header_by_skey[skey] = store_header(tenant_id, places, skey)
        return header_by_skey[skey]

    for rec in recaps:
        ev = getattr(rec, "event", None)
        skey = event_store_map.get(int(rec.event_id)) if rec.event_id else None
        header = _cached_header(skey)
        store_name, market, state_code, _addr = _extract_event_geo(
            ev, rec, header=header, aliases=aliases
        )
        ev_day = _event_day_iso(ev, rec)
        norm_key = skey or f"{store_name.casefold()}|{state_code}"

        recap_meta[rec.id] = {
            "recap_uuid": str(rec.uuid),
            "store_key": skey or "",
            "norm_key": norm_key,
            "store_name": store_name,
            "market": market,
            "state_code": state_code,
            "event_date": ev_day,
        }

        if isinstance(rec.store_signals, dict):
            sig = normalize_store_signals(rec.store_signals)
            if sig["stockout_risk"]["flagged"] and sig["stockout_risk"]["detail"]:
                stockout_by_store.setdefault(norm_key, sig["stockout_risk"]["detail"])
            if sig["competitor_promo"]["flagged"] and (
                sig["competitor_promo"]["competitor"]
                or sig["competitor_promo"]["detail"]
            ):
                comp_name = sig["competitor_promo"]["competitor"] or "Competitor"
                detail = sig["competitor_promo"]["detail"] or comp_name
                promo_by_store.setdefault(norm_key, f"{comp_name}: {detail}" if comp_name not in detail else detail)
                if len(competitor_promos) < MAX_COMPETITOR_PROMOS:
                    competitor_promos.append(
                        {
                            "store_key": skey or "",
                            "store_name": store_name,
                            "market": market,
                            "state_code": state_code,
                            "event_date": ev_day,
                            "recap_uuid": str(rec.uuid),
                            "competitor_brand": comp_name,
                            "detail": detail,
                        }
                    )

    # ── Shelf readings + audited photos ─────────────────────────────────
    recap_ids = list(recap_meta.keys())
    by_recap_answers: dict[int, list[dict[str, Any] | None]] = {}
    shelf_photos_out: list[dict[str, Any]] = []
    pending_shelf_photos: list[dict[str, Any]] = []
    all_facings: list[int] = []
    all_prices: list[float] = []

    if recap_ids:
        files = (
            CustomRecapFile.objects.filter(
                custom_recap_id__in=recap_ids,
                file_recap_category__name__iregex=SHELF_CATEGORY_IREGEX,
            )
            .filter(_image_q())
            .select_related(
                "shelf_reading",
                "shelf_correction",
                "file_recap_category",
            )
            .order_by("-id")
        )
        for rf in files:
            meta = recap_meta.get(rf.custom_recap_id)
            if meta is None:
                continue
            ans = effective_answers(
                getattr(rf, "shelf_reading", None),
                getattr(rf, "shelf_correction", None),
            )
            by_recap_answers.setdefault(rf.custom_recap_id, []).append(ans)
            if ans is None:
                if len(pending_shelf_photos) < MAX_PHOTOS:
                    url = _image_url(rf)
                    if url:
                        pending_shelf_photos.append(
                            {
                                "file_id": str(rf.id),
                                "photo_url": url,
                                "bucket_name": (
                                    getattr(rf.file_recap_category, "name", None) or "Shelf"
                                ),
                                "store_key": meta["store_key"],
                                "store_name": meta["store_name"],
                                "market": meta["market"],
                                "state_code": meta["state_code"],
                                "event_date": meta["event_date"],
                                "recap_uuid": meta["recap_uuid"],
                                "product_on_shelf": "pending",
                                "facings": None,
                                "price_tag_visible": "unclear",
                                "price": None,
                                "out_of_stock_gap": "unclear",
                            }
                        )
                continue

            facings = ans.get("facings")
            if facings is not None:
                try:
                    all_facings.append(int(facings))
                except (TypeError, ValueError):
                    facings = None
            price_raw = ans.get("price")
            price_float: float | None = None
            price_str: str | None = None
            if price_raw is not None:
                try:
                    price_float = float(price_raw)
                    all_prices.append(price_float)
                    price_str = _round2_str(price_float)
                except (TypeError, ValueError):
                    price_float = None

            url = _image_url(rf)
            if url and len(shelf_photos_out) < MAX_PHOTOS:
                shelf_photos_out.append(
                    {
                        "file_id": str(rf.id),
                        "photo_url": url,
                        "bucket_name": (
                            getattr(rf.file_recap_category, "name", None) or "Shelf"
                        ),
                        "store_key": meta["store_key"],
                        "store_name": meta["store_name"],
                        "market": meta["market"],
                        "state_code": meta["state_code"],
                        "event_date": meta["event_date"],
                        "recap_uuid": meta["recap_uuid"],
                        "product_on_shelf": ans["product_on_shelf"],
                        "facings": int(facings) if facings is not None else None,
                        "price_tag_visible": ans["price_tag_visible"],
                        "price": price_str,
                        "out_of_stock_gap": ans["out_of_stock_gap"],
                    }
                )

    if len(shelf_photos_out) < MAX_PHOTOS and pending_shelf_photos:
        shelf_photos_out.extend(
            pending_shelf_photos[: MAX_PHOTOS - len(shelf_photos_out)]
        )

    median_price_float = median(all_prices) if all_prices else None
    median_price_str = (
        _round2_str(median_price_float) if median_price_float is not None else None
    )
    min_price_str = _round2_str(min(all_prices)) if all_prices else None
    max_price_str = _round2_str(max(all_prices)) if all_prices else None
    avg_facings = (
        _round1(sum(all_facings) / len(all_facings)) if all_facings else None
    )

    audited_visits = 0
    unread_visits = 0
    on_shelf_count = 0
    not_found_count = 0
    unclear_count = 0
    price_tag_visible_count = 0
    missing_tag_count = 0
    oos_gaps_count = 0

    store_buckets: dict[str, dict[str, Any]] = {}
    photo_count_by_store: Counter[str] = Counter(
        p["store_key"] or p["store_name"] for p in shelf_photos_out
    )

    for rec_id, answers in by_recap_answers.items():
        meta = recap_meta.get(rec_id)
        if meta is None:
            continue
        known = [a for a in answers if a is not None]
        if not known:
            unread_visits += 1
            continue

        visit = visit_answer(known)
        audited_visits += 1

        is_on_shelf = visit["product_on_shelf"] == YES
        is_not_found = visit["product_on_shelf"] == NO
        is_tag_visible = visit["price_tag_visible"] == YES
        is_tag_missing = visit["price_tag_visible"] == NO
        has_oos_gap = any(a.get("out_of_stock_gap") == YES for a in known)

        if is_on_shelf:
            on_shelf_count += 1
        elif is_not_found:
            not_found_count += 1
        else:
            unclear_count += 1

        if is_tag_visible:
            price_tag_visible_count += 1
        elif is_tag_missing:
            missing_tag_count += 1
        if has_oos_gap:
            oos_gaps_count += 1

        visit_facings = [
            int(a["facings"]) for a in known if a.get("facings") is not None
        ]
        visit_prices = [
            float(a["price"]) for a in known if a.get("price") is not None
        ]

        nkey = meta["norm_key"]
        sb = store_buckets.setdefault(
            nkey,
            {
                "store_key": meta["store_key"],
                "store_name": meta["store_name"],
                "market": meta["market"],
                "state_code": meta["state_code"],
                "visit_count": 0,
                "latest_date": "",
                "latest_recap_uuid": meta["recap_uuid"],
                "latest_status": "unclear",
                "on_shelf_visits": 0,
                "not_found_visits": 0,
                "missing_tag_visits": 0,
                "oos_gap_visits": 0,
                "facings_list": [],
                "latest_facings": None,
                "prices_list": [],
                "latest_price": None,
            },
        )
        sb["visit_count"] += 1
        if is_on_shelf:
            sb["on_shelf_visits"] += 1
        elif is_not_found:
            sb["not_found_visits"] += 1
        if is_tag_missing:
            sb["missing_tag_visits"] += 1
        if has_oos_gap:
            sb["oos_gap_visits"] += 1
        sb["facings_list"].extend(visit_facings)
        sb["prices_list"].extend(visit_prices)

        if meta["event_date"] >= sb["latest_date"]:
            sb["latest_date"] = meta["event_date"]
            sb["latest_recap_uuid"] = meta["recap_uuid"]
            sb["latest_status"] = (
                "on_shelf"
                if is_on_shelf
                else ("not_found" if is_not_found else "unclear")
            )
            if visit_facings:
                sb["latest_facings"] = visit_facings[-1]
            if visit_prices:
                sb["latest_price"] = visit_prices[-1]

    stores_out: list[dict[str, Any]] = []
    for nkey, sb in store_buckets.items():
        vcount = sb["visit_count"]
        osa_pct = _round1((sb["on_shelf_visits"] / vcount) * 100.0) if vcount else None
        latest_price = sb["latest_price"]
        if latest_price is None and sb["prices_list"]:
            latest_price = sb["prices_list"][0]
        drift_pct = classify_price_drift_pct(latest_price, median_price_float)
        avg_f = (
            _round1(sum(sb["facings_list"]) / len(sb["facings_list"]))
            if sb["facings_list"]
            else None
        )
        photo_lookup_key = sb["store_key"] or sb["store_name"]
        stores_out.append(
            {
                "store_key": sb["store_key"],
                "store_name": sb["store_name"],
                "market": sb["market"],
                "state_code": sb["state_code"],
                "visit_count": vcount,
                "latest_date": sb["latest_date"],
                "latest_recap_uuid": sb["latest_recap_uuid"],
                "latest_status": sb["latest_status"],
                "on_shelf_visits": sb["on_shelf_visits"],
                "not_found_visits": sb["not_found_visits"],
                "osa_rate_pct": osa_pct,
                "missing_tag_visits": sb["missing_tag_visits"],
                "oos_gap_visits": sb["oos_gap_visits"],
                "latest_facings": sb["latest_facings"],
                "avg_facings": avg_f,
                "latest_price": _round2_str(latest_price) if latest_price is not None else None,
                "price_drift_pct": drift_pct,
                "stockout_signal": stockout_by_store.get(nkey, ""),
                "competitor_promo": promo_by_store.get(nkey, ""),
                "has_before_after": photo_count_by_store.get(photo_lookup_key, 0) >= 2,
            }
        )

    # Sort attention-first: not found -> OOS gap -> missing tag -> largest price drift -> newest
    stores_out.sort(
        key=lambda r: (
            -(1 if r["not_found_visits"] > 0 else 0),
            -(1 if r["oos_gap_visits"] > 0 else 0),
            -(1 if r["missing_tag_visits"] > 0 else 0),
            -(abs(r["price_drift_pct"]) if r["price_drift_pct"] is not None else 0.0),
            r["store_name"].lower(),
        )
    )

    before_after_pairs = compute_before_after_pairs(shelf_photos_out)

    # ── Rising competitor mentions by market (current vs prior window) ──
    competitor_markets_out: list[dict[str, Any]] = []
    try:
        # Read already-stored competitor brand list for this tenant (zero LLM wait)
        known_brands: list[str] = []
        seen_bkeys: set[str] = set()
        for row in (
            TenantCommentInsight.objects.filter(
                tenant_id=tenant_id, kind="competitors"
            )
            .order_by("-generated_at")[:5]
        ):
            payload = row.payload if isinstance(row.payload, dict) else {}
            for item in payload.get("brands") or []:
                if not isinstance(item, dict):
                    continue
                bname = str(item.get("brand") or "").strip()
                if not bname:
                    continue
                bk = brand_key(normalise(bname))
                if bk and bk not in seen_bkeys:
                    seen_bkeys.add(bk)
                    known_brands.append(bname)

        # Also include competitor brands spotted in store_signals
        for promo in competitor_promos:
            cb = (promo.get("competitor_brand") or "").strip()
            if cb and cb.lower() != "competitor":
                bk = brand_key(normalise(cb))
                if bk and bk not in seen_bkeys:
                    seen_bkeys.add(bk)
                    known_brands.append(cb)

        if known_brands:
            cur_comments, _ = gather_comments(tenant_id, eff_start, eff_end)
            if is_all_time:
                trend_cur_comments, _ = gather_comments(
                    tenant_id, trend_cur_start, trend_cur_end
                )
            else:
                trend_cur_comments = cur_comments
            prior_comments, _ = gather_comments(tenant_id, prior_start, prior_end)

            # Resolve market/state for any CustomRecap or legacy Recap keys not in recap_meta
            extra_cids: set[int] = set()
            extra_lids: set[int] = set()
            for c in cur_comments + trend_cur_comments + prior_comments:
                if c.recap.startswith("C"):
                    try:
                        cid = int(c.recap[1:])
                    except ValueError:
                        continue
                    if cid not in recap_meta:
                        extra_cids.add(cid)
                elif c.recap.startswith("L"):
                    try:
                        lid = int(c.recap[1:])
                    except ValueError:
                        continue
                    if lid not in legacy_meta:
                        extra_lids.add(lid)

            if extra_cids:
                extra_recs = list(
                    CustomRecap.objects.filter(
                        tenant_id=tenant_id, id__in=extra_cids
                    ).select_related(
                        "event",
                        "event__retailer",
                        "event__request",
                        "event__request__retailer",
                        "event__location",
                        "event__location__state",
                        "event__state",
                        "retailer",
                        "state",
                    )
                )
                extra_eids = {int(r.event_id) for r in extra_recs if r.event_id}
                extra_smap = places.for_event_ids(extra_eids) if extra_eids else {}
                for rec in extra_recs:
                    ev = getattr(rec, "event", None)
                    skey = extra_smap.get(int(rec.event_id)) if rec.event_id else None
                    header = _cached_header(skey)
                    sn, mk, sc, _ = _extract_event_geo(
                        ev, rec, header=header, aliases=aliases
                    )
                    recap_meta[rec.id] = {
                        "recap_uuid": str(rec.uuid),
                        "store_key": skey or "",
                        "norm_key": skey or f"{sn.casefold()}|{sc}",
                        "store_name": sn,
                        "market": mk,
                        "state_code": sc,
                        "event_date": _event_day_iso(ev, rec),
                    }

            if extra_lids:
                extra_legacy = list(
                    Recap.objects.filter(
                        event__tenant_id=tenant_id, id__in=extra_lids
                    ).select_related(
                        "event",
                        "event__retailer",
                        "event__request",
                        "event__request__retailer",
                        "event__location",
                        "event__location__state",
                        "event__state",
                        "retailer",
                        "state",
                    )
                )
                leg_eids = {int(r.event_id) for r in extra_legacy if r.event_id}
                leg_smap = places.for_event_ids(leg_eids) if leg_eids else {}
                for rec in extra_legacy:
                    ev = getattr(rec, "event", None)
                    skey = leg_smap.get(int(rec.event_id)) if rec.event_id else None
                    header = _cached_header(skey)
                    sn, mk, sc, _ = _extract_event_geo(
                        ev, rec, header=header, aliases=aliases
                    )
                    legacy_meta[rec.id] = {
                        "recap_uuid": str(rec.uuid),
                        "store_key": skey or "",
                        "norm_key": skey or f"{sn.casefold()}|{sc}",
                        "store_name": sn,
                        "market": mk,
                        "state_code": sc,
                        "event_date": _event_day_iso(ev, rec),
                    }

            brand_norms = [(b, normalise(b)) for b in known_brands if normalise(b)]

            def _tally_by_market(comments_list):
                counts: dict[tuple[str, str], dict[str, Any]] = {}
                seen_recap_brand: set[tuple[str, str]] = set()
                for c in comments_list:
                    cnorm = normalise(c.text)
                    meta = None
                    if c.recap.startswith("C"):
                        try:
                            meta = recap_meta.get(int(c.recap[1:]))
                        except ValueError:
                            meta = None
                    elif c.recap.startswith("L"):
                        try:
                            meta = legacy_meta.get(int(c.recap[1:]))
                        except ValueError:
                            meta = None
                    market_label = (
                        meta["market"] if meta else "General / Unmapped"
                    )
                    st_code = meta["state_code"] if meta else ""
                    for bdisplay, bnorm in brand_norms:
                        if not written_in(cnorm, bnorm):
                            continue
                        rk = (c.recap, bdisplay)
                        if rk in seen_recap_brand:
                            continue
                        seen_recap_brand.add(rk)
                        mkey = (market_label, bdisplay)
                        bucket = counts.setdefault(
                            mkey,
                            {
                                "market": market_label,
                                "state_code": st_code,
                                "competitor_brand": bdisplay,
                                "mention_count": 0,
                                "sample_quote": "",
                                "sample_store_key": meta["store_key"] if meta else "",
                                "sample_store_name": meta["store_name"] if meta else "",
                                "sample_recap_uuid": meta["recap_uuid"] if meta else "",
                            },
                        )
                        bucket["mention_count"] += 1
                        if not bucket["sample_quote"]:
                            bucket["sample_quote"] = _short_quote(c.text)
                return counts

            cur_counts = _tally_by_market(cur_comments)
            trend_cur_counts = (
                _tally_by_market(trend_cur_comments) if is_all_time else cur_counts
            )
            prior_counts = _tally_by_market(prior_comments)

            for mkey, cur in cur_counts.items():
                prior_n = prior_counts.get(mkey, {}).get("mention_count", 0)
                if is_all_time:
                    recent_n = trend_cur_counts.get(mkey, {}).get("mention_count", 0)
                    delta = recent_n - prior_n
                    trend = (
                        classify_mention_trend(recent_n, prior_n)
                        if (recent_n > 0 or prior_n > 0)
                        else "steady"
                    )
                else:
                    cur_n = cur["mention_count"]
                    delta = cur_n - prior_n
                    trend = classify_mention_trend(cur_n, prior_n)
                competitor_markets_out.append(
                    {
                        **cur,
                        "prior_mention_count": prior_n,
                        "delta": delta,
                        "trend": trend,
                    }
                )

            competitor_markets_out.sort(
                key=lambda r: (
                    -(1 if r["trend"] in ("rising", "new") else 0),
                    -r["delta"],
                    -r["mention_count"],
                    r["market"].lower(),
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("shelf_voice_hub: competitor market tally failed tenant=%s", tenant_id)

    has_any = bool(
        audited_visits > 0
        or unread_visits > 0
        or shelf_photos_out
        or competitor_markets_out
        or competitor_promos
    )
    return {
        "status": "ready" if has_any else "none",
        "start_date": eff_start.isoformat() if eff_start else "",
        "end_date": eff_end.isoformat() if eff_end else "",
        "audited_visits": audited_visits,
        "unread_visits": unread_visits,
        "on_shelf_count": on_shelf_count,
        "not_found_count": not_found_count,
        "unclear_count": unclear_count,
        "osa_rate_pct": (
            _round1((on_shelf_count / audited_visits) * 100.0)
            if audited_visits
            else None
        ),
        "price_tag_visible_count": price_tag_visible_count,
        "missing_tag_count": missing_tag_count,
        "price_tag_rate_pct": (
            _round1((price_tag_visible_count / audited_visits) * 100.0)
            if audited_visits
            else None
        ),
        "oos_gaps_count": oos_gaps_count,
        "avg_facings": avg_facings,
        "median_price": median_price_str,
        "min_price": min_price_str,
        "max_price": max_price_str,
        "priced_visits_count": len(all_prices),
        "stores": stores_out[:MAX_STORES],
        "shelf_photos": shelf_photos_out,
        "before_after_pairs": before_after_pairs,
        "competitor_markets": competitor_markets_out[:MAX_COMPETITOR_MARKETS],
        "competitor_promos": competitor_promos[:MAX_COMPETITOR_PROMOS],
    }
