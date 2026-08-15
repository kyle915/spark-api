"""Load a recap for the public share surface (JSON + PDF).

Used by ``recaps.share_views``. Token verification lives in
``recaps.recap_tokens``; this module turns a ``(kind, id)`` into a
public-safe dict and (optionally) branded PDF bytes.
"""

from __future__ import annotations

import concurrent.futures as cf
from typing import Any

from django.db.models import Prefetch

from recaps import models
from recaps.heic_conversion import display_blob_name
from recaps.pdf import (
    IMAGE_EXTENSIONS,
    build_recap_pdf,
    is_image_bytes,
    should_embed_recap_file,
)
from utils.gcs import download_blob_bytes, extract_blob_name_from_url, public_url


def load_shared_recap(kind: str, recap_id: int):
    """Return the Recap / CustomRecap row, or None if it no longer exists."""
    if kind == "custom":
        return (
            models.CustomRecap.objects.select_related(
                "event",
                "event__tenant",
                "event__retailer",
                "event__retailer__location",
                "event__location",
                "event__state",
                "event__request",
                "event__request__retailer",
                "ambassador",
                "ambassador__user",
                "job",
                "retailer",
                "location",
                "state",
                "custom_recap_template",
            )
            .prefetch_related(
                Prefetch(
                    "custom_recap_files",
                    queryset=models.CustomRecapFile.objects.select_related(
                        "file_type",
                        "file_recap_category",
                    ),
                ),
                Prefetch(
                    "custom_recap_product_sample",
                    queryset=models.CustomRecapProductSample.objects.select_related(
                        "product"
                    ),
                ),
                Prefetch(
                    "custom_recap_sale_performance",
                    queryset=models.CustomRecapSalePerformance.objects.select_related(
                        "product",
                        "type_of_good",
                    ),
                ),
                "custom_field_value__custom_field__custom_field_type",
                "custom_field_value__custom_field__recap_section",
                "custom_recap_template__custom_field__custom_field_type",
                "custom_recap_template__custom_field__recap_section",
                "event__tenant__themes",
                "tenant__themes",
            )
            .filter(id=recap_id)
            .first()
        )
    return (
        models.Recap.objects.select_related(
            "event",
            "event__tenant",
            "event__request",
            "event__request__retailer",
            "job",
            "retailer",
            "ambassador",
            "ambassador__user",
        )
        .prefetch_related(
            Prefetch(
                "recap_files",
                queryset=models.RecapFile.objects.select_related(
                    "file_type",
                    "file_recap_category",
                ),
            ),
            "consumer_engagements",
            Prefetch(
                "product_samples",
                queryset=models.ProductSamples.objects.select_related("product"),
            ),
            Prefetch(
                "sales_performance",
                queryset=models.SalesPerformance.objects.select_related(
                    "product",
                    "type_of_good",
                ),
            ),
            "consumer_feedback",
            "account_feedback",
            "event__tenant__themes",
        )
        .filter(id=recap_id)
        .first()
    )


def _ba_name(recap) -> str:
    amb = getattr(recap, "ambassador", None)
    user = getattr(amb, "user", None) if amb else None
    if user:
        return " ".join(
            p for p in (user.first_name, user.last_name) if p
        ).strip()
    return (getattr(recap, "external_ba_name", None) or "").strip()


def _file_public_url(blob) -> str | None:
    blob_name = extract_blob_name_from_url(str(blob or ""))
    if not blob_name:
        return None
    return public_url(display_blob_name(blob_name) or blob_name)



def _tenant_brand(recap) -> dict | None:
    event = getattr(recap, "event", None)
    tenant = getattr(event, "tenant", None) or getattr(recap, "tenant", None)
    if tenant is None:
        return None
    manager = getattr(tenant, "themes", None)
    themes = list(manager.all()) if manager is not None else []
    theme = next((t for t in themes if getattr(t, "color_scheme", None) == "dark"), None)
    if theme is None and themes:
        theme = themes[0]
    css = (getattr(theme, "css_variables", None) or {}) if theme else {}
    if not isinstance(css, dict):
        css = {}
    return {
        "name": getattr(tenant, "name", None),
        "colorScheme": getattr(theme, "color_scheme", None) or "dark",
        "primary": css.get("--color-primary"),
        "cssVariables": css,
    }


def _signoff_payload(recap) -> dict | None:
    status = (getattr(recap, "client_signoff_status", None) or "").strip()
    if not status:
        return None
    at = getattr(recap, "client_signoff_at", None)
    return {
        "status": status,
        "comment": getattr(recap, "client_signoff_comment", None) or "",
        "at": at.isoformat() if hasattr(at, "isoformat") else (str(at) if at else None),
    }


