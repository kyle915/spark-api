"""1-Click Shareable Buyer Pitch Decks service layer.

Builds a strictly tenant-scoped, buyer-safe snapshot from `/buyer-report` KPIs,
sampling lift, optional opted-in cost-per-unit-sold, retailer breakdown, top
store wins, and curated Live Photo Wall pins.

Security invariants:
- Non-staff users can only list, create, or revoke decks for their own
  authorized workspace tenant (requires consolidated brand access; creation and
  revocation require `can_edit_brand_ops`).
- Passcodes are hashed with Django's PBKDF2-SHA256 `make_password` and checked
  in constant time via `check_password` (`secrets.compare_digest`).
- Internal costs (`totalSupportedCost`, `costPerUnitSold`, `costPerSample`) are
  OFF by default (`include_costs=False`) and double-stripped on public output
  whenever `deck.include_costs` is False.
- Private internal notes, owner emails, agency/provider names, ambassador
  identities, and other tenants' data never enter the public payload.
"""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from brand_insights.deck_captions import enrich_deck_photos, overlay_snapshot_captions
from brand_insights.models import BuyerPitchDeck
from brand_insights.photo_wall import resolve_deck_photos
from pilot.actions import build_weekly_buyer_store_report
from pilot.costs import quantize_money
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from pilot.pos import build_retailer_pos_measurement
from tenants.models import Tenant

MAX_EXPIRY_DAYS = 365
MAX_TOP_STORE_WINS = 8


def hash_passcode(raw_passcode: str) -> str:
    clean = (raw_passcode or "").strip()
    if not clean:
        return ""
    if len(clean) < 4:
        raise ValueError("Passcode must be at least 4 characters.")
    if len(clean) > 64:
        raise ValueError("Passcode must be 64 characters or fewer.")
    return make_password(clean)


def verify_passcode(raw_passcode: str, stored_hash: str) -> bool:
    if not stored_hash:
        return True
    candidate = (raw_passcode or "").strip()
    if not candidate:
        return False
    return check_password(candidate, stored_hash)


def _mint_deck_token() -> str:
    for _ in range(12):
        candidate = f"pd_{secrets.token_urlsafe(24)}"
        if not BuyerPitchDeck.objects.filter(token=candidate).exists():
            return candidate
    raise RuntimeError("Could not mint unique BuyerPitchDeck token.")


def _tenant_brand_snapshot(tenant: Tenant) -> dict[str, Any]:
    manager = getattr(tenant, "themes", None)
    themes = list(manager.all()) if manager is not None else []
    theme = next(
        (t for t in themes if getattr(t, "color_scheme", None) == "light"), None
    )
    if theme is None:
        theme = next(
            (t for t in themes if getattr(t, "color_scheme", None) == "dark"), None
        )
    if theme is None and themes:
        theme = themes[0]
    css = (getattr(theme, "css_variables", None) or {}) if theme else {}
    if not isinstance(css, dict):
        css = {}
    return {
        "name": tenant.name,
        "slug": tenant.slug,
        "primary": css.get("--color-primary"),
        "cssVariables": css,
    }


