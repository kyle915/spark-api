"""A brand's field photos in one place, filterable.

Every image attached to an approved, non-archived custom recap, newest
event first, with enough about each one to filter and caption it: category,
store, state and event date. Receipts and other paperwork are left out
unless asked for — this is the gallery a client browses and downloads from.

Photos are returned as public image URLs (the same ones the recap pages
use); HEIC originals resolve to their converted JPG.

READ-ONLY.
"""

from __future__ import annotations

from datetime import date

from recaps.heic_conversion import display_blob_name, is_heic_blob
from recaps.models import CustomRecapFile
from recaps.tenant_overview import (
    _approved_only,
    _filter_event_window,
    _inclusive_dates_to_window,
)
from recaps.types import _PAPERWORK_CATEGORY_RE, _is_image_url
from utils.gcs import extract_blob_name_from_url, public_url

MAX_PAGE = 120
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")


def _image_url(recap_file) -> str | None:
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


def _base_queryset(tenant_id: int, start: date | None, end: date | None):
    from django.db.models import Q

    qs = CustomRecapFile.objects.filter(
        custom_recap__tenant_id=tenant_id,
        custom_recap__archived_at__isnull=True,
    )
    qs = _approved_only(qs, "custom_recap__")
    qs = _filter_event_window(
        qs, "custom_recap__event__", _inclusive_dates_to_window(start, end)
    )
    images = Q()
    for suffix in _IMAGE_SUFFIXES:
        images |= Q(url__iendswith=suffix)
    return qs.filter(images)


def tenant_photo_gallery(
    tenant_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    state: str | None = None,
    store: str | None = None,
    category: str | None = None,
    include_paperwork: bool = False,
    first: int = 60,
    offset: int = 0,
) -> dict:
    """One page of photos plus the filter choices that exist in the window."""
    base = _base_queryset(tenant_id, start, end)
    if not include_paperwork:
        base = base.exclude(
            file_recap_category__name__iregex=_PAPERWORK_CATEGORY_RE.pattern
        )

    # Filter choices come from the whole window, before the narrowing
    # filters, so picking one state does not hide the others.
    categories = sorted(
        {
            (n or "").strip()
            for n in base.values_list("file_recap_category__name", flat=True).distinct()
            if (n or "").strip()
        },
        key=str.lower,
    )
    states = sorted(
        {
            (c or "").strip().upper()
            for c in base.values_list(
                "custom_recap__event__state__code", flat=True
            ).distinct()
            if (c or "").strip()
        }
    )

    qs = base
    if state and state.strip():
        qs = qs.filter(custom_recap__event__state__code__iexact=state.strip())
    if category and category.strip():
        qs = qs.filter(file_recap_category__name__iexact=category.strip())
    if store and store.strip():
        from django.db.models import Q

        needle = store.strip()
        qs = qs.filter(
            Q(custom_recap__event__retailer__name__icontains=needle)
            | Q(custom_recap__event__name__icontains=needle)
            | Q(custom_recap__event__address__icontains=needle)
            | Q(custom_recap__name__icontains=needle)
        )

    total = qs.count()
    first = max(1, min(int(first or 60), MAX_PAGE))
    offset = max(0, int(offset or 0))
    page = (
        qs.select_related(
            "file_recap_category",
            "custom_recap",
            "custom_recap__event",
            "custom_recap__event__retailer",
            "custom_recap__event__state",
        )
        .order_by("-custom_recap__event__date", "-custom_recap_id", "id")[
            offset : offset + first
        ]
    )

    photos: list[dict] = []
    for f in page:
        url = _image_url(f)
        if not url:
            continue
        recap = f.custom_recap
        event = getattr(recap, "event", None)
        when = getattr(event, "date", None) or getattr(event, "start_time", None)
        photos.append(
            {
                "id": str(f.id),
                "url": url,
                "category": getattr(f.file_recap_category, "name", None) or "",
                "recap_uuid": str(recap.uuid),
                "store": (
                    getattr(getattr(event, "retailer", None), "name", None)
                    or getattr(event, "name", None)
                    or recap.name
                    or ""
                ),
                "state": getattr(getattr(event, "state", None), "code", None) or "",
                "event_date": when.date().isoformat() if when else "",
            }
        )
    return {
        "total": total,
        "photos": photos,
        "categories": categories,
        "states": states,
    }
