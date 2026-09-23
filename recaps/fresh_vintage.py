"""Fresh Vintage Farms · Costco Roadshow recap.

Field names here are the contract for the walk-up form, the sold-units
matcher, and the auto-calc that fills units sold, estimated sales, and
conversion from inventory and price. Photo buckets stay photo dropzones.
"Total spend on the card" is the dollar field (cents required).
"""

from __future__ import annotations

import re

TENANT_NAME = "Fresh Vintage Farms"
TENANT_SLUG = "fresh-vintage-farms"
TEMPLATE_NAME = "Fresh Vintage Farms · Costco Roadshow Recap"
CODE_PREFIX = "FVF-"
PROGRAM_NAME = "Costco Roadshow"

BOTTLES_ALMOND = "Expeller Pressed Almond Oil: bottles opened for sampling"
BOTTLES_GARLIC = "Expeller Pressed Garlic Almond Oil: bottles opened for sampling"

PRICE_ALMOND = "Costco retail price: Almond Oil"
PRICE_GARLIC = "Costco retail price: Garlic Almond Oil"
START_ALMOND = "Starting inventory: Almond Oil"
START_GARLIC = "Starting inventory: Garlic Almond Oil"
END_ALMOND = "Ending inventory: Almond Oil"
END_GARLIC = "Ending inventory: Garlic Almond Oil"
UNITS_SOLD_ALMOND = "Units sold: Almond Oil"
UNITS_SOLD_GARLIC = "Units sold: Garlic Almond Oil"
TOTAL_UNITS = "Total units sold today"
ESTIMATED_SALES = "Estimated sales $"
SELL_OUT = "Did either SKU sell out?"
SELL_OUT_TIME = "Sell-out time"

CONSUMERS_SAMPLED = "Total number of consumers sampled"
SAMPLED_ALMOND = "# sampled Almond Oil"
SAMPLED_GARLIC = "# sampled Garlic Almond Oil"
FEMALES = "# of Females Sampled"
MALES = "# of Males Sampled"
FIRST_TIME = "How many were first-time Fresh Vintage Farms consumers?"
HEARD_OF = "How many had heard of Fresh Vintage Farms before?"
COOK_WITH = "How many already cook with almond oil?"
CURRENT_OIL = "What oil do most members currently use?"
WILLING = "How many would be willing to purchase after tasting?"
NOT_WILLING = "How many would NOT be willing to purchase after tasting?"
CONVERSION = "Conversion rate"

CONTACT = (
    "Costco contact spoken to (name + title, e.g. Roadshow Coordinator, "
    "Front End Manager)"
)
WAREHOUSE_FEEDBACK = "Any feedback from the warehouse / roadshow coordinator?"
CHANGE_NEXT = "Is there anything about the event you'd change next time?"
NOTES = "Any additional notes or observations?"

BOOTH = "Booth location in warehouse"
FOOT_TRAFFIC = "Foot traffic level"
BUSIEST = "Busiest time window"
NEARBY = "Other roadshows or demos nearby (brands/categories)?"
ISSUES = "Any inventory, pallet, or pricing sign issues?"
ISSUE_DETAILS = "Inventory, pallet, or pricing sign details"

FLAVOR = "Which flavor did members prefer?"
COMMENTS = "A few comments you heard from members about the product?"
STORIES = "What were 2 positive stories or reactions from today?"
DECLINE = "What were the top 2 reasons members declined to purchase?"
DECLINE_OTHER = "Other reason members declined"
USE_IT = "How did members say they'd use it?"
DEMOGRAPHICS = (
    "General demographics of members sampled (age range, gender, ethnicity)"
)

ACCOUNT_SPEND = "Total spend on the card"

SALES_NOTE = (
    "Note: This section matters most for a roadshow, because Costco and the "
    "client both judge the event on units moved."
)

OIL_OPTIONS = ["olive", "avocado", "canola/vegetable", "coconut", "other"]
DECLINE_OPTIONS = [
    "price",
    "already have oil at home",
    "tree nut allergy",
    "garlic too strong",
    "pack size",
    "don't cook much",
    "other",
]
USE_OPTIONS = [
    "high-heat cooking",
    "salads/dressings",
    "baking",
    "dipping",
    "skin/hair",
    "other",
]
BOOTH_OPTIONS = [
    "main aisle",
    "near entrance",
    "center",
    "back / food section",
    "other",
]
TRAFFIC_OPTIONS = ["1", "2", "3", "4", "5"]
WINDOW_OPTIONS = ["exec hours", "morning", "midday", "afternoon", "evening"]
FLAVOR_OPTIONS = ["Almond Oil", "Garlic Almond Oil", "split evenly"]
YES_NO = ["Yes", "No"]