def _safe_div_money(numerator: Decimal, denominator: int) -> str | None:
    if denominator <= 0:
        return None
    val = (numerator / Decimal(denominator)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return str(val)


def _build_deck_snapshot(
    access: ResolvedWorkspaceAccess,
    *,
    period_start: date | None,
    period_end: date | None,
    retailer_filter: str,
    market_filter: str,
    provider_uuid: str | None,
    include_costs: bool,
    photos: list[dict[str, Any]],
) -> tuple[date, date, dict[str, Any]]:
    report = build_weekly_buyer_store_report(
        access,
        period_start=period_start,
        period_end=period_end,
        retailer_filter=retailer_filter or None,
        market_filter=market_filter or None,
        provider_uuid=provider_uuid or None,
    )
    eff_start = date.fromisoformat(report["period_start"])
    eff_end = date.fromisoformat(report["period_end"])
    rows = report["rows"]

    pos_data = build_retailer_pos_measurement(
        access, period_start=eff_start, period_end=eff_end
    )

    store_keys_in_report = {r["store_key"] for r in rows}
    # When the deck is scoped (retailer / market / provider filters) and that
    # scope yields zero stores, never fall through to tenant-wide POS lift.
    scoped = bool(
        (retailer_filter or "").strip()
        or (market_filter or "").strip()
        or (provider_uuid or "").strip()
    )

    # Aggregate sampling lift strictly from eligible matched store-weeks in scope.
    total_lift = Decimal("0.00")
    total_baseline = Decimal("0.00")
    has_lift = False
    has_baseline = False
    matched_sku_weeks = 0
    eligible_sku_weeks = 0
    # Scoped + empty report → skip POS entirely (outer guard). Otherwise only
    # aggregate store-weeks whose store_key is in the (filtered) report.
    if pos_data["available"] and (store_keys_in_report or not scoped):
        for sw in pos_data["store_weeks"]:
            if store_keys_in_report and sw["store_key"] not in store_keys_in_report:
                continue
            cells = sw.get("sku_cells") or []
            eligible_sku_weeks += len(cells)
            for cell in cells:
                if cell.get("status") in ("matched", "explicit_zero"):
                    matched_sku_weeks += 1
                if cell.get("incremental_lift_units") is not None:
                    total_lift += Decimal(str(cell["incremental_lift_units"]))
                    has_lift = True
                if cell.get("baseline_units") is not None:
                    total_baseline += Decimal(str(cell["baseline_units"]))
                    has_baseline = True

    sampling_lift_pct: float | None = None
    if has_lift and has_baseline and total_baseline > Decimal("0.00"):
        pct = ((total_lift / total_baseline) * Decimal("100")).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
        sampling_lift_pct = float(pct)

    total_visits = int(report["total_visits"])
    stores_activated = sum(1 for r in rows if int(r["visit_count"]) > 0)
    total_samples = sum(int(r["samples_distributed"] or 0) for r in rows)
    total_rep_units = sum(int(r["rep_observed_units"] or 0) for r in rows)

    adj_values = [
        int(r["adjusted_register_units"])
        for r in rows
        if r.get("adjusted_register_units") is not None
    ]
    total_adj_units: int | None = sum(adj_values) if adj_values else None

    supported_cost_dec = Decimal(str(report["total_supported_cost"] or "0.00"))
    cost_per_unit: str | None = None
    cost_per_unit_basis: str | None = None
    cost_per_sample: str | None = None
    if include_costs:
        if total_adj_units is not None and total_adj_units > 0:
            cost_per_unit = _safe_div_money(supported_cost_dec, total_adj_units)
            cost_per_unit_basis = "adjusted_pos"
        elif total_rep_units > 0:
            cost_per_unit = _safe_div_money(supported_cost_dec, total_rep_units)
            cost_per_unit_basis = "rep_observed"
        if total_samples > 0:
            cost_per_sample = _safe_div_money(supported_cost_dec, total_samples)

    # Retailer banner rollup (buyer-safe, zero agency/internal info)
    retailer_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        banner = (r.get("retailer_banner") or r.get("store_label") or "Retailer").strip()
        bucket = retailer_map.setdefault(
            banner,
            {
                "retailerBanner": banner,
                "storesCount": 0,
                "visits": 0,
                "samplesDistributed": 0,
                "repObservedUnits": 0,
                "adjustedRegisterUnits": 0,
                "hasPos": False,
                "supportedCost": Decimal("0.00"),
            },
        )
        bucket["storesCount"] += 1
        bucket["visits"] += int(r["visit_count"] or 0)
        bucket["samplesDistributed"] += int(r["samples_distributed"] or 0)
        bucket["repObservedUnits"] += int(r["rep_observed_units"] or 0)
        if r.get("adjusted_register_units") is not None:
            bucket["adjustedRegisterUnits"] += int(r["adjusted_register_units"])
            bucket["hasPos"] = True
        bucket["supportedCost"] += Decimal(str(r.get("supported_brand_cost") or "0.00"))

    retailer_breakdown: list[dict[str, Any]] = []
    for b in sorted(
        retailer_map.values(),
        key=lambda x: (-x["visits"], -x["repObservedUnits"], x["retailerBanner"].lower()),
    ):
        adj_u = b["adjustedRegisterUnits"] if b["hasPos"] else None
        entry: dict[str, Any] = {
            "retailerBanner": b["retailerBanner"],
            "storesCount": b["storesCount"],
            "visits": b["visits"],
            "samplesDistributed": b["samplesDistributed"],
            "repObservedUnits": b["repObservedUnits"],
            "adjustedRegisterUnits": adj_u,
        }
        if include_costs:
            denom = adj_u if (adj_u is not None and adj_u > 0) else b["repObservedUnits"]
            entry["supportedCost"] = str(quantize_money(b["supportedCost"]))
            entry["costPerUnitSold"] = _safe_div_money(b["supportedCost"], denom)
        retailer_breakdown.append(entry)

    # Top store wins — ranked by POS/observed unit performance then visits
    ranked_stores = sorted(
        rows,
        key=lambda r: (
            -(
                int(r["adjusted_register_units"])
                if r.get("adjusted_register_units") is not None
                else int(r.get("rep_observed_units") or 0)
            ),
            -int(r.get("samples_distributed") or 0),
            -int(r.get("visit_count") or 0),
            (r.get("store_label") or "").lower(),
        ),
    )[:MAX_TOP_STORE_WINS]

    top_store_wins: list[dict[str, Any]] = []
    for r in ranked_stores:
        adj_u = r.get("adjusted_register_units")
        rep_u = int(r.get("rep_observed_units") or 0)
        win: dict[str, Any] = {
            "storeLabel": r["store_label"],
            "retailerBanner": r["retailer_banner"],
            "market": r.get("market") or "",
            "visits": int(r["visit_count"] or 0),
            "samplesDistributed": int(r["samples_distributed"] or 0),
            "repObservedUnits": rep_u,
            "adjustedRegisterUnits": int(adj_u) if adj_u is not None else None,
            "posCoverageLabel": r.get("pos_coverage_label") or "",
            "highlight": r.get("recommendation_summary"),
        }
        if include_costs:
            row_cost = Decimal(str(r.get("supported_brand_cost") or "0.00"))
            denom = int(adj_u) if (adj_u is not None and int(adj_u) > 0) else rep_u
            win["supportedCost"] = str(quantize_money(row_cost))
            win["costPerUnitSold"] = _safe_div_money(row_cost, denom)
        top_store_wins.append(win)

    if not pos_data["available"]:
        pos_cov_label = "POS feed not connected"
    elif scoped and not store_keys_in_report:
        pos_cov_label = "No stores in this deck's filter scope"
    elif eligible_sku_weeks > 0:
        cov_ratio = matched_sku_weeks / eligible_sku_weeks
        pos_cov_label = (
            f"{int(round(cov_ratio * 100))}% store-SKU-week POS coverage "
            f"({matched_sku_weeks}/{eligible_sku_weeks} matched in scope)"
        )
    else:
        cov = pos_data.get("coverage") or {}
        cov_ratio = cov.get("coverage_ratio")
        pos_cov_label = (
            f"{cov.get('matched_store_sku_weeks', 0)}/"
            f"{cov.get('eligible_store_sku_weeks', 0)} matched"
        )
        if cov_ratio is not None:
            pct_cov = int(round(float(cov_ratio) * 100))
            pos_cov_label = f"{pct_cov}% store-SKU-week POS coverage"

    kpis: dict[str, Any] = {
        "totalVisits": total_visits,
        "storesActivated": stores_activated,
        "totalSamplesDistributed": total_samples,
        "repObservedUnits": total_rep_units,
        "adjustedRegisterUnits": total_adj_units,
        "incrementalLiftUnits": (
            str(quantize_money(total_lift)) if has_lift else None
        ),
        "samplingLiftPct": sampling_lift_pct,
        "posAvailable": bool(pos_data["available"]),
        "posCoverageLabel": pos_cov_label,
        "includeCosts": include_costs,
    }
    if include_costs:
        kpis["totalSupportedCost"] = str(quantize_money(supported_cost_dec))
        kpis["costPerUnitSold"] = cost_per_unit
        kpis["costPerUnitBasis"] = cost_per_unit_basis
        kpis["costPerSample"] = cost_per_sample

    snapshot = {
        "brand": _tenant_brand_snapshot(access.tenant),
        "periodStart": eff_start.isoformat(),
        "periodEnd": eff_end.isoformat(),
        "retailerFilter": retailer_filter,
        "marketFilter": market_filter,
        "providerUuid": (provider_uuid or "").strip(),
        "kpis": kpis,
        "retailerBreakdown": retailer_breakdown,
        "topStoreWins": top_store_wins,
        "photos": photos,
    }
    return eff_start, eff_end, snapshot


@transaction.atomic
def create_buyer_pitch_deck(
    access: ResolvedWorkspaceAccess,
    *,
    title: str,
    subtitle: str = "",
    buyer_name: str = "",
    intro_note: str = "",
    period_start: date | None = None,
    period_end: date | None = None,
    retailer_filter: str = "",
    market_filter: str = "",
    provider_uuid: str | None = None,
    include_costs: bool = False,
    passcode: str | None = None,
    expires_in_days: int | None = None,
    pinned_photo_ids: list[str | int] | None = None,
) -> BuyerPitchDeck:
    """Create a snapshot-backed, revocable BuyerPitchDeck for the caller's tenant."""
    if not access.can_view_brand_consolidated or not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can create Buyer Pitch Decks."
        )

    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("Pitch deck title is required.")

    clean_subtitle = (subtitle or "").strip()[:280]
    clean_buyer = (buyer_name or "").strip()[:140]
    clean_intro = (intro_note or "").strip()[:1500]
    clean_retailer = (retailer_filter or "").strip()[:120]
    clean_market = (market_filter or "").strip()[:120]
    clean_provider = (provider_uuid or "").strip() or None

    pass_hash = hash_passcode(passcode) if passcode and passcode.strip() else ""

    expires_at = None
    if expires_in_days is not None:
        days = int(expires_in_days)
        if days < 1 or days > MAX_EXPIRY_DAYS:
            raise ValueError(
                f"expires_in_days must be between 1 and {MAX_EXPIRY_DAYS}."
            )
        expires_at = timezone.now() + timedelta(days=days)

    validated_photo_ids, photos = resolve_deck_photos(
        access.tenant.id,
        requested_photo_ids=pinned_photo_ids,
    )

    eff_start, eff_end, snapshot = _build_deck_snapshot(
        access,
        period_start=period_start,
        period_end=period_end,
        retailer_filter=clean_retailer,
        market_filter=clean_market,
        provider_uuid=clean_provider,
        include_costs=bool(include_costs),
        photos=photos,
    )

    # Sell-story captions from existing photo scores + mentions + shelf
    # (no new vision). Staff intro_note wins; otherwise we draft a leave-behind.
    enriched_photos, sell_story, sell_source = enrich_deck_photos(
        access.tenant.id,
        list(snapshot.get("photos") or []),
        brand_name=(snapshot.get("brand") or {}).get("name") or access.tenant.name,
        kpis=snapshot.get("kpis") or {},
        period_start=eff_start.isoformat(),
        period_end=eff_end.isoformat(),
        intro_note=clean_intro,
    )
    snapshot["photos"] = enriched_photos
    snapshot["sellStory"] = sell_story
    snapshot["sellStorySource"] = sell_source
    if not clean_intro and sell_story:
        clean_intro = sell_story[:1500]

    deck = BuyerPitchDeck.objects.create(
        tenant=access.tenant,
        token=_mint_deck_token(),
        title=clean_title[:180],
        subtitle=clean_subtitle,
        buyer_name=clean_buyer,
        intro_note=clean_intro,
        period_start=eff_start,
        period_end=eff_end,
        retailer_filter=clean_retailer,
        market_filter=clean_market,
        include_costs=bool(include_costs),
        passcode_hash=pass_hash,
        expires_at=expires_at,
        pinned_photo_ids=validated_photo_ids,
        snapshot_payload=snapshot,
        created_by=access.user,
    )

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="buyer_pitch_deck",
        action="pitch_deck_created",
        target_type="BuyerPitchDeck",
        target_id=str(deck.uuid),
        summary=(
            f"Created Buyer Pitch Deck '{deck.title}'"
            + (f" for {deck.buyer_name}" if deck.buyer_name else "")
            + f" ({len(validated_photo_ids)} photos, costs={'on' if deck.include_costs else 'off'})."
        ),
    )
    return deck


