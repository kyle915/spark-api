"""Live In-Store Photo Wall service layer.

Filterable visual gallery of approved in-store demo photos by retailer,
market, SKU, category, and date range, plus a tenant-scoped "Pin to Buyer
Pitch Deck" toggle that feeds directly into shareable Buyer Pitch Decks.

Strict multi-tenant isolation:
- Non-staff callers are bound to their authorized workspace tenant via
  ``ResolvedWorkspaceAccess``.
- Only photos attached to approved, non-archived recaps of that tenant are
  eligible; receipt / paperwork categories are always excluded.
- Provider-scoped users see only photos from their assigned events and
  cannot mutate brand-wide pitch deck pins.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db.models import Prefetch, Q

from brand_insights.models import PhotoWallPin
from events.models import Event
from events.retailer_aliases import split_store_number
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from recaps.models import (
    CustomRecapFile,
    CustomRecapProductSample,
    CustomRecapSalePerformance,
)
from recaps.photo_gallery import MAX_PAGE, _IMAGE_SUFFIXES, _image_url
from recaps.photo_picks import _related
from recaps.tenant_overview import (
    _approved_only,
    _filter_event_window,
    _inclusive_dates_to_window,
)
from recaps.types import _PAPERWORK_CATEGORY_RE

MAX_PINNED_PHOTOS_PER_DECK = 24


def _parse_file_id(raw_id: str | int | None) -> int | None:
    if raw_id is None:
        return None
    text = str(raw_id).strip()
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    return int(text) if text.isdigit() else None


def _base_photo_wall_queryset(
    tenant_id: int,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    access: ResolvedWorkspaceAccess | None = None,
):
    qs = CustomRecapFile.objects.filter(
        custom_recap__tenant_id=tenant_id,
        custom_recap__archived_at__isnull=True,
    )
    qs = _approved_only(qs, "custom_recap__")
    qs = qs.exclude(
        file_recap_category__name__iregex=_PAPERWORK_CATEGORY_RE.pattern
    )
    if start_date is not None or end_date is not None:
        qs = _filter_event_window(
            qs,
            "custom_recap__event__",
            _inclusive_dates_to_window(start_date, end_date),
        )
    if access is not None and not access.can_view_brand_consolidated:
        allowed_event_ids = Event.objects.filter(
            access.allowed_events_q()
        ).values_list("id", flat=True)
        qs = qs.filter(custom_recap__event_id__in=allowed_event_ids)

    image_q = Q()
    for suffix in _IMAGE_SUFFIXES:
        image_q |= Q(url__iendswith=suffix)
    return qs.filter(image_q)


def _extract_photo_metadata(
    recap_file: CustomRecapFile,
    pin_map: dict[int, PhotoWallPin],
) -> dict[str, Any] | None:
    url = _image_url(recap_file)
    if not url:
        return None
    recap = recap_file.custom_recap
    if recap is None:
        return None
    event = getattr(recap, "event", None)
    when = (
        getattr(event, "date", None)
        or getattr(event, "start_time", None)
        or getattr(recap, "submitted_at", None)
        or getattr(recap, "created_at", None)
    )

    store_name = (
        getattr(getattr(event, "retailer", None), "name", None)
        or getattr(getattr(recap, "retailer", None), "name", None)
        or getattr(recap, "typed_store_name", None)
        or getattr(event, "name", None)
        or getattr(recap, "name", None)
        or ""
    ).strip()
    banner_base, _store_num, _marked = split_store_number(store_name)
    retailer_banner = (banner_base or store_name).strip()

    city = (
        getattr(getattr(event, "location", None), "name", None)
        or getattr(getattr(recap, "location", None), "name", None)
        or ""
    ).strip()
    state_code = (
        getattr(getattr(event, "state", None), "code", None)
        or getattr(getattr(recap, "state", None), "code", None)
        or ""
    ).strip().upper()
    if city and state_code:
        market = f"{city}, {state_code}"
    else:
        market = city or state_code

    sku_set: set[str] = set()
    for ps in recap.custom_recap_product_sample.all():
        pname = getattr(getattr(ps, "product", None), "name", None)
        if pname and pname.strip():
            sku_set.add(pname.strip())
    for sp in recap.custom_recap_sale_performance.all():
        pname = getattr(getattr(sp, "product", None), "name", None)
        if pname and pname.strip():
            sku_set.add(pname.strip())
    req = getattr(event, "request", None) if event is not None else None
    if req is not None:
        for rp in req.request_product.all():
            pname = getattr(getattr(rp, "product", None), "name", None)
            if pname and pname.strip():
                sku_set.add(pname.strip())

    pin = pin_map.get(recap_file.id)
    scorer_caption = getattr(_related(recap_file, "photo_score"), "caption", "") or ""
    caption = (pin.note if pin and pin.note else scorer_caption).strip()

    return {
        "id": str(recap_file.id),
        "url": url,
        "category": (
            getattr(recap_file.file_recap_category, "name", None) or "In-Store Demo"
        ).strip(),
        "caption": caption,
        "store": store_name,
        "retailer": retailer_banner,
        "market": market,
        "state": state_code,
        "event_date": when.date().isoformat() if when else "",
        "skus": sorted(sku_set, key=str.lower),
        "recap_uuid": str(recap.uuid),
        "is_pinned": pin is not None,
        "pinned_at": pin.created_at.isoformat() if pin else None,
        "pin_note": pin.note if pin else "",
    }


def _hydrated_queryset(qs):
    return qs.select_related(
        "file_recap_category",
        "custom_recap",
        "custom_recap__retailer",
        "custom_recap__location",
        "custom_recap__state",
        "custom_recap__event",
        "custom_recap__event__retailer",
        "custom_recap__event__location",
        "custom_recap__event__state",
        "custom_recap__event__request",
        "photo_score",
    ).prefetch_related(
        Prefetch(
            "custom_recap__custom_recap_product_sample",
            queryset=CustomRecapProductSample.objects.select_related("product"),
        ),
        Prefetch(
            "custom_recap__custom_recap_sale_performance",
            queryset=CustomRecapSalePerformance.objects.select_related("product"),
        ),
        "custom_recap__event__request__request_product__product",
    )


def get_photo_wall(
    access: ResolvedWorkspaceAccess,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    retailer: str | None = None,
    market: str | None = None,
    sku: str | None = None,
    category: str | None = None,
    search: str | None = None,
    pinned_only: bool = False,
    first: int = 60,
    offset: int = 0,
) -> dict[str, Any]:
    """Return filterable live in-store demo photos + facet choices for the
    caller's authorized brand tenant."""
    tenant_id = access.tenant.id
    base_qs = _base_photo_wall_queryset(
        tenant_id,
        start_date=start_date,
        end_date=end_date,
        access=access,
    )

    pins = list(
        PhotoWallPin.objects.filter(tenant_id=tenant_id).order_by(
            "display_order", "-created_at"
        )
    )
    pin_map = {p.custom_recap_file_id: p for p in pins}

    # Hydrate within a bounded window (newest first) so facets (retailer,
    # market, SKU, category) reflect real photos in this brand's date window.
    rows = list(
        _hydrated_queryset(base_qs).order_by(
            "-custom_recap__event__date", "-custom_recap_id", "id"
        )[:600]
    )

    all_items: list[dict[str, Any]] = []
    retailers_set: set[str] = set()
    markets_set: set[str] = set()
    skus_set: set[str] = set()
    categories_set: set[str] = set()

    for rf in rows:
        item = _extract_photo_metadata(rf, pin_map)
        if item is None:
            continue
        all_items.append(item)
        if item["retailer"]:
            retailers_set.add(item["retailer"])
        if item["market"]:
            markets_set.add(item["market"])
        for s_name in item["skus"]:
            skus_set.add(s_name)
        if item["category"]:
            categories_set.add(item["category"])

    pinned_count = sum(1 for it in all_items if it["is_pinned"])

    filtered = all_items
    if pinned_only:
        filtered = [it for it in filtered if it["is_pinned"]]
    if retailer and retailer.strip():
        needle = retailer.strip().lower()
        filtered = [
            it
            for it in filtered
            if needle in it["retailer"].lower() or needle in it["store"].lower()
        ]
    if market and market.strip():
        needle = market.strip().lower()
        filtered = [
            it
            for it in filtered
            if needle in it["market"].lower() or needle == it["state"].lower()
        ]
    if sku and sku.strip():
        needle = sku.strip().lower()
        filtered = [
            it
            for it in filtered
            if any(needle in s.lower() for s in it["skus"])
        ]
    if category and category.strip():
        needle = category.strip().lower()
        filtered = [it for it in filtered if it["category"].lower() == needle]
    if search and search.strip():
        needle = search.strip().lower()
        filtered = [
            it
            for it in filtered
            if needle in it["store"].lower()
            or needle in it["retailer"].lower()
            or needle in it["market"].lower()
            or needle in it["caption"].lower()
            or needle in it["category"].lower()
            or any(needle in s.lower() for s in it["skus"])
        ]

    total = len(filtered)
    eff_first = max(1, min(int(first or 60), MAX_PAGE))
    eff_offset = max(0, int(offset or 0))
    page = filtered[eff_offset : eff_offset + eff_first]

    return {
        "total": total,
        "pinned_count": pinned_count,
        "photos": page,
        "retailers": sorted(retailers_set, key=str.lower),
        "markets": sorted(markets_set, key=str.lower),
        "skus": sorted(skus_set, key=str.lower),
        "categories": sorted(categories_set, key=str.lower),
    }