# name, kind, required, options, placeholder
FieldSpec = tuple[str, str, bool, list[str], str]

SPEC: list[tuple[str, list[FieldSpec]]] = [
    (
        "Product Samples",
        [
            (BOTTLES_ALMOND, "number", True, [], ""),
            (BOTTLES_GARLIC, "number", True, [], ""),
        ],
    ),
    (
        "Sales Performance",
        [
            (PRICE_ALMOND, "number", True, [], SALES_NOTE),
            (PRICE_GARLIC, "number", True, [], ""),
            (START_ALMOND, "number", True, [], ""),
            (START_GARLIC, "number", True, [], ""),
            (END_ALMOND, "number", True, [], ""),
            (END_GARLIC, "number", True, [], ""),
            (
                UNITS_SOLD_ALMOND,
                "number",
                False,
                [],
                "Auto: starting inventory minus ending inventory",
            ),
            (
                UNITS_SOLD_GARLIC,
                "number",
                False,
                [],
                "Auto: starting inventory minus ending inventory",
            ),
            (
                TOTAL_UNITS,
                "number",
                False,
                [],
                "Auto: Almond Oil units sold + Garlic Almond Oil units sold",
            ),
            (
                ESTIMATED_SALES,
                "number",
                False,
                [],
                "Auto: units sold × Costco retail price, per SKU",
            ),
            (SELL_OUT, "select", True, YES_NO, ""),
            (SELL_OUT_TIME, "text", False, [], "What time, if a SKU sold out"),
        ],
    ),
    (
        "Sampling",
        [
            (CONSUMERS_SAMPLED, "number", True, [], ""),
            (SAMPLED_ALMOND, "number", True, [], ""),
            (SAMPLED_GARLIC, "number", True, [], ""),
            (FEMALES, "number", True, [], ""),
            (MALES, "number", True, [], ""),
            (FIRST_TIME, "number", True, [], ""),
            (HEARD_OF, "number", True, [], ""),
            (COOK_WITH, "number", True, [], ""),
            (CURRENT_OIL, "multiselect", True, OIL_OPTIONS, ""),
            (WILLING, "number", True, [], ""),
            (NOT_WILLING, "number", True, [], ""),
            (
                CONVERSION,
                "number",
                False,
                [],
                "Auto: total units sold / consumers sampled",
            ),
        ],
    ),
    (
        "Account Feedback",
        [
            (CONTACT, "text", True, [], ""),
            (WAREHOUSE_FEEDBACK, "longtext", True, [], ""),
            (CHANGE_NEXT, "longtext", True, [], ""),
            (NOTES, "longtext", False, [], ""),
        ],
    ),
    (
        "Visit Details",
        [
            (BOOTH, "select", True, BOOTH_OPTIONS, ""),
            (FOOT_TRAFFIC, "select", True, TRAFFIC_OPTIONS, ""),
            (BUSIEST, "select", True, WINDOW_OPTIONS, ""),
            (NEARBY, "text", False, [], ""),
            (ISSUES, "select", True, YES_NO, ""),
            (ISSUE_DETAILS, "longtext", False, [], ""),
        ],
    ),
    (
        "Expenses",
        [
            (
                ACCOUNT_SPEND,
                "number",
                True,
                [],
                "Dollars and cents, like 24.89",
            ),
        ],
    ),
    (
        "Customer Feedback",
        [
            (FLAVOR, "select", True, FLAVOR_OPTIONS, ""),
            (COMMENTS, "longtext", True, [], ""),
            (STORIES, "longtext", True, [], ""),
            (DECLINE, "multiselect", True, DECLINE_OPTIONS, ""),
            (DECLINE_OTHER, "text", False, [], "If you picked other"),
            (USE_IT, "multiselect", True, USE_OPTIONS, ""),
            (DEMOGRAPHICS, "text", True, [], ""),
        ],
    ),
]