def list_buyer_pitch_decks(access: ResolvedWorkspaceAccess) -> list[BuyerPitchDeck]:
    if not access.can_view_brand_consolidated:
        raise PermissionError(
            "Outside providers cannot list brand Buyer Pitch Decks."
        )
    return list(
        BuyerPitchDeck.objects.select_related("created_by")
        .filter(tenant=access.tenant)
        .order_by("-created_at", "-id")[:50]
    )


@transaction.atomic
def revoke_buyer_pitch_deck(
    access: ResolvedWorkspaceAccess,
    *,
    deck_uuid: str,
) -> BuyerPitchDeck:
    if not access.can_view_brand_consolidated or not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can revoke Buyer Pitch Decks."
        )
    deck = (
        BuyerPitchDeck.objects.filter(
            tenant=access.tenant, uuid=(deck_uuid or "").strip()
        ).first()
    )
    if deck is None:
        raise ValueError("Buyer pitch deck not found.")
    if deck.revoked_at is None:
        deck.revoked_at = timezone.now()
        deck.revoked_by = access.user
        deck.save(update_fields=["revoked_at", "revoked_by", "updated_at"])
        log_workspace_audit(
            tenant=access.tenant,
            actor=access.user,
            category="buyer_pitch_deck",
            action="pitch_deck_revoked",
            target_type="BuyerPitchDeck",
            target_id=str(deck.uuid),
            summary=f"Revoked Buyer Pitch Deck '{deck.title}'.",
        )
    return deck