def recap_to_public_dict(kind: str, recap) -> dict[str, Any]:
    """CamelCase payload for GET /api/public/recap/<token>.

    Photos are public GCS URLs (JPG sibling for HEIC). No shareToken —
    the caller already holds the token they arrived with.
    """
    event = getattr(recap, "event", None)
    req = getattr(event, "request", None) if event else None
    ev_retailer = getattr(event, "retailer", None) if event else None
    req_retailer = getattr(req, "retailer", None) if req else None
    retailer = (
        getattr(getattr(recap, "retailer", None), "name", None)
        or getattr(ev_retailer, "name", None)
        or getattr(req_retailer, "name", None)
        or getattr(req, "retailer_name", None)
    )
    venue = getattr(ev_retailer, "name", None) or getattr(req, "retailer_name", None)
    address = (
        (getattr(event, "address", None) or "").strip()
        or (getattr(ev_retailer, "address", None) or "").strip()
        or (getattr(req, "retailer_address", None) or "").strip()
        or (getattr(req, "address", None) or "").strip()
        or None
    )
    city = (
        getattr(getattr(event, "location", None), "name", None)
        or getattr(getattr(ev_retailer, "location", None), "name", None)
        or getattr(getattr(recap, "location", None), "name", None)
    )
    state = (
        getattr(getattr(event, "state", None), "name", None)
        or getattr(getattr(recap, "state", None), "name", None)
        or getattr(getattr(event, "state", None), "code", None)
    )
    date = (
        getattr(recap, "event_date", None)
        or getattr(event, "date", None)
        or getattr(recap, "created_at", None)
    )
    if hasattr(date, "isoformat"):
        date = date.isoformat()

    photos: list[dict[str, Any]] = []
    if kind == "custom":
        files = list(recap.custom_recap_files.all())
        for f in files:
            url = _file_public_url(getattr(f, "url", None))
            if not url:
                continue
            photos.append(
                {
                    "url": url,
                    "name": f.name,
                    "category": getattr(f.file_recap_category, "name", None)
                    or "Uncategorized",
                }
            )
        samples = [
            {
                "name": getattr(s.product, "name", None),
                "quantity": s.quantity,
            }
            for s in recap.custom_recap_product_sample.all()
        ]
        fields: list[dict[str, Any]] = []
        value_by_id = {
            item.custom_field_id: item for item in recap.custom_field_value.all()
        }
        template = getattr(recap, "custom_recap_template", None)
        template_fields = (
            list(template.custom_field.all()) if template else []
        )
        for cf in template_fields:
            cfv = value_by_id.get(cf.id)
            raw = cfv.value if cfv else None
            type_name = getattr(
                getattr(cf, "custom_field_type", None), "name", ""
            ) or ""
            is_image = any(
                tok in type_name.lower()
                for tok in ("image", "photo", "img")
            )
            fields.append(
                {
                    "section": getattr(
                        getattr(cf, "recap_section", None), "name", None
                    )
                    or "Details",
                    "name": cf.name,
                    "value": None if is_image else raw,
                    "imageUrl": _file_public_url(raw) if is_image and raw else None,
                }
            )
        sold = sampled = spend = None
        try:
            from recaps.types import (
                _account_spend_from_fields,
                _consumers_sampled_from_fields,
                _sold_units_from_fields,
            )
            pairs = [
                (getattr(v.custom_field, "name", None), v.value)
                for v in recap.custom_field_value.all()
            ]
            sold = _sold_units_from_fields(pairs)
            sampled = _consumers_sampled_from_fields(pairs)
            spend = _account_spend_from_fields(pairs)
        except Exception:
            sold = sampled = spend = None
        kpis = [
            {"label": "Engagements", "value": recap.total_engagements},
            {"label": "Sold", "value": sold},
            {"label": "Sampled", "value": sampled},
        ]
        base = sampled or recap.total_engagements
        if spend and base:
            kpis.append({"label": "$ / sample", "value": round(float(spend) / base, 2)})
        kpis = [k for k in kpis if k["value"] is not None]
    else:
        files = list(recap.recap_files.all())
        for f in files:
            url = _file_public_url(getattr(f, "file", None))
            if not url:
                continue
            photos.append(
                {
                    "url": url,
                    "name": f.name,
                    "category": getattr(f.file_recap_category, "name", None)
                    or "Uncategorized",
                }
            )
        samples = [
            {
                "name": getattr(s.product, "name", None),
                "quantity": s.quantity,
            }
            for s in recap.product_samples.all()
        ]
        fields = []
        engagement = next(iter(recap.consumer_engagements.all()), None)
        kpis = [
            {"label": "Samples", "value": getattr(engagement, "total_consumer", None)},
            {
                "label": "First-time",
                "value": getattr(engagement, "first_time_consumers", None),
            },
            {
                "label": "Brand aware",
                "value": getattr(engagement, "brand_aware_consumers", None),
            },
            {
                "label": "Willing to buy",
                "value": getattr(
                    engagement, "willing_to_purchase_consumers", None
                ),
            },
            {"label": "Products sold", "value": recap.products_sold},
        ]
        kpis = [k for k in kpis if k["value"] is not None]

    return {
        "kind": kind,
        "name": recap.name,
        "date": str(date) if date else None,
        "retailer": retailer,
        "venue": venue or getattr(event, "name", None),
        "address": address,
        "city": city,
        "state": state,
        "approved": bool(recap.approved),
        "ambassadorName": _ba_name(recap) or None,
        "templateName": getattr(
            getattr(recap, "custom_recap_template", None), "name", None
        ),
        "kpis": kpis,
        "samples": samples,
        "fields": fields,
        "photos": photos,
        "brand": _tenant_brand(recap),
        "clientSignoff": _signoff_payload(recap),
        "sharedAt": (
            recap.shared_at.isoformat()
            if getattr(recap, "shared_at", None) and hasattr(recap.shared_at, "isoformat")
            else None
        ),
    }


