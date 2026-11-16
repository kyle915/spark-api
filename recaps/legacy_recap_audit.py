"""Read-only per-recap audit of a tenant's LEGACY ``Recap`` rows.

The field-level export (:mod:`recaps.recap_field_export`) only covers
``CustomRecap``. ``campaign-to-date`` covers legacy recaps but only as
per-Request roll-ups, only for live (not soft-deleted) requests, and without
the per-recap approval flag. That leaves two questions a client deliverable
can't answer:

  * was each legacy recap actually approved (and not archived)?
  * which legacy recaps sit OUTSIDE a live request — on a soft-deleted
    request, or on a standalone event with no request at all — and so are
    silently missing from any campaign-to-date roll-up?

This module answers both: one row per legacy recap with its request link and
``deleted_at``, event date, retailer/store/state, approval state, the KPI
children (ConsumerEngagements, cans/packs/products sold, ProductSamples),
consumer feedback text, and file links.

READ-ONLY. Brand ambassadors are emitted as opaque ids only (ambassador id),
never names or emails — the payload lands in a CI artifact.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from django.db.models import Q

from recaps.models import Recap
from utils.gcs import extract_blob_name_from_url, public_url


def _parse_ymd(raw):
    if not raw:
        return None
    if isinstance(raw, date):
        return raw
    return datetime.strptime(str(raw), "%Y-%m-%d").date()


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _event_date(recap):
    """Effective EVENT date: event.date → event.start_time → request.date.

    Same coalesce order as the dashboard hero and ``_event_date_expr``.
    """
    ev = recap.event
    if ev is None:
        return None
    for value in (ev.date, ev.start_time, getattr(ev.request, "date", None) if ev.request_id else None):
        d = _as_date(value)
        if d:
            return d
    return None


def _name(obj, attr="name"):
    return getattr(obj, attr, None) if obj is not None else None


def _iso(value):
    return value.isoformat() if value else None


def _file_url(f):
    field_file = getattr(f, "file", None)
    if not field_file:
        return None
    try:
        blob = field_file.name
    except Exception:  # noqa: BLE001
        blob = str(field_file)
    return public_url(extract_blob_name_from_url(blob))


def _queryset(tenant_id: int):
    return (
        Recap.objects.filter(event__tenant_id=tenant_id)
        .select_related(
            "event",
            "event__request",
            "event__event_type",
            "event__retailer",
            "event__location",
            "event__state",
            "retailer",
            "location",
            "state",
        )
        .prefetch_related(
            "consumer_engagements",
            "product_samples__product",
            "consumer_feedback",
            "recap_files__file_recap_category",
        )
        .order_by("id")
    )


def build_legacy_recap_audit(tenant_id: int, *, start=None, end=None) -> dict:
    """One row per legacy recap for ``tenant_id``, optionally windowed by EVENT date
    (inclusive). Recaps with no resolvable event date are kept only when no window
    is given, and are always counted in ``summary.no_event_date``."""
    start_d, end_d = _parse_ymd(start), _parse_ymd(end)
    windowed = bool(start_d or end_d)

    # Requests that also carry a CUSTOM recap: campaign-to-date mixes the two
    # shapes per request, so callers that split legacy from custom by request
    # need to know which requests are mixed.
    from recaps.models import CustomRecap

    mixed_request_ids = set(
        CustomRecap.objects.filter(
            Q(tenant_id=tenant_id) | Q(event__tenant_id=tenant_id),
            event__request__isnull=False,
        ).values_list("event__request_id", flat=True)
    )

    rows = []
    summary = Counter()
    for recap in _queryset(tenant_id):
        ev = recap.event
        req = ev.request if ev is not None and ev.request_id else None
        ev_date = _event_date(recap)
        summary["legacy_recaps_total"] += 1
        if ev_date is None:
            summary["no_event_date"] += 1
            if windowed:
                continue
        elif windowed and ((start_d and ev_date < start_d) or (end_d and ev_date > end_d)):
            summary["out_of_window"] += 1
            continue

        if req is None:
            link = "no_request"
        elif req.deleted_at is not None:
            link = "request_soft_deleted"
        else:
            link = "live_request"
        in_campaign_to_date = link == "live_request"

        eng = next(iter(recap.consumer_engagements.all()), None)
        fb = next(iter(recap.consumer_feedback.all()), None)
        approved = bool(recap.approved)
        archived = recap.archived_at is not None
        client_visible = approved and not archived

        summary[f"link:{link}"] += 1
        summary["approved" if approved else "not_approved"] += 1
        if archived:
            summary["archived"] += 1
        if client_visible and in_campaign_to_date:
            summary["approved_in_live_request"] += 1

        rows.append(
            {
                "recap_id": recap.id,
                "recap_uuid": str(recap.uuid),
                "recap_name": recap.name,
                "event_id": ev.id if ev else None,
                "event_name": _name(ev),
                "event_type": _name(getattr(ev, "event_type", None)),
                "event_date": _iso(ev_date),
                "event_address": getattr(ev, "address", None),
                "event_exclude_from_dashboard": bool(getattr(ev, "exclude_from_dashboard", False)),
                "request_id": req.id if req else None,
                "request_name": req.name if req else None,
                "request_deleted_at": _iso(req.deleted_at) if req else None,
                "request_link": link,
                "in_campaign_to_date": in_campaign_to_date,
                "request_has_custom_recap": bool(req and req.id in mixed_request_ids),
                "retailer": _name(recap.retailer) or _name(getattr(ev, "retailer", None)),
                "location": _name(recap.location) or _name(getattr(ev, "location", None)),
                "state": _name(recap.state, "code") or _name(getattr(ev, "state", None), "code"),
                "approved": approved,
                "approved_at": _iso(recap.approved_at),
                "archived_at": _iso(recap.archived_at),
                "client_visible": client_visible,
                "submitted_at": _iso(recap.submited_at),
                "created_at": _iso(recap.created_at),
                # Opaque BA identity only — never names/emails in this payload.
                "ambassador_id": recap.ambassador_id,
                "has_external_ba": bool(recap.external_ba_name),
                "total_engagements": recap.total_engagements,
                "consumers": getattr(eng, "total_consumer", None),
                "first_time_consumers": getattr(eng, "first_time_consumers", None),
                "brand_aware_consumers": getattr(eng, "brand_aware_consumers", None),
                "willing_to_purchase": getattr(eng, "willing_to_purchase_consumers", None),
                "not_willing": getattr(eng, "not_willing_consumers", None),
                "cans_sold": recap.total_cans_sold,
                "packs_sold": recap.total_packs_sold,
                "products_sold": recap.products_sold,
                "product_samples": [
                    {"product": _name(s.product), "quantity": s.quantity}
                    for s in recap.product_samples.all()
                ],
                "feedback": {
                    "feedback": getattr(fb, "feedback", None),
                    "quotes": getattr(fb, "quotes", None),
                    "positive_stories": getattr(fb, "positive_stories", None),
                    "reasons_to_decline": getattr(fb, "reasons_to_decline", None),
                    "demographics": getattr(fb, "demographics", None),
                },
                "files": [
                    {
                        "category": _name(f.file_recap_category),
                        "name": f.name,
                        "url": _file_url(f),
                    }
                    for f in recap.recap_files.all()
                ],
            }
        )

    return {
        "tenant_id": tenant_id,
        "window": {"start": _iso(start_d), "end": _iso(end_d)},
        "summary": dict(sorted(summary.items())),
        "row_count": len(rows),
        "rows": rows,
    }
