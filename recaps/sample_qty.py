"""Per-template copy for the per-SKU sample count (e.g. "Cans sampled").

Set on ``CustomRecapTemplate.layout`` as ``sampleQtyLabel`` /
``sampleQtyTotalLabel``. Templates without them keep the generic copy.
"""

from __future__ import annotations


def sample_qty_labels(template) -> tuple[str, str]:
    """``(per_sku_label, total_label)``; empty strings when the template has none."""
    layout = getattr(template, "layout", None)
    if not isinstance(layout, dict):
        return "", ""
    per_sku = str(layout.get("sampleQtyLabel") or "").strip()
    total = str(layout.get("sampleQtyTotalLabel") or "").strip()
    if per_sku and not total:
        total = f"Total {per_sku[:1].lower()}{per_sku[1:]}"
    return per_sku, total
