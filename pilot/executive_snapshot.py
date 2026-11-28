"""Automated 1st-of-the-Month Executive Snapshot brief + PDF generator.

Produces a concise, buyer-safe, tenant-isolated monthly executive snapshot:
1. Headline KPIs vs. prior complete calendar month (events, recaps, consumers
   sampled, units sold, conversion % — with exact MoM deltas).
2. Top 5 converting doors (store, market, sampled, sold, conversion %).
3. Shelf OSA & MSRP price-drift callouts (OOS stores, missing price tags,
   and >5% price-drift callouts).
4. Top 3 shopper feedback themes (from cached themes or approved comments).
5. Up to 4 best approved field photos (never receipts, zero BA PII).

Strictly client-safe: approved recaps only, zero ambassador pay/PII, zero
internal Ignite operating figures.
"""

from __future__ import annotations

import base64
import calendar
import html as _html
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from django.utils import timezone

from pilot.models import ExecutiveSnapshotConfig
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from tenants.models import Tenant, parse_recipient_emails

logger = logging.getLogger(__name__)

_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(val: str) -> str:
    return _SLUG_RE.sub("-", (val or "brand").lower()).strip("-")[:48] or "brand"


def _esc(val: Any) -> str:
    if val in (None, ""):
        return "—"
    return _html.escape(str(val))