PHOTO_BUCKETS: list[dict] = [
    {"name": "Full booth setup, straight-on", "min": 1},
    {"name": "Product display / pallet with price sign visible", "min": 1},
    {"name": "Ambassador at booth in uniform", "min": 1},
    {"name": "Sampling in action with members", "min": 1},
    {"name": "End-of-day remaining inventory", "min": 1},
    {"name": "Expense Receipts", "min": 1},
]

AUTO_FIELDS = (
    UNITS_SOLD_ALMOND,
    UNITS_SOLD_GARLIC,
    TOTAL_UNITS,
    ESTIMATED_SALES,
    CONVERSION,
)

_MONEY_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _money(raw: str | None) -> float | None:
    if raw is None:
        return None
    match = _MONEY_RE.search(str(raw).replace(",", "").replace("$", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _whole(raw: str | None) -> int | None:
    amount = _money(raw)
    if amount is None:
        return None
    return int(amount)


def _sold_for_sku(by_name: dict[str, str], start: str, end: str) -> int | None:
    opening = _whole(by_name.get(start))
    closing = _whole(by_name.get(end))
    if opening is None or closing is None:
        return None
    return opening - closing


def roadshow_derived(by_name: dict[str, str]) -> dict[str, str]:
    """Fill the auto-calc fields from inventory, price, and consumers sampled.

    Missing inputs are omitted so a half-filled recap is not stamped with a
    zero. Conversion is a percent string. Estimated sales is dollars and cents.
    """
    almond = _sold_for_sku(by_name, START_ALMOND, END_ALMOND)
    garlic = _sold_for_sku(by_name, START_GARLIC, END_GARLIC)
    out: dict[str, str] = {}
    if almond is not None:
        out[UNITS_SOLD_ALMOND] = str(almond)
    if garlic is not None:
        out[UNITS_SOLD_GARLIC] = str(garlic)

    parts = [n for n in (almond, garlic) if n is not None]
    total = sum(parts) if parts else None
    if total is not None:
        out[TOTAL_UNITS] = str(total)

    dollars = 0.0
    priced = False
    for sold, price_name in (
        (almond, PRICE_ALMOND),
        (garlic, PRICE_GARLIC),
    ):
        price = _money(by_name.get(price_name))
        if sold is None or price is None:
            continue
        dollars += sold * price
        priced = True
    if priced:
        out[ESTIMATED_SALES] = f"{dollars:.2f}"

    sampled = _whole(by_name.get(CONSUMERS_SAMPLED))
    if total is not None and sampled is not None and sampled > 0:
        out[CONVERSION] = f"{(total / sampled) * 100:.1f}%"
    return out


def inject_roadshow_calcs(*, template, field_values: list) -> list:
    """Write auto-calc answers onto a walk-up submit. No-op for other brands."""
    from recaps.models import CustomField

    template_id = getattr(template, "id", None)
    if not template_id:
        return list(field_values or [])
    fields = list(
        CustomField.objects.filter(custom_recap_template_id=template_id)
    )
    if not any(f.name == TOTAL_UNITS for f in fields):
        return list(field_values or [])

    by_id = {str(f.id): f for f in fields}
    by_name: dict[str, str] = {}
    for fv in field_values or []:
        if not isinstance(fv, dict):
            continue
        raw_id = fv.get("customFieldId") or fv.get("custom_field_id")
        field = by_id.get(str(raw_id or ""))
        if field is None or fv.get("value") is None:
            continue
        by_name[field.name] = str(fv.get("value"))

    derived = roadshow_derived(by_name)
    if not derived:
        return list(field_values or [])

    name_to_id = {f.name: str(f.id) for f in fields}
    out: list = []
    seen: set[str] = set()
    for fv in field_values or []:
        if not isinstance(fv, dict):
            out.append(fv)
            continue
        raw_id = str(fv.get("customFieldId") or fv.get("custom_field_id") or "")
        field = by_id.get(raw_id)
        if field is not None and field.name in derived:
            out.append({**fv, "customFieldId": raw_id, "value": derived[field.name]})
            seen.add(field.name)
        else:
            out.append(fv)
    for name, value in derived.items():
        if name in seen or value == "":
            continue
        fid = name_to_id.get(name)
        if fid:
            out.append({"customFieldId": fid, "value": value})
    return out
