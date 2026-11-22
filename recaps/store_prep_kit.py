"""Store Prep Kit — pre-shift briefing payload for BA shift cards.

Composes existing store logistics notes, last shelf visit at that door,
SKUs on the event, BA training resources, and a light stock-risk badge.
No new aisle columns; free-text logistics notes stay the source of truth
for cooler/parking/door copy.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.utils import timezone

from recaps.shelf_check import NO, UNCLEAR, effective_answers, visit_answer

logger = logging.getLogger(__name__)

_LAST_SHELF_LOOKBACK_DAYS = 90


def _normalize_address(raw: str | None) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip().lower())
    text = re.sub(r"[.,#]", "", text)
    return text


def _event_address(event) -> str:
    req = getattr(event, "request", None)
    return (
        (getattr(event, "address", None) or "").strip()
        or (getattr(req, "address", None) or "").strip()
    )


def _sku_names(event) -> list[str]:
    from ambassadors.checkin_web import _event_products

    names: list[str] = []
    for row in _event_products(event) or []:
        name = (row.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names[:24]


def _resources(event) -> list[dict]:
    from ambassadors.checkin_web import build_checkin_resources

    tenant = getattr(event, "tenant", None)
    return build_checkin_resources(tenant)


def _last_shelf_visit(event) -> dict | None:
    """Most recent approved retail shelf visit at the same address."""
    from recaps.models import CustomRecap, CustomRecapFile
    from recaps.shelf_check import SHELF_CATEGORY_IREGEX, _image_q, retail_recap_q

    tenant_id = getattr(event, "tenant_id", None)
    address = _normalize_address(_event_address(event))
    if not tenant_id or not address:
        return None

    since = timezone.now().date() - timedelta(days=_LAST_SHELF_LOOKBACK_DAYS)
    event_id = getattr(event, "id", None)
    qs = (
        CustomRecap.objects.filter(tenant_id=tenant_id)
        .filter(retail_recap_q())
        .filter(approved=True, archived_at__isnull=True)
        .filter(event__date__gte=since)
        .exclude(event_id=event_id)
        .select_related("event", "event__request", "event__retailer")
        .order_by("-event__date", "-id")
    )
    matched = None
    for recap in qs[:80]:
        other = _normalize_address(_event_address(recap.event))
        if other and other == address:
            matched = recap
            break
    if matched is None:
        return None

    files = (
        CustomRecapFile.objects.filter(
            custom_recap_id=matched.id,
            file_recap_category__name__iregex=SHELF_CATEGORY_IREGEX,
        )
        .filter(_image_q())
        .select_related("shelf_reading", "shelf_correction")
    )
    answers = []
    for recap_file in files:
        ans = effective_answers(
            getattr(recap_file, "shelf_reading", None),
            getattr(recap_file, "shelf_correction", None),
        )
        if ans is not None:
            answers.append(ans)
    if not answers:
        return None

    visit = visit_answer(answers)
    day = getattr(matched.event, "date", None)
    return {
        "eventDate": day.isoformat() if day else "",
        "productOnShelf": visit.get("product_on_shelf") or UNCLEAR,
        "priceTagVisible": visit.get("price_tag_visible") or UNCLEAR,
        "storeName": (getattr(matched.event, "name", None) or "").strip()[:120],
    }


def _stock_risk(event, last_shelf: dict | None) -> dict | None:
    """Light pre-demo risk badge from the last shelf visit only.

    High = product was not found last visit; watch = unclear / no price tag.
    """
    if last_shelf is None:
        return None
    on_shelf = last_shelf.get("productOnShelf") or UNCLEAR
    tag = last_shelf.get("priceTagVisible") or UNCLEAR
    day = last_shelf.get("eventDate") or ""
    if on_shelf == NO:
        return {
            "level": "high",
            "reason": (
                f"Last shelf photo ({day or 'recent'}) did not show the product. "
                "Confirm stock before the demo."
            ),
        }
    if on_shelf == UNCLEAR or tag == NO:
        return {
            "level": "watch",
            "reason": (
                f"Last shelf check ({day or 'recent'}) was unclear or missing a price tag. "
                "Verify the set before you start sampling."
            ),
        }
    return {
        "level": "none",
        "reason": f"Last shelf visit ({day or 'recent'}) showed the product on shelf.",
    }


def build_store_prep_kit(event) -> dict:
    """Public prep-kit DTO for check-in context / GraphQL / job-board shifts."""
    empty = {
        "logisticsNotes": "",
        "lastShelfVisit": None,
        "skus": [],
        "resources": [],
        "stockRisk": None,
    }
    if event is None or not getattr(event, "tenant_id", None):
        return empty

    logistics = ""
    try:
        from store_logistics.service import get_store_logistics_for_event

        logistics = get_store_logistics_for_event(event) or ""
    except Exception:  # noqa: BLE001 — prep kit is optional chrome
        logger.exception("store prep kit logistics failed event=%s", getattr(event, "id", None))

    last_shelf = None
    try:
        last_shelf = _last_shelf_visit(event)
    except Exception:  # noqa: BLE001
        logger.exception("store prep kit last shelf failed event=%s", getattr(event, "id", None))

    stock_risk = None
    try:
        stock_risk = _stock_risk(event, last_shelf)
    except Exception:  # noqa: BLE001
        logger.exception("store prep kit stock risk failed event=%s", getattr(event, "id", None))

    return {
        "logisticsNotes": logistics,
        "lastShelfVisit": last_shelf,
        "skus": _sku_names(event),
        "resources": _resources(event),
        "stockRisk": stock_risk,
    }


def prep_kit_is_empty(kit: dict | None) -> bool:
    if not isinstance(kit, dict):
        return True
    if (kit.get("logisticsNotes") or "").strip():
        return False
    if kit.get("lastShelfVisit"):
        return False
    if kit.get("skus"):
        return False
    if kit.get("resources"):
        return False
    risk = kit.get("stockRisk")
    if isinstance(risk, dict) and risk.get("level") in ("high", "watch"):
        return False
    return True