def _round1(val: float) -> float:
    return float(Decimal(str(val)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def resolve_snapshot_month(
    year: int | None = None,
    month: int | None = None,
    *,
    today: date | None = None,
) -> tuple[int, int]:
    """Return ``(year, month)`` — defaults to the prior complete calendar month."""
    if year is not None and month is not None:
        y = int(year)
        m = int(month)
        if not (2020 <= y <= 2100 and 1 <= m <= 12):
            raise ValueError("Specify a valid year and month (1-12).")
        return y, m
    ref = today or timezone.localdate()
    if ref.month == 1:
        return ref.year - 1, 12
    return ref.year, ref.month - 1


def prior_month_of(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def month_bounds_inclusive(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def month_label(year: int, month: int) -> str:
    name = _MONTH_NAMES[month] if 1 <= month <= 12 else str(month)
    return f"{name} {year}"


def compute_conversion_pct(units_sold: int, sampled: int) -> float | None:
    if sampled <= 0:
        return None
    return _round1((units_sold / sampled) * 100.0)


def compute_mom_delta(
    current: float | int | None,
    prior: float | int | None,
    *,
    is_percent_points: bool = False,
) -> dict[str, Any]:
    """Compute a clean month-over-month delta descriptor."""
    if current is None:
        return {"delta": None, "pct": None, "direction": "flat", "label": "—"}
    cur_f = float(current)
    if prior is None:
        return {"delta": None, "pct": None, "direction": "flat", "label": "New"}
    pri_f = float(prior)
    diff = cur_f - pri_f
    if abs(diff) < 1e-9:
        return {
            "delta": 0.0,
            "pct": 0.0,
            "direction": "flat",
            "label": "Flat vs. prior",
        }
    direction = "up" if diff > 0 else "down"
    sign = "+" if diff > 0 else ""
    if is_percent_points:
        pp = _round1(diff)
        return {
            "delta": pp,
            "pct": pp,
            "direction": direction,
            "label": f"{sign}{pp:.1f} pp",
        }
    if pri_f <= 0:
        return {
            "delta": _round1(diff),
            "pct": None,
            "direction": direction,
            "label": f"{sign}{int(round(diff)):,}",
        }
    pct = _round1((diff / pri_f) * 100.0)
    return {
        "delta": _round1(diff),
        "pct": pct,
        "direction": direction,
        "label": f"{sign}{pct:.1f}%",
    }


def rank_top_converting_doors(
    rows: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Sort aggregated door dicts by conversion % (with volume tie-breakers)."""
    ranked: list[dict[str, Any]] = []
    for row in rows:
        sampled = int(row.get("consumers_sampled") or 0)
        sold = int(row.get("units_sold") or 0)
        recaps = int(row.get("recaps") or 0)
        if sampled <= 0 and sold <= 0:
            continue
        conv = compute_conversion_pct(sold, sampled)
        ranked.append(
            {
                "store_name": str(row.get("store_name") or "Retail Store"),
                "market": str(row.get("market") or ""),
                "state_code": str(row.get("state_code") or ""),
                "recaps": recaps,
                "consumers_sampled": sampled,
                "units_sold": sold,
                "conversion_pct": conv,
                "conversion_label": f"{conv:.1f}%" if conv is not None else "—",
            }
        )
    ranked.sort(
        key=lambda r: (
            1 if (r["units_sold"] > 0 and r["consumers_sampled"] >= 5) else 0,
            r["conversion_pct"] if r["conversion_pct"] is not None else -1.0,
            r["units_sold"],
            r["consumers_sampled"],
        ),
        reverse=True,
    )
    return ranked[: max(1, limit)]


def _parse_optional_msrp(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    cleaned = str(value).strip().lstrip("$").replace(",", "")
    if not cleaned:
        return None
    try:
        dec = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Target MSRP must be a positive dollar amount like 4.99.") from exc
    if dec <= 0 or dec > Decimal("9999.99"):
        raise ValueError("Target MSRP must be between $0.01 and $9,999.99.")
    return dec


def _units_sold_from_kpis(kpis) -> int:
    products = int(getattr(kpis, "products_sold", 0) or 0)
    cans = int(getattr(kpis, "cans_sold", 0) or 0)
    packs = int(getattr(kpis, "packs_sold", 0) or 0)
    return products if products > 0 else (cans + packs)


def _sampled_from_kpis(kpis) -> int:
    samples = int(getattr(kpis, "samples_distributed", 0) or 0)
    consumers = int(getattr(kpis, "consumers_reached", 0) or 0)
    return consumers if consumers > 0 else samples


def _top_converting_doors_for_window(
    tenant_id: int,
    start: date,
    end: date,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    from events.market import parse_address_geo
    from events.retailer_aliases import accepted_alias_map, resolve_store_name
    from recaps.models import CustomFieldValue, CustomRecap, CustomRecapProductSample
    from recaps.tenant_overview import (
        _approved_only,
        _filter_event_window,
        _inclusive_dates_to_window,
    )
    from recaps.types import _consumers_sampled_from_fields, _sold_units_from_fields

    window = _inclusive_dates_to_window(start, end)
    recaps = list(
        _approved_only(
            _filter_event_window(
                CustomRecap.objects.filter(tenant_id=tenant_id, archived_at__isnull=True),
                "event__",
                window,
            ),
            "",
        ).select_related(
            "event",
            "event__retailer",
            "event__state",
            "event__location",
            "event__location__state",
            "event__request",
            "event__request__retailer",
        )[:400]
    )
    if not recaps:
        return []

    recap_ids = [r.id for r in recaps]
    field_map: dict[int, list[tuple[str, str]]] = {}
    for row in CustomFieldValue.objects.filter(custom_recap_id__in=recap_ids).values(
        "custom_recap_id", "custom_field__name", "value"
    ):
        field_map.setdefault(row["custom_recap_id"], []).append(
            (row["custom_field__name"] or "", str(row["value"] or ""))
        )

    sample_totals: dict[int, tuple[int, int]] = {}
    for row in CustomRecapProductSample.objects.filter(
        custom_recap_id__in=recap_ids
    ).values("custom_recap_id", "samples_given", "units_sold"):
        rid = row["custom_recap_id"]
        prev_s, prev_u = sample_totals.get(rid, (0, 0))
        sample_totals[rid] = (
            prev_s + int(row["samples_given"] or 0),
            prev_u + int(row["units_sold"] or 0),
        )

    aliases = accepted_alias_map(tenant_id)
    by_store: dict[str, dict[str, Any]] = {}
    for recap in recaps:
        ev = getattr(recap, "event", None)
        req = getattr(ev, "request", None) if ev else None
        raw_name = (
            getattr(getattr(ev, "retailer", None), "name", None)
            or getattr(getattr(req, "retailer", None), "name", None)
            or getattr(req, "retailer_name", None)
            or getattr(ev, "name", None)
            or recap.name
            or ""
        ).strip()
        store_name = resolve_store_name(raw_name, aliases) if raw_name else "Retail Store"
        address = (
            (getattr(ev, "address", None) or "")
            or (getattr(req, "address", None) or "")
        ).strip()
        geo = parse_address_geo(address) if address else None
        loc = getattr(ev, "location", None) if ev else None
        city = ((getattr(loc, "name", None) if loc else "") or (geo.city if geo else "") or "").strip()
        state_code = (
            getattr(getattr(loc, "state", None), "code", None)
            or getattr(getattr(ev, "state", None), "code", None)
            or (geo.state_code if geo else "")
            or ""
        ).strip().upper()
        market = f"{city}, {state_code}" if city and state_code else (city or state_code or "")

        pairs = field_map.get(recap.id, [])
        sampled_from_fields = int(_consumers_sampled_from_fields(pairs) or 0)
        sold_from_fields = int(_sold_units_from_fields(pairs) or 0)
        prod_samples, prod_sold = sample_totals.get(recap.id, (0, 0))
        sampled = max(sampled_from_fields, prod_samples)
        sold = max(sold_from_fields, prod_sold)

        key = f"{store_name.lower()}|{state_code}"
        bucket = by_store.setdefault(
            key,
            {
                "store_name": store_name,
                "market": market,
                "state_code": state_code,
                "recaps": 0,
                "consumers_sampled": 0,
                "units_sold": 0,
            },
        )
        bucket["recaps"] += 1
        bucket["consumers_sampled"] += sampled
        bucket["units_sold"] += sold

    return rank_top_converting_doors(list(by_store.values()), limit=limit)


def _shelf_callouts_for_window(
    tenant_id: int,
    start: date,
    end: date,
    *,
    target_msrp: Decimal | None = None,
) -> dict[str, Any]:
    from recaps.shelf_voice_hub import build_shelf_voice_hub, classify_price_drift_pct

    try:
        hub = build_shelf_voice_hub(tenant_id, start, end)
    except Exception:  # noqa: BLE001
        logger.debug("shelf_voice_hub unavailable for snapshot tenant=%s", tenant_id)
        return {
            "audited_visits": 0,
            "osa_rate_pct": None,
            "price_tag_rate_pct": None,
            "oos_gaps_count": 0,
            "missing_tag_count": 0,
            "median_price": None,
            "target_msrp": f"{target_msrp:.2f}" if target_msrp is not None else None,
            "callouts": [],
        }

    baseline_float: float | None = None
    if target_msrp is not None:
        baseline_float = float(target_msrp)
    elif hub.get("median_price"):
        try:
            baseline_float = float(hub["median_price"])
        except (TypeError, ValueError):
            baseline_float = None

    callouts: list[dict[str, Any]] = []
    for st in hub.get("stores") or []:
        store_name = str(st.get("store_name") or "Retail Store")
        market = str(st.get("market") or "")
        reasons: list[str] = []
        severity = "info"
        if int(st.get("not_found_visits") or 0) > 0 or int(st.get("oos_gap_visits") or 0) > 0:
            reasons.append("Out-of-stock gap logged")
            severity = "alert"
        if int(st.get("missing_tag_visits") or 0) > 0:
            reasons.append("Missing shelf price tag")
            if severity != "alert":
                severity = "warn"
        latest_price = st.get("latest_price")
        if latest_price and baseline_float:
            try:
                p_val = float(latest_price)
                drift = classify_price_drift_pct(p_val, baseline_float)
                if drift is not None and abs(drift) >= 5.0:
                    sign = "+" if drift > 0 else ""
                    ref_lbl = f"${target_msrp:.2f} MSRP" if target_msrp is not None else f"${baseline_float:.2f} median"
                    reasons.append(f"Shelf ${p_val:.2f} ({sign}{drift:.1f}% vs. {ref_lbl})")
                    if severity != "alert":
                        severity = "warn"
            except (TypeError, ValueError):
                pass
        if reasons:
            callouts.append(
                {
                    "store_name": store_name,
                    "market": market,
                    "severity": severity,
                    "detail": " · ".join(reasons),
                }
            )

    # Sort alerts before warnings, cap to 6 concise callouts
    callouts.sort(key=lambda c: (0 if c["severity"] == "alert" else 1, c["store_name"]))
    return {
        "audited_visits": int(hub.get("audited_visits") or 0),
        "osa_rate_pct": hub.get("osa_rate_pct"),
        "price_tag_rate_pct": hub.get("price_tag_rate_pct"),
        "oos_gaps_count": int(hub.get("oos_gaps_count") or 0),
        "missing_tag_count": int(hub.get("missing_tag_count") or 0),
        "median_price": hub.get("median_price"),
        "target_msrp": f"{target_msrp:.2f}" if target_msrp is not None else None,
        "callouts": callouts[:6],
    }


def _top_shopper_themes_for_window(
    tenant_id: int,
    start: date,
    end: date,
    *,
    allow_ai: bool = False,
) -> list[dict[str, Any]]:
    from recaps.feedback_themes import gather_comments, get_feedback_themes, source_hash
    from tenants.models import TenantFeedbackThemes

    try:
        comments, _truncated = gather_comments(tenant_id, start, end)
    except Exception:  # noqa: BLE001
        comments = []

    themes_out: list[dict[str, Any]] = []
    if comments:
        digest = source_hash(comments)
        cached = TenantFeedbackThemes.objects.filter(
            tenant_id=tenant_id, source_hash=digest
        ).first()
        payload_themes = None
        if cached is not None and isinstance(cached.payload, dict):
            payload_themes = cached.payload.get("themes")
        elif allow_ai:
            try:
                res = get_feedback_themes(tenant_id, start, end, refresh=False)
                if res.get("status") == "ready":
                    payload_themes = res.get("themes")
            except Exception:  # noqa: BLE001
                payload_themes = None

        if payload_themes:
            for item in list(payload_themes)[:3]:
                title = str(item.get("name") or item.get("title") or "").strip()
                if not title:
                    continue
                mentions = int(item.get("count") or item.get("mentions") or 0)
                sentiment = str(item.get("sentiment") or "neutral").strip().lower()
                quotes = item.get("quotes") or []
                sample_quote = ""
                if quotes:
                    q0 = quotes[0]
                    sample_quote = (
                        str(q0.get("text") or q0.get("quote") or "")
                        if isinstance(q0, dict)
                        else str(q0 or "")
                    ).strip()[:180]
                themes_out.append(
                    {
                        "title": title,
                        "mentions": mentions,
                        "sentiment": sentiment,
                        "summary": sample_quote,
                    }
                )
        if not themes_out:
            # Deterministic fallback: surface top 3 recent PII-scrubbed shopper quotes
            for idx, c in enumerate(comments[:3], start=1):
                quote_txt = str(getattr(c, "text", "") or "").strip()[:180]
                if not quote_txt:
                    continue
                store = str(getattr(c, "store", "") or "").strip()
                themes_out.append(
                    {
                        "title": store or f"Shopper note #{idx}",
                        "mentions": 1,
                        "sentiment": "neutral",
                        "summary": f"“{quote_txt}”",
                    }
                )
    return themes_out[:3]


def _best_field_photos_for_window(
    tenant_id: int,
    start: date,
    end: date,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    from recaps.photo_gallery import tenant_photo_gallery

    try:
        picks = tenant_photo_gallery(
            tenant_id,
            start=start,
            end=end,
            picks_only=True,
            first=limit,
        ).get("photos") or []
        if len(picks) < limit:
            seen_ids = {p["id"] for p in picks}
            fallback = tenant_photo_gallery(
                tenant_id,
                start=start,
                end=end,
                picks_only=False,
                first=limit + 4,
            ).get("photos") or []
            for item in fallback:
                if item["id"] not in seen_ids:
                    picks.append(item)
                    seen_ids.add(item["id"])
                if len(picks) >= limit:
                    break
        return [
            {
                "id": str(p.get("id") or ""),
                "url": str(p.get("url") or ""),
                "store": str(p.get("store") or "Field activation"),
                "state": str(p.get("state") or ""),
                "event_date": str(p.get("event_date") or ""),
                "category": str(p.get("category") or ""),
                "caption": str(p.get("caption") or ""),
            }
            for p in picks[:limit]
            if p.get("url")
        ]
    except Exception:  # noqa: BLE001
        logger.debug("photo_gallery unavailable for snapshot tenant=%s", tenant_id)
        return []


def get_or_create_executive_snapshot_config(tenant: Tenant) -> ExecutiveSnapshotConfig:
    cfg, _ = ExecutiveSnapshotConfig.objects.get_or_create(tenant=tenant)
    return cfg


def resolve_snapshot_recipients(
    tenant: Tenant,
    config: ExecutiveSnapshotConfig,
    *,
    override_email: str = "",
) -> list[str]:
    if (override_email or "").strip():
        return parse_recipient_emails(override_email)
    configured = parse_recipient_emails(config.recipient_emails or "")
    if configured:
        return configured
    # Fallback to tenant's existing client report recipients if configured
    if hasattr(tenant, "scheduled_report_recipients"):
        return list(tenant.scheduled_report_recipients())
    return []


def update_executive_snapshot_config(
    access: ResolvedWorkspaceAccess,
    *,
    enabled: bool | None = None,
    recipient_emails: str | None = None,
    target_msrp: str | None = None,
    include_photos: bool | None = None,
) -> ExecutiveSnapshotConfig:
    if not (access.is_brand_admin or access.is_ignite_admin or access.can_manage_pos):
        raise PermissionError(
            "Only brand administrators can update Executive Snapshot delivery settings."
        )
    cfg = get_or_create_executive_snapshot_config(access.tenant)
    if enabled is not None:
        cfg.enabled = bool(enabled)
    if recipient_emails is not None:
        parsed = parse_recipient_emails(recipient_emails)
        cfg.recipient_emails = ", ".join(parsed)
    if target_msrp is not None:
        cfg.target_msrp = _parse_optional_msrp(target_msrp)
    if include_photos is not None:
        cfg.include_photos = bool(include_photos)
    cfg.updated_by = access.user if getattr(access.user, "pk", None) else None
    cfg.save()
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="reporting",
        action="executive_snapshot.updated",
        summary=(
            f"{'Enabled' if cfg.enabled else 'Paused'} 1st-of-month Executive Snapshot"
            + (f" ({cfg.recipient_emails})" if cfg.recipient_emails else "")
        ),
        metadata={"enabled": cfg.enabled, "include_photos": cfg.include_photos},
    )
    return cfg


def build_executive_snapshot_data(
    tenant: Tenant,
    *,
    year: int | None = None,
    month: int | None = None,
    target_msrp: Decimal | None = None,
    include_photos: bool = True,
    allow_ai_themes: bool = False,
) -> dict[str, Any]:
    """Assemble the complete monthly Executive Snapshot data structure."""
    from recaps.tenant_overview import (
        _add_months,
        _month_start,
        _tenant_event_recap_counts_window,
        _tenant_kpi_totals_window,
    )

    y, m = resolve_snapshot_month(year, month)
    py, pm = prior_month_of(y, m)
    cur_window = (_month_start(y, m), _month_start(*_add_months(y, m, 1)))
    pri_window = (_month_start(py, pm), _month_start(*_add_months(py, pm, 1)))

    cur_events, cur_recaps = _tenant_event_recap_counts_window(tenant.id, cur_window)
    pri_events, pri_recaps = _tenant_event_recap_counts_window(tenant.id, pri_window)
    cur_kpis = _tenant_kpi_totals_window(tenant.id, cur_window)
    pri_kpis = _tenant_kpi_totals_window(tenant.id, pri_window)

    cur_sampled = _sampled_from_kpis(cur_kpis)
    pri_sampled = _sampled_from_kpis(pri_kpis)
    cur_sold = _units_sold_from_kpis(cur_kpis)
    pri_sold = _units_sold_from_kpis(pri_kpis)
    cur_conv = compute_conversion_pct(cur_sold, cur_sampled)
    pri_conv = compute_conversion_pct(pri_sold, pri_sampled)

    def _kpi_row(key: str, label: str, cur_v, pri_v, fmt: str, is_pp: bool = False) -> dict[str, Any]:
        d = compute_mom_delta(cur_v, pri_v, is_percent_points=is_pp)
        return {
            "key": key,
            "label": label,
            "value": float(cur_v) if cur_v is not None else None,
            "prior_value": float(pri_v) if pri_v is not None else None,
            "formatted_value": fmt,
            "delta_label": d["label"],
            "direction": d["direction"],
        }

    kpis = [
        _kpi_row("activations", "Activations", cur_events, pri_events, f"{cur_events:,}"),
        _kpi_row("recaps", "Approved Recaps", cur_recaps, pri_recaps, f"{cur_recaps:,}"),
        _kpi_row("sampled", "Consumers Sampled", cur_sampled, pri_sampled, f"{cur_sampled:,}"),
        _kpi_row("units_sold", "Units Sold", cur_sold, pri_sold, f"{cur_sold:,}"),
        _kpi_row(
            "conversion",
            "Sample → Sale Conv.",
            cur_conv,
            pri_conv,
            f"{cur_conv:.1f}%" if cur_conv is not None else "—",
            is_pp=True,
        ),
    ]

    start_d, end_d = month_bounds_inclusive(y, m)
    top_doors = _top_converting_doors_for_window(tenant.id, start_d, end_d, limit=5)
    shelf = _shelf_callouts_for_window(
        tenant.id, start_d, end_d, target_msrp=target_msrp
    )
    themes = _top_shopper_themes_for_window(
        tenant.id, start_d, end_d, allow_ai=allow_ai_themes
    )
    photos = (
        _best_field_photos_for_window(tenant.id, start_d, end_d, limit=4)
        if include_photos
        else []
    )

    return {
        "tenant_id": tenant.id,
        "tenant_name": tenant.name or "Brand",
        "year": y,
        "month": m,
        "month_key": f"{y:04d}-{m:02d}",
        "month_label": month_label(y, m),
        "prior_month_label": month_label(py, pm),
        "kpis": kpis,
        "top_doors": top_doors,
        "shelf": shelf,
        "themes": themes,
        "photos": photos,
        "has_activity": bool(
            cur_events > 0
            or cur_recaps > 0
            or cur_sampled > 0
            or shelf.get("audited_visits", 0) > 0
        ),
    }


_SNAPSHOT_PDF_CSS = """
@page { size: letter; margin: 0.55in 0.6in; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
  color: #14160f;
  background: #ffffff;
  font-size: 11px;
  line-height: 1.45;
  margin: 0;
}
.hdr { border-bottom: 3px solid #c5f546; padding-bottom: 10px; margin-bottom: 16px; }
.eyebrow { font-size: 9px; text-transform: uppercase; letter-spacing: 0.14em; color: #6b705c; margin: 0 0 4px; }
h1 { font-size: 24px; margin: 0; letter-spacing: -0.02em; color: #14160f; }
.sub { font-size: 10px; color: #6b705c; margin: 4px 0 0; }
.section { margin-bottom: 16px; page-break-inside: avoid; }
h2 { font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: #5c6152; margin: 0 0 8px; }
.kpis { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
.kpi { background: #f7f6f1; border: 1px solid #e5e4dc; border-top: 3px solid #c5f546; border-radius: 8px; padding: 9px 10px; }
.kpi .lbl { font-size: 8px; text-transform: uppercase; letter-spacing: 0.06em; color: #6b705c; }
.kpi .val { font-size: 17px; font-weight: 700; color: #14160f; margin: 3px 0 2px; }
.kpi .delta { font-size: 9px; color: #464a3e; }
.kpi .delta.up { color: #2b6b1d; font-weight: 600; }
.kpi .delta.down { color: #a8340f; font-weight: 600; }
table { width: 100%; border-collapse: collapse; border: 1px solid #e5e4dc; border-radius: 6px; overflow: hidden; }
th { background: #f5f4ee; text-align: left; font-size: 8.5px; text-transform: uppercase; letter-spacing: 0.06em; color: #6b705c; padding: 6px 8px; border-bottom: 1px solid #e5e4dc; }
td { padding: 6px 8px; border-bottom: 1px solid #f0efe8; color: #14160f; font-size: 10px; }
.num { text-align: right; }
.muted { color: #6b705c; font-size: 9px; }
.callout { background: #fbfaf6; border-left: 3px solid #d97706; padding: 6px 10px; margin-bottom: 6px; border-radius: 0 6px 6px 0; }
.callout.alert { border-left-color: #c2410c; background: #fff7f4; }
.theme { background: #f7f6f1; border: 1px solid #e5e4dc; border-radius: 8px; padding: 8px 10px; margin-bottom: 6px; }
.photos { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.photo-card { border: 1px solid #e5e4dc; border-radius: 8px; overflow: hidden; background: #faf9f5; }
.photo-card img { width: 100%; height: 96px; object-fit: cover; display: block; }
.photo-cap { padding: 5px 6px; font-size: 8.5px; color: #464a3e; }
"""


def render_executive_snapshot_html(data: dict[str, Any]) -> str:
    """Render self-contained HTML for the Executive Snapshot PDF."""
    kpi_cards = "".join(
        f"<div class='kpi'>"
        f"<div class='lbl'>{_esc(k.get('label'))}</div>"
        f"<div class='val'>{_esc(k.get('formatted_value'))}</div>"
        f"<div class='delta {_esc(k.get('direction', 'flat'))}'>{_esc(k.get('delta_label'))}</div>"
        f"</div>"
        for k in data.get("kpis") or []
    )

    doors = data.get("top_doors") or []
    if doors:
        door_rows = "".join(
            f"<tr>"
            f"<td><strong>{_esc(d.get('store_name'))}</strong>"
            f"<div class='muted'>{_esc(d.get('market'))}</div></td>"
            f"<td class='num'>{int(d.get('recaps') or 0)}</td>"
            f"<td class='num'>{int(d.get('consumers_sampled') or 0):,}</td>"
            f"<td class='num'>{int(d.get('units_sold') or 0):,}</td>"
            f"<td class='num'><strong>{_esc(d.get('conversion_label'))}</strong></td>"
            f"</tr>"
            for d in doors
        )
        doors_html = (
            "<table><thead><tr>"
            "<th>Store</th><th class='num'>Recaps</th><th class='num'>Sampled</th>"
            "<th class='num'>Units Sold</th><th class='num'>Conversion</th>"
            f"</tr></thead><tbody>{door_rows}</tbody></table>"
        )
    else:
        doors_html = "<p class='muted'>No approved retail sampling conversions recorded this month.</p>"

    shelf = data.get("shelf") or {}
    shelf_stats = []
    if shelf.get("osa_rate_pct") is not None:
        shelf_stats.append(f"On-Shelf Availability: <strong>{shelf['osa_rate_pct']}%</strong>")
    if shelf.get("price_tag_rate_pct") is not None:
        shelf_stats.append(f"Price-Tag Visibility: <strong>{shelf['price_tag_rate_pct']}%</strong>")
    if shelf.get("median_price"):
        shelf_stats.append(f"Median Shelf Price: <strong>${_esc(shelf['median_price'])}</strong>")
    if shelf.get("target_msrp"):
        shelf_stats.append(f"Target MSRP: <strong>${_esc(shelf['target_msrp'])}</strong>")
    callouts = shelf.get("callouts") or []
    callouts_html = "".join(
        f"<div class='callout {_esc(c.get('severity', 'warn'))}'>"
        f"<strong>{_esc(c.get('store_name'))}</strong> "
        f"<span class='muted'>({_esc(c.get('market'))})</span> — {_esc(c.get('detail'))}"
        f"</div>"
        for c in callouts
    )
    shelf_html = (
        f"<p style='margin:0 0 8px'>{' &nbsp;·&nbsp; '.join(shelf_stats)}</p>{callouts_html}"
        if (shelf_stats or callouts_html)
        else "<p class='muted'>No shelf audits completed during this period.</p>"
    )

    themes = data.get("themes") or []
    themes_html = (
        "".join(
            f"<div class='theme'><strong>{_esc(t.get('title'))}</strong>"
            + (f" <span class='muted'>({int(t.get('mentions') or 0)} mentions)</span>" if int(t.get("mentions") or 0) > 1 else "")
            + (f"<div style='margin-top:3px;color:#3d4136'>{_esc(t.get('summary'))}</div>" if t.get("summary") else "")
            + "</div>"
            for t in themes
        )
        if themes
        else "<p class='muted'>Not enough shopper comments in this window to summarize themes.</p>"
    )

    photos = data.get("photos") or []
    if photos:
        photo_cards = "".join(
            f"<div class='photo-card'>"
            f"<img src='{_esc(p.get('url'))}' alt='{_esc(p.get('store'))}' />"
            f"<div class='photo-cap'><strong>{_esc(p.get('store'))}</strong>"
            f"<br/>{_esc(p.get('event_date'))}</div>"
            f"</div>"
            for p in photos[:4]
        )
        photos_section = f"<div class='section'><h2>Featured Field Photos</h2><div class='photos'>{photo_cards}</div></div>"
    else:
        photos_section = ""

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{_esc(data.get('tenant_name'))} — {_esc(data.get('month_label'))} Executive Snapshot</title>
</head>
<body>
  <div class="hdr">
    <p class="eyebrow">{_esc(data.get('tenant_name'))} · Monthly Executive Snapshot</p>
    <h1>{_esc(data.get('month_label'))}</h1>
    <p class="sub">Month-over-month comparison vs. {_esc(data.get('prior_month_label'))} · Approved field recaps only</p>
  </div>

  <div class="section">
    <h2>Headline KPIs</h2>
    <div class="kpis">{kpi_cards}</div>
  </div>

  <div class="section">
    <h2>Top 5 Converting Doors</h2>
    {doors_html}
  </div>

  <div class="section">
    <h2>Shelf Compliance &amp; Price-Drift Callouts</h2>
    {shelf_html}
  </div>

  <div class="section">
    <h2>Top Shopper Themes</h2>
    {themes_html}
  </div>

  {photos_section}
</body>
</html>"""


def render_executive_snapshot_pdf(data: dict[str, Any]) -> bytes:
    """Render Executive Snapshot HTML into PDF bytes via WeasyPrint (lazy import)."""
    html_doc = render_executive_snapshot_html(data)
    try:
        from weasyprint import CSS, HTML
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("PDF renderer is unavailable on this server.") from exc
    return HTML(string=html_doc).write_pdf(stylesheets=[CSS(string=_SNAPSHOT_PDF_CSS)])


def export_executive_snapshot_pdf_b64(
    tenant: Tenant,
    *,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    cfg = get_or_create_executive_snapshot_config(tenant)
    data = build_executive_snapshot_data(
        tenant,
        year=year,
        month=month,
        target_msrp=cfg.target_msrp,
        include_photos=cfg.include_photos,
    )
    pdf_bytes = render_executive_snapshot_pdf(data)
    filename = f"{_slug(tenant.name)}-executive-snapshot-{data['month_key']}.pdf"
    return {
        "filename": filename,
        "month_label": data["month_label"],
        "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
    }


def send_executive_snapshot_email(
    tenant: Tenant,
    *,
    year: int | None = None,
    month: int | None = None,
    override_email: str = "",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate and email the Executive Snapshot brief + PDF attachment."""
    from pilot.envelopes import ExecutiveSnapshotMailer

    cfg = get_or_create_executive_snapshot_config(tenant)
    recipients = resolve_snapshot_recipients(tenant, cfg, override_email=override_email)
    y, m = resolve_snapshot_month(year, month)
    month_key = f"{y:04d}-{m:02d}"

    if not recipients:
        return {"status": "skipped_no_recipients", "recipients": [], "month_key": month_key}

    if not force and not override_email and cfg.last_sent_month == month_key:
        return {"status": "skipped_already_sent", "recipients": recipients, "month_key": month_key}

    data = build_executive_snapshot_data(
        tenant,
        year=y,
        month=m,
        target_msrp=cfg.target_msrp,
        include_photos=cfg.include_photos,
    )

    if not data["has_activity"] and not force:
        return {"status": "skipped_empty", "recipients": recipients, "month_key": month_key}

    if dry_run:
        return {
            "status": "dry_run",
            "recipients": recipients,
            "month_key": month_key,
            "month_label": data["month_label"],
        }

    pdf_bytes: bytes | None = None
    try:
        pdf_bytes = render_executive_snapshot_pdf(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Executive Snapshot PDF unavailable for tenant=%s: %s", tenant.id, exc)

    filename = f"{_slug(tenant.name)}-executive-snapshot-{month_key}.pdf"
    mailer = ExecutiveSnapshotMailer(
        recipients=recipients,
        tenant_name=tenant.name or "Brand",
        month_label=data["month_label"],
        prior_month_label=data["prior_month_label"],
        kpis=data["kpis"],
        top_doors=data["top_doors"],
        shelf_summary=data["shelf"],
        themes=data["themes"],
        photos=data["photos"],
        pdf_bytes=pdf_bytes,
        pdf_filename=filename,
    )
    sent = mailer.send_now()
    now = timezone.now()
    if sent is True:
        if not override_email:
            cfg.last_sent_at = now
            cfg.last_sent_month = month_key
            cfg.last_send_status = "sent"
            cfg.last_send_error = ""
            cfg.save(
                update_fields=[
                    "last_sent_at",
                    "last_sent_month",
                    "last_send_status",
                    "last_send_error",
                    "updated_at",
                ]
            )
        return {
            "status": "sent",
            "recipients": recipients,
            "month_key": month_key,
            "month_label": data["month_label"],
            "pdf_attached": pdf_bytes is not None,
        }

    if not override_email:
        cfg.last_send_status = "failed"
        cfg.last_send_error = "Mail provider did not accept delivery."
        cfg.save(update_fields=["last_send_status", "last_send_error", "updated_at"])
    return {
        "status": "failed",
        "recipients": recipients,
        "month_key": month_key,
        "month_label": data["month_label"],
        "pdf_attached": pdf_bytes is not None,
    }


def run_monthly_executive_snapshots(
    *,
    dry_run: bool = False,
    force: bool = False,
    tenant_id: int | None = None,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    """Run the 1st-of-month Executive Snapshot job across all opted-in active brands."""
    qs = ExecutiveSnapshotConfig.objects.select_related("tenant").filter(
        enabled=True,
        tenant__in=Tenant.active(),
    )
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)

    summary: dict[str, Any] = {
        "checked": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "details": [],
    }
    for cfg in qs.iterator():
        summary["checked"] += 1
        res = send_executive_snapshot_email(
            cfg.tenant,
            year=year,
            month=month,
            force=force,
            dry_run=dry_run,
        )
        st = res.get("status", "failed")
        if st in {"sent", "dry_run"}:
            summary["sent"] += 1
        elif st.startswith("skipped"):
            summary["skipped"] += 1
        else:
            summary["failed"] += 1
        summary["details"].append(
            {
                "tenant_id": cfg.tenant_id,
                "tenant_name": cfg.tenant.name,
                **res,
            }
        )
    return summary