def toggle_photo_wall_pin(
    access: ResolvedWorkspaceAccess,
    *,
    photo_id: str | int,
    pinned: bool | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Pin or unpin an approved in-store demo photo for this brand's Buyer Pitch Decks."""
    if not access.can_view_brand_consolidated or not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can pin photos to Buyer Pitch Decks."
        )

    file_id = _parse_file_id(photo_id)
    if file_id is None:
        raise ValueError("Invalid photo ID.")

    eligible = (
        _base_photo_wall_queryset(access.tenant.id)
        .filter(id=file_id)
        .first()
    )
    if eligible is None:
        raise PermissionError(
            "Photo not found in this brand workspace or is not an approved demo photo."
        )

    existing = PhotoWallPin.objects.filter(
        tenant=access.tenant, custom_recap_file=eligible
    ).first()

    should_pin = (existing is None) if pinned is None else bool(pinned)

    if should_pin:
        clean_note = (note or "").strip()[:240]
        if existing is None:
            pin = PhotoWallPin.objects.create(
                tenant=access.tenant,
                custom_recap_file=eligible,
                pinned_by=access.user,
                note=clean_note,
            )
            action = "photo_pinned"
        else:
            if note is not None:
                existing.note = clean_note
                existing.save(update_fields=["note"])
            pin = existing
            action = "photo_pin_updated"
        is_pinned = True
        pinned_at = pin.created_at.isoformat()
        pin_note = pin.note
    else:
        if existing is not None:
            existing.delete()
        action = "photo_unpinned"
        is_pinned = False
        pinned_at = None
        pin_note = ""

    pinned_count = PhotoWallPin.objects.filter(tenant=access.tenant).count()
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="buyer_pitch_deck",
        action=action,
        target_type="CustomRecapFile",
        target_id=str(file_id),
        summary=f"{'Pinned' if is_pinned else 'Unpinned'} photo #{file_id} for Buyer Pitch Decks.",
    )
    return {
        "photo_id": str(file_id),
        "is_pinned": is_pinned,
        "pinned_at": pinned_at,
        "pin_note": pin_note,
        "pinned_count": pinned_count,
    }


