"""Roll up EVERY numeric recap answer for a brand, field by field.

The program report's headline KPIs come from a fixed vocabulary (sampled,
sold, …). Recap forms ask for much more than that — per-SKU counts,
inventory, demographics, foot traffic — and each brand words its form
differently. This module totals whatever the brand's approved recaps
actually collected, under the form's own section and field names, so the
report shows the whole picture without anyone maintaining a field list.

Scope matches the other client-facing numbers: approved, non-archived custom
recaps, windowed by EVENT date.

How a field is treated is decided from its answers and its label:

* It is only included when most of its answers are plain numbers.
* Counts and money are SUMMED.
* Prices, rates, percentages and 1–5 style ratings are AVERAGED — adding
  them up would be meaningless.
* Identifiers that merely look numeric (store numbers, zip codes, phone
  numbers, times) are left out.

READ-ONLY.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date

from recaps.models import CustomFieldValue
from recaps.tenant_overview import (
    _approved_only,
    _filter_event_window,
    _inclusive_dates_to_window,
)

_NUMBER_RE = re.compile(r"^\s*\$?\s*(-?\d{1,3}(?:,\d{3})+|-?\d+)(\.\d+)?\s*(%)?\s*$")
_NOT_A_MEASURE_RE = re.compile(
    r"zip|postal|phone|\bstore\s*(#|no\b|number)|\bid\b|\bcode\b|\byear\b|"
    r"address|receipt\s*#|\btime\b|\bdate\b|\bhour of\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"\$|\bspend\b|\bspent\b|\bcost\b|\brevenue\b|\bsales\s*(\$|amount|total)|"
    r"\bamount\b|\bexpense",
    re.IGNORECASE,
)
_AVERAGE_RE = re.compile(
    r"\bprice\b|\brate\b|\bpercent|%|\blevel\b|\brating\b|\bscore\b|\bscale\b|"
    r"\baverage\b|\bavg\b|\bper\s+(hour|unit|person|consumer)\b|\btemperature\b",
    re.IGNORECASE,
)

# A field needs at least this share of plain-number answers to be a metric.
_MIN_NUMERIC_SHARE = 0.6


def parse_number(raw) -> tuple[float, bool] | None:
    """(value, had_percent_sign) for a plain numeric answer, else None."""
    m = _NUMBER_RE.match(str(raw if raw is not None else ""))
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    try:
        value = float(whole + (m.group(2) or ""))
    except ValueError:
        return None
    return value, bool(m.group(3))


def field_kind(label: str, saw_percent: bool) -> str:
    """'money' | 'average' | 'count' for an included field."""
    label = label or ""
    if saw_percent or _AVERAGE_RE.search(label):
        return "average"
    if _MONEY_RE.search(label):
        return "money"
    return "count"


def rollup_field_values(rows) -> list[dict]:
    """Group ``(section, section_order, label, field_order, value, recap_id)``
    rows into sections of totals. Pure — no database."""
    fields: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    for section, section_order, label, field_order, value, recap_id in rows:
        label = (label or "").strip()
        if not label:
            continue
        text = str(value if value is not None else "").strip()
        if text in ("", "[]", "null"):
            continue
        section = (section or "").strip() or "Other"
        key = (section.lower(), label.lower())
        f = fields.get(key)
        if f is None:
            f = fields[key] = {
                "section": section,
                "section_order": section_order or 0,
                "label": label,
                "field_order": field_order or 0,
                "sum": 0.0,
                "numeric": 0,
                "answered": 0,
                "percent": False,
                "recaps": set(),
            }
        f["answered"] += 1
        parsed = parse_number(text)
        if parsed is None:
            continue
        f["sum"] += parsed[0]
        f["numeric"] += 1
        f["percent"] = f["percent"] or parsed[1]
        f["recaps"].add(recap_id)

    sections: "OrderedDict[str, dict]" = OrderedDict()
    ordered = sorted(
        fields.values(),
        key=lambda f: (f["section_order"], f["section"].lower(), f["field_order"]),
    )
    for f in ordered:
        if f["numeric"] == 0 or f["numeric"] / f["answered"] < _MIN_NUMERIC_SHARE:
            continue
        if _NOT_A_MEASURE_RE.search(f["label"]):
            continue
        kind = field_kind(f["label"], f["percent"])
        average = f["sum"] / f["numeric"]
        sec = sections.setdefault(
            f["section"].lower(), {"section": f["section"], "fields": []}
        )
        sec["fields"].append(
            {
                "label": f["label"],
                "kind": kind,
                "total": average if kind == "average" else f["sum"],
                "average": average,
                "recaps": len(f["recaps"]),
                "is_percent": bool(f["percent"]),
            }
        )
    return [s for s in sections.values() if s["fields"]]


def tenant_recap_field_totals(
    tenant_id: int, start: date | None, end: date | None
) -> list[dict]:
    window = _inclusive_dates_to_window(start, end)
    qs = CustomFieldValue.objects.filter(
        custom_recap__tenant_id=tenant_id,
        custom_recap__archived_at__isnull=True,
    )
    qs = _approved_only(qs, "custom_recap__")
    qs = _filter_event_window(qs, "custom_recap__event__", window)
    rows = qs.order_by("custom_recap_id", "id").values_list(
        "custom_field__recap_section__name",
        "custom_field__recap_section__order",
        "custom_field__name",
        "custom_field__order",
        "value",
        "custom_recap_id",
    )
    return rollup_field_values(rows.iterator(chunk_size=5000))
