"""Per-recap facts the dashboard sentences are computed from.

One row per recap, on the basis every dashboard figure for a client uses:

* approved, not archived;
* the request it hangs off is not soft-deleted, the event is not kept off
  the dashboard, and the filing is not an agency one left out of totals
  (:func:`recaps.tenant_overview._filter_event_window`);
* windowed on the EVENT date, never on when the recap row was created.

Consumers sampled comes from :func:`recaps.consumer_breakdown.consumer_rows`
and the conversion base and units from
:func:`recaps.tenant_overview.conversion_window_totals`: the same code the
charts and the Conversion tile run, so a sentence cannot disagree with them
for any other reason than an archived recap, which the charts still count
and these rows do not.

READ-ONLY. No ambassador name is read here, so none can reach a sentence.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Sum
from django.db.models.functions import Coalesce

from recaps.models import (
    CustomFieldValue,
    CustomRecap,
    CustomRecapProductSample,
    ProductSamples,
    Recap,
)
from recaps.tenant_overview import (
    _approved_only,
    _event_date_expr,
    _filter_event_window,
    _inclusive_dates_to_window,
    _sku_pulse_label,
    conversion_window_totals,
)
from recaps.types import _SOLD_SKU_LINE_RE, _parse_recap_int

_PLACE_FIELDS = (
    "id",
    "_day",
    "event_id",
    "event__retailer__name",
    "event__request__retailer__name",
    "event__request__retailer_name",
    "event__state__code",
    "event__address",
)


def _first(*names) -> str:
    for name in names:
        cleaned = (name or "").strip()
        if cleaned:
            return cleaned
    return ""


def _live(model, tenant_filter: dict, window):
    """Approved, non-archived recaps of ``model`` in the event-date window."""
    return (
        _approved_only(
            _filter_event_window(
                model.objects.filter(archived_at__isnull=True, **tenant_filter),
                "event__",
                window,
            ),
            "",
        )
        .annotate(_day=_event_date_expr("event__"))
        .filter(_day__isnull=False)
    )


def recap_facts(tenant_id: int, start: date | None, end: date | None) -> list[dict]:
    """One dict per recap: ``kind``, ``id``, ``event_id``, ``day``, ``store``
    (the retailer name the store chart uses, may be blank), ``place`` (the
    folded street address, blank when there is none), ``state``, ``city``,
    ``consumers`` (None when the recap recorded none), and ``base`` /
    ``units`` (None when the recap is not in the conversion rate)."""
    from events.map_points import is_lookup_worthy, normalise_address
    from events.market import parse_address_geo
    from recaps.consumer_breakdown import consumer_rows

    window = _inclusive_dates_to_window(start, end)
    rows: dict[tuple[str, int], dict] = {}
    for kind, model, tenant_filter in (
        ("custom", CustomRecap, {"tenant_id": tenant_id}),
        ("legacy", Recap, {"event__tenant_id": tenant_id}),
    ):
        for recap_id, day, event_id, retailer, req_retailer, req_name, state, address in (
            _live(model, tenant_filter, window).values_list(*_PLACE_FIELDS).iterator()
        ):
            geo = parse_address_geo(address)
            rows[(kind, recap_id)] = {
                "kind": kind,
                "id": recap_id,
                "event_id": event_id,
                "day": day.date(),
                "store": _first(retailer, req_retailer, req_name),
                "place": normalise_address(address) if is_lookup_worthy(address) else "",
                "state": (state or geo.state_code or "").upper(),
                "city": (geo.city or "").strip(),
                "consumers": None,
                "base": None,
                "units": None,
            }

    for row in consumer_rows(tenant_id, start, end, include_archived=False):
        held = rows.get((row["kind"], row["id"]))
        if held is not None:
            held["consumers"] = int(row["consumers"] or 0)

    conversion = conversion_window_totals(tenant_id, start, end, detail=True)
    for row in conversion.get("recaps", []):
        if not row.get("counted"):
            continue
        held = rows.get((row["kind"], row["id"]))
        if held is not None:
            held["base"] = int(row["base"] or 0)
            held["units"] = int(row["units"] or 0)
    return list(rows.values())


def product_facts(tenant_id: int, start: date | None, end: date | None) -> list[dict]:
    """Per catalog product: ``label``, ``samples`` handed out, the number of
    ``recaps`` that logged it, and, only where recaps carry a per-product
    "Units sold: <product>" line for it, ``sold`` and ``sold_recaps``.

    Most templates log purchases as one total, not per product. For those
    ``sold`` is None: a per-product sales figure is never derived from a
    total.
    """
    window = _inclusive_dates_to_window(start, end)
    products: dict[int, dict] = {}

    def add(rows, recap_field: str) -> None:
        for row in rows:
            qty = int(row["total"] or 0)
            label = _sku_pulse_label(
                row["product__name"], row["product__product_type__name"]
            )
            if qty <= 0 or not label:
                continue
            bucket = products.setdefault(
                int(row["product_id"]),
                {
                    "label": label,
                    "name": (row["product__name"] or "").strip(),
                    "samples": 0,
                    "recap_ids": set(),
                    "sold": None,
                    "sold_recaps": 0,
                },
            )
            bucket["samples"] += qty
            bucket["recap_ids"].add((recap_field, row[recap_field]))

    add(
        _approved_only(
            _filter_event_window(
                CustomRecapProductSample.objects.filter(
                    custom_recap__tenant_id=tenant_id,
                    custom_recap__archived_at__isnull=True,
                ),
                "custom_recap__event__",
                window,
            ),
            "custom_recap__",
        )
        .values(
            "custom_recap_id",
            "product_id",
            "product__name",
            "product__product_type__name",
        )
        .annotate(total=Coalesce(Sum("quantity"), 0)),
        "custom_recap_id",
    )
    add(
        _approved_only(
            _filter_event_window(
                ProductSamples.objects.filter(
                    recap__event__tenant_id=tenant_id,
                    recap__archived_at__isnull=True,
                ),
                "recap__event__",
                window,
            ),
            "recap__",
        )
        .values(
            "recap_id", "product_id", "product__name", "product__product_type__name"
        )
        .annotate(total=Coalesce(Sum("quantity"), 0)),
        "recap_id",
    )

    # Per-product sold lines ("Units sold: Almond Oil"), matched by name.
    by_name = {
        p["name"].casefold(): p for p in products.values() if p["name"]
    }
    if by_name:
        lines = (
            _approved_only(
                _filter_event_window(
                    CustomFieldValue.objects.filter(
                        custom_recap__tenant_id=tenant_id,
                        custom_recap__archived_at__isnull=True,
                        custom_field__name__iregex=_SOLD_SKU_LINE_RE.pattern,
                    ),
                    "custom_recap__event__",
                    window,
                ),
                "custom_recap__",
            )
            .values_list("custom_field__name", "value")
            .iterator()
        )
        for name, value in lines:
            label = _SOLD_SKU_LINE_RE.split(name or "", maxsplit=1)[-1]
            product = by_name.get(" ".join(label.split()).casefold())
            parsed = _parse_recap_int(value)
            if product is None or parsed is None:
                continue
            product["sold"] = (product["sold"] or 0) + max(0, parsed)
            product["sold_recaps"] += 1

    out = []
    for product in products.values():
        out.append(
            {
                "label": product["label"],
                "samples": product["samples"],
                "recaps": len(product["recap_ids"]),
                "sold": product["sold"],
                "sold_recaps": product["sold_recaps"],
            }
        )
    out.sort(key=lambda p: (-p["samples"], p["label"].casefold()))
    return out


def previous_window(start: date, end: date) -> tuple[date, date]:
    """The window of the same length that ends the day before ``start``."""
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return prev_end - timedelta(days=days - 1), prev_end


def state_names(codes) -> dict[str, str]:
    """``{"TX": "Texas"}`` for the codes Spark has a State row for."""
    from events.models import State

    wanted = {c for c in codes if c}
    if not wanted:
        return {}
    return {
        code.upper(): name
        for code, name in State.objects.filter(code__in=wanted).values_list(
            "code", "name"
        )
        if name
    }