def resolve_deck_photos(
    tenant_id: int,
    *,
    requested_photo_ids: list[str | int] | None = None,
) -> tuple[list[int], list[dict[str, Any]]]:
    """Validate and resolve the buyer-safe photo list for a BuyerPitchDeck.

    When ``requested_photo_ids`` is provided, every ID is strictly validated to
    belong to ``tenant_id`` (approved, non-archived, non-paperwork); any cross-
    tenant or unapproved ID raises ``PermissionError``.
    When ``requested_photo_ids`` is ``None``, uses the brand's currently pinned
    ``PhotoWallPin`` photos (newest pins first, up to MAX_PINNED_PHOTOS_PER_DECK).
    """
    pins = list(
        PhotoWallPin.objects.filter(tenant_id=tenant_id).order_by(
            "display_order", "-created_at"
        )
    )
    pin_map = {p.custom_recap_file_id: p for p in pins}

    if requested_photo_ids is not None:
        parsed_ids: list[int] = []
        seen: set[int] = set()
        for raw in requested_photo_ids[:MAX_PINNED_PHOTOS_PER_DECK]:
            pid = _parse_file_id(raw)
            if pid is None:
                raise ValueError(f"Invalid photo ID: {raw}")
            if pid not in seen:
                seen.add(pid)
                parsed_ids.append(pid)
        if not parsed_ids:
            return [], []
        eligible_rows = list(
            _hydrated_queryset(
                _base_photo_wall_queryset(tenant_id).filter(id__in=parsed_ids)
            )
        )
        by_id = {r.id: r for r in eligible_rows}
        if len(by_id) != len(parsed_ids):
            raise PermissionError(
                "One or more selected photos do not belong to this brand or are not approved."
            )
        ordered_rows = [by_id[pid] for pid in parsed_ids]
    else:
        pin_ids = [p.custom_recap_file_id for p in pins[:MAX_PINNED_PHOTOS_PER_DECK]]
        if not pin_ids:
            return [], []
        eligible_rows = list(
            _hydrated_queryset(
                _base_photo_wall_queryset(tenant_id).filter(id__in=pin_ids)
            )
        )
        by_id = {r.id: r for r in eligible_rows}
        ordered_rows = [by_id[pid] for pid in pin_ids if pid in by_id]

    validated_ids: list[int] = []
    photos_out: list[dict[str, Any]] = []
    for rf in ordered_rows:
        meta = _extract_photo_metadata(rf, pin_map)
        if meta is None:
            continue
        validated_ids.append(rf.id)
        # Buyer-safe photo record — no internal recap UUIDs needed publicly.
        photos_out.append(
            {
                "id": meta["id"],
                "url": meta["url"],
                "category": meta["category"],
                "caption": meta["caption"],
                "store": meta["store"],
                "retailer": meta["retailer"],
                "market": meta["market"],
                "state": meta["state"],
                "eventDate": meta["event_date"],
                "skus": meta["skus"],
            }
        )
    return validated_ids, photos_out
