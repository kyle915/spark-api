"""Named portal CSV column presets for automated / assisted POS ingestion.

Maps the six distributor/retailer portal exports beverage brands download
weekly onto the canonical POS field keys used by ``pilot.pos._infer_pos_map``.
Presets are exact header names; missing columns still fall through to synonym
inference so partial exports remain usable.
"""

from __future__ import annotations

from typing import Any

# field_key → exact CSV header as exported by the portal.
POS_PRESETS: dict[str, dict[str, str]] = {
    "unfi_clearview": {
        "retailer": "Banner",
        "store_id": "Customer Number",
        "store_name": "Customer Name",
        "address": "Address",
        "city": "City",
        "state_code": "State",
        "sku": "UPC",
        "pack_size": "Pack Size",
        "week_end": "Week Ending",
        "register_units": "Units",
        "sales_dollars": "Dollars",
        "baseline_units": "Baseline Units",
    },
    "kehe_connect": {
        "retailer": "Retail Banner",
        "store_id": "Store Number",
        "store_name": "Store Name",
        "city": "City",
        "state_code": "State",
        "sku": "Item Number",
        "pack_size": "Unit Size",
        "week_end": "Week End Date",
        "register_units": "Qty Sold",
        "sales_dollars": "Sales $",
        "baseline_units": "Avg Weekly Units",
    },
    "whole_foods_vip": {
        "retailer": "Banner",
        "store_id": "Store #",
        "store_name": "Store",
        "city": "City",
        "state_code": "State",
        "sku": "PLU/UPC",
        "week_end": "Week Ending",
        "register_units": "Units Sold",
        "sales_dollars": "Net Sales",
        "baseline_units": "4-Wk Avg Units",
        "has_promotion": "Promo Flag",
    },
    "sprouts": {
        "retailer": "Chain",
        "store_id": "Location ID",
        "store_name": "Location Name",
        "city": "City",
        "state_code": "ST",
        "sku": "UPC",
        "week_end": "Week Ending Date",
        "register_units": "Scan Units",
        "sales_dollars": "Scan Dollars",
        "baseline_units": "Baseline Units",
        "has_stockout": "OOS Flag",
    },
    "vip_idig": {
        "retailer": "Account Banner",
        "store_id": "Outlet ID",
        "store_name": "Outlet Name",
        "city": "City",
        "state_code": "State",
        "sku": "Product Code",
        "pack_size": "Pack",
        "week_end": "Period End",
        "register_units": "Depletion Units",
        "sales_dollars": "Depletion Dollars",
        "baseline_units": "Prior Period Units",
    },
    "spins_byzzer": {
        "retailer": "Retailer",
        "store_id": "Store ID",
        "store_name": "Store Name",
        "city": "City",
        "state_code": "State",
        "sku": "UPC",
        "week_end": "Week Ending",
        "register_units": "Unit Sales",
        "sales_dollars": "Dollar Sales",
        "baseline_units": "Base Unit Sales",
        "has_promotion": "Promo Indicator",
    },
}

POS_PRESET_LABELS: dict[str, str] = {
    "unfi_clearview": "UNFI Clearview",
    "kehe_connect": "KeHE Connect",
    "whole_foods_vip": "Whole Foods VIP / Portal",
    "sprouts": "Sprouts",
    "vip_idig": "VIP iDIG (Bev-Alc / RTD)",
    "spins_byzzer": "SPINS / Byzzer store-level",
}

# Soft default banner hints when the CSV omits a retailer column.
POS_PRESET_DEFAULT_BANNERS: dict[str, str] = {
    "unfi_clearview": "UNFI",
    "kehe_connect": "KeHE",
    "whole_foods_vip": "Whole Foods Market",
    "sprouts": "Sprouts Farmers Market",
    "vip_idig": "VIP",
    "spins_byzzer": "SPINS",
}


def list_pos_presets() -> list[dict[str, Any]]:
    """Stable ordered list for GraphQL / UI selectors."""
    return [
        {
            "key": key,
            "label": POS_PRESET_LABELS.get(key, key),
            "column_map": dict(cols),
            "default_banner": POS_PRESET_DEFAULT_BANNERS.get(key, ""),
        }
        for key, cols in POS_PRESETS.items()
    ]


def resolve_pos_column_map(
    preset_key: str | None,
    explicit_map: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Merge a named preset with an optional explicit override map.

    Returns None when neither is provided (caller falls back to synonym
    inference only). Explicit keys win over preset keys.
    """
    merged: dict[str, str] = {}
    key = (preset_key or "").strip()
    if key and key in POS_PRESETS:
        merged.update(POS_PRESETS[key])
    if explicit_map:
        for k, col in explicit_map.items():
            if col:
                merged[k] = col
    return merged or None
