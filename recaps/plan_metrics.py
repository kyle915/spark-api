"""The numbers a filed recap can push to a field-marketing plan.

Read-only: every value comes straight off the recap through the same
matchers the cards, Insights and PDFs use. Nothing here edits, scales or
re-derives a submitted number. Each metric keeps its own meaning, so
consumers sampled, people engaged, samples handed out, emails collected and
cases dropped never fold into one another.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ambassadors.attendance_hours import clock_facts, worked_hours
from events.torch_retail_routing import (
    EXECUTION_EVENT,
    EXECUTION_GUERILLA,
    EXECUTION_RETAIL,
    EXECUTION_SEEDING,
    torch_recap_execution_type,
)
from recaps.drop_off_locations import (
    is_drop_off_locations_field,
    parse_drop_off_locations,
    sku_label,
)
from recaps.sample_qty import sample_qty_labels
from recaps.tenant_overview import EMAIL_METHOD_FIELD, EMAILS_COLLECTED_FIELD
from recaps.types import (
    _account_spend_from_fields,
    _consumers_sampled_from_fields,
    _parse_recap_int,
    _parse_recap_money,
    _people_engaged_from_fields,
    _sold_units_from_fields,
)

KIND_LEGACY = "legacy"
KIND_CUSTOM = "custom"

CONSUMERS_SAMPLED = "consumers_sampled"
PEOPLE_ENGAGED = "people_engaged"
SAMPLES_BY_SKU = "samples_by_sku"
EMAILS_COLLECTED = "emails_collected"
SAMPLE_FORMAT = "sample_format"
UNITS_SOLD = "units_sold"
CASES_DROPPED = "cases_dropped"
DROP_OFF_LOCATIONS = "drop_off_locations"
MILEAGE = "mileage"
CLOCKED_HOURS = "clocked_hours"
PHOTOS = "photos"
SPEND = "spend"

METRIC_ORDER: tuple[str, ...] = (
    CONSUMERS_SAMPLED,
    PEOPLE_ENGAGED,
    SAMPLES_BY_SKU,
    EMAILS_COLLECTED,
    SAMPLE_FORMAT,
    UNITS_SOLD,
    CASES_DROPPED,
    DROP_OFF_LOCATIONS,
    MILEAGE,
    CLOCKED_HOURS,
    PHOTOS,
    SPEND,
)

UNITS: dict[str, str] = {
    CONSUMERS_SAMPLED: "consumers",
    PEOPLE_ENGAGED: "people",
    SAMPLES_BY_SKU: "samples",
    EMAILS_COLLECTED: "emails",
    SAMPLE_FORMAT: "",
    UNITS_SOLD: "units",
    CASES_DROPPED: "cases",
    DROP_OFF_LOCATIONS: "stops",
    MILEAGE: "mi",
    CLOCKED_HOURS: "h",
    PHOTOS: "photos",
    SPEND: "$",
}

_SAMPLING = (
    CONSUMERS_SAMPLED,
    PEOPLE_ENGAGED,
    SAMPLES_BY_SKU,
    EMAILS_COLLECTED,
    SAMPLE_FORMAT,
    CLOCKED_HOURS,
    PHOTOS,
)
DEFAULT_KEYS: dict[str, tuple[str, ...]] = {
    EXECUTION_EVENT: _SAMPLING,
    EXECUTION_GUERILLA: _SAMPLING,
    EXECUTION_SEEDING: (
        CASES_DROPPED,
        DROP_OFF_LOCATIONS,
        MILEAGE,
        EMAILS_COLLECTED,
        PHOTOS,
    ),
    EXECUTION_RETAIL: (
        CONSUMERS_SAMPLED,
        PEOPLE_ENGAGED,
        UNITS_SOLD,
        EMAILS_COLLECTED,
        SAMPLE_FORMAT,
        CLOCKED_HOURS,
        PHOTOS,
    ),
}

_SAMPLE_FORMAT_RE = re.compile(r"^\s*sample\s+format\s*$", re.I)
_MILEAGE_RE = re.compile(r"\bmileage\b|\bmiles\b", re.I)


@dataclass(frozen=True)
class RecapMetric:
    key: str
    label: str
    value: float | None
    text: str
    unit: str
    parts: tuple[tuple[str, int], ...] = field(default_factory=tuple)


def _fmt_number(value: float, unit: str) -> str:
    if unit == "$":
        return f"${value:,.2f}"
    if unit in ("h", "mi"):
        return f"{value:,.1f} {unit}"
    return f"{int(round(value)):,}"


def _metric(key: str, label: str, value: float, parts=()) -> RecapMetric:
    unit = UNITS[key]
    return RecapMetric(
        key=key,
        label=label,
        value=float(value),
        text=_fmt_number(float(value), unit),
        unit=unit,
        parts=tuple(parts),
    )


def _choice_text(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text.split(",")
    if not isinstance(parsed, list):
        parsed = [parsed]
    return [str(v).strip() for v in parsed if str(v).strip()]


def _field_pairs(custom_recap) -> list[tuple[str | None, str | None]]:
    return [
        (getattr(cfv.custom_field, "name", None), cfv.value)
        for cfv in custom_recap.custom_field_value.select_related("custom_field").order_by(
            "custom_field__order", "custom_field_id"
        )
    ]


def _first_value(pairs, name: str) -> str | None:
    for field_name, value in pairs:
        if (field_name or "").strip().lower() == name.lower():
            return value
    return None


def _clocked_hours(recap) -> float | None:
    if not recap.event_id or not recap.ambassador_id:
        return None
    facts = clock_facts([recap.event_id]).get((recap.event_id, recap.ambassador_id))
    hours, estimated = worked_hours(facts, None)
    if hours is None or estimated:
        return None
    return hours


def _sample_parts(rows) -> list[tuple[str, int]]:
    merged: dict[str, int] = {}
    for name, qty in rows:
        if qty is None or qty <= 0:
            continue
        merged[name] = merged.get(name, 0) + int(qty)
    return list(merged.items())


def _custom_metrics(recap) -> list[RecapMetric]:
    pairs = _field_pairs(recap)
    out: list[RecapMetric] = []

    sampled = _consumers_sampled_from_fields(pairs)
    if sampled is not None:
        out.append(_metric(CONSUMERS_SAMPLED, "Consumers sampled", sampled))
    engaged = _people_engaged_from_fields(pairs)
    if engaged is not None:
        out.append(_metric(PEOPLE_ENGAGED, "People engaged", engaged))

    parts = _sample_parts(
        (s.product.name, s.quantity)
        for s in recap.custom_recap_product_sample.select_related("product").order_by("id")
    )
    if parts:
        _per_sku, total_label = sample_qty_labels(recap.custom_recap_template)
        out.append(
            _metric(
                SAMPLES_BY_SKU,
                total_label or "Samples handed out",
                sum(qty for _name, qty in parts),
                parts,
            )
        )

    emails_raw = _first_value(pairs, EMAILS_COLLECTED_FIELD)
    emails = _parse_recap_int(emails_raw) if emails_raw is not None else None
    if emails is not None:
        metric = _metric(EMAILS_COLLECTED, "Emails collected", max(0, emails))
        methods = _choice_text(_first_value(pairs, EMAIL_METHOD_FIELD))
        if methods:
            metric = RecapMetric(
                key=metric.key,
                label=metric.label,
                value=metric.value,
                text=f"{metric.text} · {', '.join(methods)}",
                unit=metric.unit,
            )
        out.append(metric)

    formats: list[str] = []
    for name, value in pairs:
        if name and _SAMPLE_FORMAT_RE.search(name):
            formats.extend(_choice_text(value))
    if formats:
        out.append(
            RecapMetric(
                key=SAMPLE_FORMAT,
                label="Sample format",
                value=None,
                text=", ".join(dict.fromkeys(formats)),
                unit="",
            )
        )

    if torch_recap_execution_type(recap) == EXECUTION_RETAIL:
        sold = _sold_units_from_fields(pairs)
        if sold is not None:
            out.append(_metric(UNITS_SOLD, "Units sold", sold))

    locations = []
    for name, value in pairs:
        if is_drop_off_locations_field(name):
            locations.extend(parse_drop_off_locations(value))
    if locations:
        case_parts = _sample_parts(
            (sku_label(sku), sku.cases) for loc in locations for sku in loc.skus
        )
        out.append(
            _metric(
                CASES_DROPPED,
                "Cases dropped",
                sum(loc.cases for loc in locations),
                case_parts,
            )
        )
        out.append(_metric(DROP_OFF_LOCATIONS, "Drop-off locations", len(locations)))

    for name, value in pairs:
        if name and _MILEAGE_RE.search(name):
            miles = _parse_recap_money(value)
            if miles is not None and miles >= 0:
                out.append(_metric(MILEAGE, "Mileage", miles))
                break

    hours = _clocked_hours(recap)
    if hours is not None:
        out.append(_metric(CLOCKED_HOURS, "BA hours (clocked)", hours))

    photos = recap.custom_recap_files.count()
    if photos:
        out.append(_metric(PHOTOS, "Photos", photos))

    spend = _account_spend_from_fields(pairs)
    if spend is not None:
        out.append(_metric(SPEND, "Spend", spend))
    return out


def _legacy_metrics(recap) -> list[RecapMetric]:
    out: list[RecapMetric] = []
    engagement = recap.consumer_engagements.order_by("id").first()
    if engagement is not None and engagement.total_consumer is not None:
        out.append(
            _metric(CONSUMERS_SAMPLED, "Consumers sampled", engagement.total_consumer)
        )
    if recap.total_engagements is not None:
        out.append(_metric(PEOPLE_ENGAGED, "People engaged", recap.total_engagements))
    parts = _sample_parts(
        (s.product.name, s.quantity)
        for s in recap.product_samples.select_related("product").order_by("id")
    )
    if parts:
        out.append(
            _metric(
                SAMPLES_BY_SKU,
                "Samples handed out",
                sum(qty for _name, qty in parts),
                parts,
            )
        )
    if torch_recap_execution_type(recap) == EXECUTION_RETAIL:
        sold = recap.products_sold
        if sold is None and (
            recap.total_cans_sold is not None or recap.total_packs_sold is not None
        ):
            sold = (recap.total_cans_sold or 0) + (recap.total_packs_sold or 0)
        if sold is not None:
            out.append(_metric(UNITS_SOLD, "Units sold", sold))
    hours = _clocked_hours(recap)
    if hours is not None:
        out.append(_metric(CLOCKED_HOURS, "BA hours (clocked)", hours))
    photos = recap.recap_files.count()
    if photos:
        out.append(_metric(PHOTOS, "Photos", photos))
    if recap.account_spend_amount is not None:
        out.append(_metric(SPEND, "Spend", float(recap.account_spend_amount)))
    return out


def recap_metrics(recap, kind: str) -> list[RecapMetric]:
    """Every pushable metric the recap actually answered, in display order."""
    found = _custom_metrics(recap) if kind == KIND_CUSTOM else _legacy_metrics(recap)
    rank = {key: i for i, key in enumerate(METRIC_ORDER)}
    return sorted(found, key=lambda m: rank[m.key])


def default_keys(recap, available: list[RecapMetric]) -> list[str]:
    """Tactic-appropriate starting selection, limited to what the recap has."""
    wanted = DEFAULT_KEYS.get(torch_recap_execution_type(recap), DEFAULT_KEYS[EXECUTION_RETAIL])
    have = {m.key for m in available}
    return [key for key in wanted if key in have]