def _legacy_image_candidates(recap) -> list[tuple[object, str]]:
    out: list[tuple[object, str]] = []
    for rf in recap.recap_files.all():
        if not should_embed_recap_file(rf):
            continue
        blob_name = extract_blob_name_from_url(str(rf.file))
        if blob_name:
            out.append((rf, blob_name))
    return out


def _custom_image_candidates(recap) -> tuple[list[tuple[object, str]], list[tuple[str, str]]]:
    candidates: list[tuple[object, str]] = []
    for crf in recap.custom_recap_files.all():
        if not should_embed_recap_file(crf):
            continue
        blob_name = extract_blob_name_from_url(str(crf.url))
        if blob_name:
            candidates.append((crf, blob_name))

    field_candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cfv in recap.custom_field_value.all():
        raw = cfv.value
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = raw.split("?", 1)[0].split("#", 1)[0]
        _, _, ext = path.rpartition(".")
        if not ext or f".{ext.lower()}" not in IMAGE_EXTENSIONS:
            continue
        if raw in seen:
            continue
        blob_name = extract_blob_name_from_url(raw)
        if not blob_name:
            continue
        seen.add(raw)
        field_candidates.append((raw, blob_name))
    return candidates, field_candidates


def _fetch_images(
    candidates: list[tuple[object, str]],
) -> list[dict[str, Any]]:
    def _one(item):
        recap_file, blob_name = item
        try:
            data = download_blob_bytes(blob_name)
        except Exception:
            return None
        if not data or not is_image_bytes(data):
            return None
        cat = getattr(
            getattr(recap_file, "file_recap_category", None), "name", None
        )
        return {
            "name": getattr(recap_file, "name", None) or "Image",
            "bytes": data,
            "category": cat or "Uncategorized",
        }

    images: list[dict[str, Any]] = []
    if not candidates:
        return images
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        for entry in pool.map(_one, candidates):
            if entry is not None:
                images.append(entry)
    return images


def _fetch_field_images(
    field_candidates: list[tuple[str, str]],
) -> dict[str, bytes]:
    def _one(item):
        value_key, blob_name = item
        try:
            data = download_blob_bytes(blob_name)
        except Exception:
            return None
        if not data or not is_image_bytes(data):
            return None
        return (value_key, data)

    out: dict[str, bytes] = {}
    if not field_candidates:
        return out
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        for result in pool.map(_one, field_candidates):
            if result is not None:
                out[result[0]] = result[1]
    return out


def render_shared_recap_pdf(kind: str, recap) -> bytes:
    """Branded PDF bytes for a public recap share link."""
    if kind == "custom":
        candidates, field_candidates = _custom_image_candidates(recap)
        images = _fetch_images(candidates)
        field_images = _fetch_field_images(field_candidates)
        return build_recap_pdf(
            recap, images, custom_field_images=field_images, public_share=True
        )
    images = _fetch_images(_legacy_image_candidates(recap))
    return build_recap_pdf(recap, images, public_share=True)