def _fresh_deck_photos(deck: BuyerPitchDeck) -> list[dict[str, Any]]:
    """Remint interactive media URLs from durable photo ids on every public
    read. Snapshot photo URLs are never trusted for serving — they may hold
    expired PRIVATE_MEDIA interactive tokens."""
    ids = list(deck.pinned_photo_ids or [])
    if not ids:
        return []
    try:
        _validated, photos = resolve_deck_photos(
            deck.tenant_id,
            requested_photo_ids=ids,
        )
        return photos
    except (PermissionError, ValueError):
        # Fall back to non-URL snapshot metadata only if remint fails hard.
        return [
            {k: v for k, v in dict(p).items() if k != "url"}
            for p in ((deck.snapshot_payload or {}).get("photos") or [])
        ]


def _sanitize_public_deck_payload(deck: BuyerPitchDeck) -> dict[str, Any]:
    """Serialize a read-only, buyer-safe payload strictly from this deck's
    snapshot. Double-strips cost fields when ``deck.include_costs`` is False.
    Photo URLs are reminted from ``pinned_photo_ids`` on every read."""
    snap = dict(deck.snapshot_payload or {})
    kpis = dict(snap.get("kpis") or {})
    retailer_breakdown = [dict(r) for r in (snap.get("retailerBreakdown") or [])]
    top_store_wins = [dict(s) for s in (snap.get("topStoreWins") or [])]
    photos = overlay_snapshot_captions(
        _fresh_deck_photos(deck),
        snap.get("photos") or [],
    )

    if not deck.include_costs:
        for cost_key in (
            "totalSupportedCost",
            "costPerUnitSold",
            "costPerUnitBasis",
            "costPerSample",
        ):
            kpis.pop(cost_key, None)
        kpis["includeCosts"] = False
        for r in retailer_breakdown:
            r.pop("supportedCost", None)
            r.pop("costPerUnitSold", None)
        for s in top_store_wins:
            s.pop("supportedCost", None)
            s.pop("costPerUnitSold", None)

    brand = dict(snap.get("brand") or {})
    brand.setdefault("name", deck.tenant.name)
    sell_story = (snap.get("sellStory") or "").strip() or (deck.intro_note or "")

    return {
        "title": deck.title,
        "subtitle": deck.subtitle,
        "buyerName": deck.buyer_name,
        "introNote": deck.intro_note or sell_story,
        "sellStory": sell_story,
        "sellStorySource": snap.get("sellStorySource") or "",
        "periodStart": deck.period_start.isoformat(),
        "periodEnd": deck.period_end.isoformat(),
        "retailerFilter": deck.retailer_filter,
        "marketFilter": deck.market_filter,
        "providerUuid": snap.get("providerUuid") or "",
        "includeCosts": deck.include_costs,
        "requiresPasscode": deck.has_passcode,
        "expiresAt": deck.expires_at.isoformat() if deck.expires_at else None,
        "createdAt": deck.created_at.isoformat(),
        "brand": brand,
        "kpis": kpis,
        "retailerBreakdown": retailer_breakdown,
        "topStoreWins": top_store_wins,
        "photos": photos,
    }


