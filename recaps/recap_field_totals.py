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


_FIVE_DIGITS = re.compile(r"\b\d{5}\b")
_SUFFIXES = {
    "rd": "road", "ave": "avenue", "av": "avenue", "st": "street", "dr": "drive",
    "ln": "lane", "blvd": "boulevard", "pkwy": "parkway", "hwy": "highway",
    "ct": "court", "pl": "place", "n": "north", "s": "south", "e": "east", "w": "west",
}


def _zip_code(text: str) -> str:
    """The ZIP in a typed address: the last 5-digit group that is not the
    leading street number ("28000 Greenspot Rd … 92346" → 92346)."""
    text = (text or "").strip()
    found = [m for m in _FIVE_DIGITS.finditer(text)]
    found = [m for m in found if m.start() > 0]
    return found[-1].group(0) if found else ""


def _normal_place(text: str) -> str:
    tokens = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
    return " ".join(_SUFFIXES.get(t, t) for t in tokens)


def booth_day_key(address, event_name, coordinates, event_date) -> str:
    """Which booth, which day — "" when it cannot be told.

    Ambassadors at the same booth each type the address their own way
    ("28000 Greenspot Rd, Highland, CA 92346" / "Greenspot Road, Highland,
    California 92346"), so the store is identified by the most stable thing
    available, in order:

    1. the ZIP code in the address or event name;
    2. the event's coordinates, rounded to about a kilometre;
    3. the address (else name) with punctuation, case and street
       abbreviations normalised.

    The retailer is deliberately NOT used: a retailer here is a banner
    (one "Costco" for every warehouse), which would merge different stores.
    """
    day = event_date.date().isoformat() if event_date else ""
    if not day:
        return ""
    zip_code = _zip_code(address) or _zip_code(event_name)
    if zip_code:
        return f"z{zip_code}|{day}"
    try:
        if coordinates and len(coordinates) >= 2:
            lat, lng = float(coordinates[0]), float(coordinates[1])
            if lat or lng:
                return f"g{lat:.2f},{lng:.2f}|{day}"
    except (TypeError, ValueError):
        pass
    place = _normal_place(address) or _normal_place(event_name)
    return f"a{place}|{day}" if place else ""


def collapse_booth_days(rows):
    """One answer per question per booth-day.

    ``rows`` are ``(section, section_order, label, field_order, value,
    recap_id, booth_key)``. When several recaps describe the same booth on
    the same day they each repeat the booth's numbers, so for every question
    this keeps a single value per booth-day:

    * counts and money — the LARGEST value any of those recaps reported (a
      partial or blank recap must not pull the day down, and two matching
      recaps must not double it);
    * prices, rates and ratings — the mean of the values reported that day.

    Non-numeric answers pass through (they are ignored by the roll-up
    anyway). The booth-day stands in for the recap id afterwards, so the
    "how many" beside each total counts booth-days. A recap with no usable
    booth key is kept as its own day rather than merged with strangers.
    """
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    passthrough = []
    for section, section_order, label, field_order, value, recap_id, booth in rows:
        day = booth or f"recap-{recap_id}"
        parsed = parse_number(value)
        if parsed is None:
            passthrough.append((section, section_order, label, field_order, value, day))
            continue
        key = ((section or "").strip().lower(), (label or "").strip().lower(), day)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "head": (section, section_order, label, field_order),
                "day": day,
                "values": [],
                "percent": False,
            }
        g["values"].append(parsed[0])
        g["percent"] = g["percent"] or parsed[1]
    out = list(passthrough)
    for g in groups.values():
        section, section_order, label, field_order = g["head"]
        if field_kind(label or "", g["percent"]) == "average":
            number = sum(g["values"]) / len(g["values"])
        else:
            number = max(g["values"])
        text = repr(round(number, 4)).rstrip("0").rstrip(".") if number % 1 else str(int(number))
        if g["percent"]:
            text += "%"
        out.append((section, section_order, label, field_order, text, g["day"]))
    return out


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
    tenant_id: int,
    start: date | None,
    end: date | None,
    *,
    one_per_booth_day: bool | None = None,
) -> list[dict]:
    """``one_per_booth_day=None`` follows the brand's own setting
    (``Tenant.count_one_recap_per_booth_day``)."""
    if one_per_booth_day is None:
        from tenants.models import Tenant

        one_per_booth_day = bool(
            Tenant.objects.filter(id=tenant_id)
            .values_list("count_one_recap_per_booth_day", flat=True)
            .first()
        )
    window = _inclusive_dates_to_window(start, end)
    qs = CustomFieldValue.objects.filter(
        custom_recap__tenant_id=tenant_id,
        custom_recap__archived_at__isnull=True,
    )
    qs = _approved_only(qs, "custom_recap__")
    qs = _filter_event_window(qs, "custom_recap__event__", window)
    base = qs.order_by("custom_recap_id", "id")
    if not one_per_booth_day:
        rows = base.values_list(
            "custom_field__recap_section__name",
            "custom_field__recap_section__order",
            "custom_field__name",
            "custom_field__order",
            "value",
            "custom_recap_id",
        )
        return rollup_field_values(rows.iterator(chunk_size=5000))
    wide = base.values_list(
        "custom_field__recap_section__name",
        "custom_field__recap_section__order",
        "custom_field__name",
        "custom_field__order",
        "value",
        "custom_recap_id",
        "custom_recap__event__address",
        "custom_recap__event__name",
        "custom_recap__event__coordinates",
        "custom_recap__event__date",
    )
    keyed = (
        (sec, sec_order, label, f_order, value, recap_id,
         booth_day_key(address, event_name, coordinates, event_date))
        for sec, sec_order, label, f_order, value, recap_id,
            address, event_name, coordinates, event_date in wide.iterator(chunk_size=5000)
    )
    return rollup_field_values(collapse_booth_days(keyed))
