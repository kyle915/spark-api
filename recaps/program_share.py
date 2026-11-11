"""What a brand's PUBLIC program page shows.

Anyone with the link can read this, so it is deliberately narrow: program
totals, the states activated in, and field photos from approved recaps. It
never includes ambassador names, receipts, spend, store-level rows, notes
or free-text answers.
"""

from __future__ import annotations

from django.utils import timezone

from recaps.models import CustomRecapFile
from recaps.tenant_overview import (
    tenant_event_recap_counts,
    tenant_kpi_totals,
    tenant_market_performance,
)
from recaps.types import _is_image_url, _is_paperwork_file
from recaps.heic_conversion import display_blob_name, is_heic_blob
from utils.gcs import extract_blob_name_from_url, public_url

MAX_PHOTOS = 12
# Look at more files than we show: receipts, PDFs and videos are skipped.
_PHOTO_SCAN_LIMIT = 150


def _photo_url(recap_file) -> str | None:
    field_file = getattr(recap_file, "url", None)
    blob = getattr(field_file, "name", None) or (str(field_file) if field_file else "")
    blob_name = extract_blob_name_from_url(blob)
    if not blob_name:
        return None
    url = public_url(blob_name)
    if _is_image_url(url):
        return url
    if is_heic_blob(blob_name):
        return public_url(display_blob_name(blob_name))
    return None


def program_photos(tenant_id: int, limit: int = MAX_PHOTOS) -> list[dict]:
    """Recent field photos from approved, non-archived recaps — at most one
    per recap first, so one photo-heavy activation cannot fill the page."""
    files = (
        CustomRecapFile.objects.filter(
            custom_recap__tenant_id=tenant_id,
            custom_recap__approved=True,
            custom_recap__archived_at__isnull=True,
        )
        .select_related("file_recap_category")
        .order_by("-custom_recap_id", "id")[:_PHOTO_SCAN_LIMIT]
    )
    firsts: list[dict] = []
    extras: list[dict] = []
    seen_recaps: set[int] = set()
    for f in files:
        if _is_paperwork_file(f):
            continue
        url = _photo_url(f)
        if not url:
            continue
        category = getattr(getattr(f, "file_recap_category", None), "name", None)
        item = {"url": url, "caption": category or ""}
        if f.custom_recap_id in seen_recaps:
            extras.append(item)
        else:
            seen_recaps.add(f.custom_recap_id)
            firsts.append(item)
    return (firsts + extras)[:limit]


def build_program_share(tenant) -> dict:
    """camelCase payload for ``GET /api/public/program/<token>``."""
    totals = tenant_kpi_totals(tenant.id)
    event_count, recap_count = tenant_event_recap_counts(tenant.id)
    sold = int(totals.products_sold or 0)
    markets = [
        {"state": row["state"], "activations": int(row.get("recap_count") or 0)}
        for row in tenant_market_performance(tenant.id)
        if row.get("state") and int(row.get("recap_count") or 0) > 0
    ]
    markets.sort(key=lambda m: (-m["activations"], m["state"]))
    return {
        "brand": tenant.name,
        "generatedAt": timezone.now().isoformat(),
        "totals": {
            "activations": int(recap_count or 0),
            "events": int(event_count or 0),
            "consumersReached": int(totals.consumers_reached or 0),
            "samplesDistributed": int(totals.samples_distributed or 0),
            "productsSold": sold,
            "states": len(markets),
        },
        "markets": markets,
        "photos": program_photos(tenant.id),
    }