def resolve_public_pitch_deck(
    token: str,
) -> tuple[BuyerPitchDeck | None, str | None, int]:
    """Resolve a public pitch deck token and enforce revocation + expiry."""
    clean_token = (token or "").strip()
    if not clean_token:
        return None, "not_found", 404
    deck = (
        BuyerPitchDeck.objects.select_related("tenant")
        .filter(token=clean_token)
        .first()
    )
    if deck is None:
        return None, "not_found", 404
    if deck.revoked_at is not None:
        return None, "revoked", 403
    if deck.expires_at is not None and deck.expires_at <= timezone.now():
        return None, "expired", 410
    return deck, None, 200


def record_pitch_deck_view(deck: BuyerPitchDeck) -> None:
    BuyerPitchDeck.objects.filter(pk=deck.pk).update(
        view_count=F("view_count") + 1,
        last_viewed_at=timezone.now(),
    )


def locked_pitch_deck_preview(deck: BuyerPitchDeck) -> dict[str, Any]:
    """Minimal metadata for passcode-locked screen — reveals zero KPIs, stores,
    costs, or photos until unlocked."""
    snap_brand = (deck.snapshot_payload or {}).get("brand") or {}
    return {
        "title": deck.title,
        "subtitle": deck.subtitle,
        "buyerName": deck.buyer_name,
        "brandName": snap_brand.get("name") or deck.tenant.name,
        "requiresPasscode": True,
        "expiresAt": deck.expires_at.isoformat() if deck.expires_at else None,
    }


def unlocked_pitch_deck_payload(deck: BuyerPitchDeck) -> dict[str, Any]:
    record_pitch_deck_view(deck)
    return _sanitize_public_deck_payload(deck)
