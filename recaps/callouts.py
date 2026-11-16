"""Callouts: the few things in a program that stand out, found by rule.

Each rule has a threshold and a minimum amount of data behind it, so one
small event cannot raise a callout. All of it is arithmetic done here;
:mod:`recaps.explain_text` only words each sentence.

The four rules (constants below hold every threshold):

1. A store converting far above or below the brand's average.
2. A state that was active and has had no approved recap since.
3. A product with samples handed out and almost nothing sold, where the
   recaps log sales per product.
4. A sharp week-over-week drop in consumers sampled.

At most :data:`MAX_CALLOUTS` are returned, most significant first.
"""

from __future__ import annotations

from datetime import date, timedelta

from recaps.consumer_breakdown import week_start
from recaps.explain_facts import product_facts, recap_facts, state_names
from recaps.explain_text import Draft, n, pct, plural

MAX_CALLOUTS = 4
# With no dates picked, stores and products are judged on this many days.
DEFAULT_DAYS = 90

# 1. Store conversion.
BRAND_MIN_RECAPS = 10  # recaps in the brand's rate before any store is judged
BRAND_MIN_STORES = 3  # stores with a rate, so "average" means something
STORE_MIN_RECAPS = 3
STORE_MIN_BASE = 50  # people sampled at the store
STORE_HIGH_RATIO = 1.5  # at least 1.5x the brand's rate
STORE_LOW_RATIO = 0.5  # at most half the brand's rate
STORE_MIN_POINTS = 5.0  # and at least 5 points away from it
# A store with more than this share of everyone sampled IS the average.
STORE_MAX_SHARE = 0.5

# 2. A state gone quiet.
QUIET_WEEKS = 4  # no approved recap for this long
ACTIVE_WEEKS = 8  # judged against the 8 weeks before that
ACTIVE_MIN_RECAPS = 3  # recaps in those 8 weeks
PROGRAM_MIN_RECENT_RECAPS = 3  # the brand itself is still filing

# 3. Product with samples and almost no sales.
PRODUCT_MIN_SAMPLES = 100
PRODUCT_MIN_RECAPS = 3  # recaps with the product's own sold line
PRODUCT_MAX_SOLD_PCT = 2.0

# 4. Week-over-week drop.
DROP_MIN_PCT = 40
DROP_MIN_RECAPS = 3  # in each of the two weeks
DROP_MIN_CONSUMERS = 100  # in the earlier week


def _rate(units: int, base: int) -> float:
    return round(units / base * 100, 1)


def _day(day: date) -> str:
    """November 2."""
    return f"{day:%B} {day.day}"


def _number(label: str, value) -> dict:
    return {"label": label, "value": str(value)}


def _store_callouts(rows: list[dict], start: date, end: date) -> list[dict]:
    counted = [r for r in rows if r["base"]]
    if len(counted) < BRAND_MIN_RECAPS:
        return []
    base = sum(r["base"] for r in counted)
    units = sum(r["units"] for r in counted)
    if base <= 0 or units <= 0:
        return []
    average = _rate(units, base)
    stores: dict[str, dict] = {}
    for r in counted:
        if not r["store"]:
            continue
        s = stores.setdefault(
            r["store"].casefold(),
            {"label": r["store"], "recaps": 0, "base": 0, "units": 0},
        )
        s["recaps"] += 1
        s["base"] += r["base"]
        s["units"] += r["units"]
    if len(stores) < BRAND_MIN_STORES:
        return []

    high = low = None
    for s in stores.values():
        if s["recaps"] < STORE_MIN_RECAPS or s["base"] < STORE_MIN_BASE:
            continue
        if s["base"] > base * STORE_MAX_SHARE:
            continue
        rate = _rate(s["units"], s["base"])
        gap = round(rate - average, 1)
        if abs(gap) < STORE_MIN_POINTS:
            continue
        found = {**s, "rate": rate, "gap": gap}
        if rate >= average * STORE_HIGH_RATIO and (high is None or gap > high["gap"]):
            high = found
        if rate <= average * STORE_LOW_RATIO and (low is None or gap < low["gap"]):
            low = found

    out = []
    for found, word in ((high, "above"), (low, "below")):
        if found is None:
            continue
        recaps = plural(found["recaps"], "approved recap")
        out.append(
            {
                "kind": f"store_{word}_average",
                "key": f"store:{found['label'].casefold()}:{word}",
                "severity": min(3.0, abs(found["gap"]) / average)
                * min(1.0, found["recaps"] / 6),
                "template": (
                    f"{found['label']} converted at {pct(found['rate'])}% across "
                    f"{recaps}, well {word} the {pct(average)}% average."
                ),
                "facts": (
                    f"Store: {found['label']}.\n"
                    f"Its conversion rate: {pct(found['rate'])}% "
                    f"({n(found['units'])} sold, {n(found['base'])} sampled, {recaps}).\n"
                    f"Brand average conversion rate: {pct(average)}%.\n"
                    f"The store is {word} the average."
                ),
                "numbers": [
                    _number("Conversion rate", f"{pct(found['rate'])}%"),
                    _number("Brand average", f"{pct(average)}%"),
                    _number("Units sold", n(found["units"])),
                    _number("People sampled", n(found["base"])),
                    _number("Approved recaps", n(found["recaps"])),
                ],
                "link": {
                    "store": found["label"],
                    "state": None,
                    "start": start,
                    "end": end,
                },
            }
        )
    return out


def _quiet_state_callouts(tenant_id: int, anchor: date) -> list[dict]:
    quiet_from = anchor - timedelta(weeks=QUIET_WEEKS) + timedelta(days=1)
    active_from = quiet_from - timedelta(weeks=ACTIVE_WEEKS)
    rows = recap_facts(tenant_id, active_from, anchor)
    recent = [r for r in rows if r["day"] >= quiet_from]
    if len(recent) < PROGRAM_MIN_RECENT_RECAPS:
        return []  # the whole program is quiet: not one state's story
    active: dict[str, dict] = {}
    for r in rows:
        if not r["state"] or r["day"] >= quiet_from:
            continue
        s = active.setdefault(r["state"], {"recaps": 0, "consumers": 0, "last": r["day"]})
        s["recaps"] += 1
        s["consumers"] += r["consumers"] or 0
        s["last"] = max(s["last"], r["day"])
    still = {r["state"] for r in recent}
    names = state_names(active)
    out = []
    for code, s in active.items():
        if code in still or s["recaps"] < ACTIVE_MIN_RECAPS:
            continue
        weeks = (anchor - s["last"]).days // 7
        name = names.get(code, code)
        recaps = plural(s["recaps"], "approved recap")
        out.append(
            {
                "kind": "state_quiet",
                "key": f"state:{code}",
                "severity": 1.0 + min(1.0, s["recaps"] / 10),
                "template": (
                    f"{name} has had no approved recap for {plural(weeks, 'week')}. "
                    f"It had {recaps} in the {ACTIVE_WEEKS} weeks before that."
                ),
                "facts": (
                    f"State: {name}.\n"
                    f"Weeks since its last approved recap: {weeks}.\n"
                    f"Approved recaps in the {ACTIVE_WEEKS} weeks before: {s['recaps']}.\n"
                    f"Consumers sampled in those weeks: {n(s['consumers'])}."
                ),
                "numbers": [
                    _number("Last approved recap", _day(s["last"])),
                    _number(f"Recaps in the {ACTIVE_WEEKS} weeks before", n(s["recaps"])),
                    _number("Consumers sampled then", n(s["consumers"])),
                ],
                "link": {
                    "store": None,
                    "state": code,
                    "start": active_from,
                    "end": anchor,
                },
            }
        )
    return out


def _product_callouts(tenant_id: int, start: date, end: date) -> list[dict]:
    out = []
    for product in product_facts(tenant_id, start, end):
        if product["sold"] is None:
            continue  # sales are not logged per product: nothing to compare
        if product["samples"] < PRODUCT_MIN_SAMPLES:
            continue
        if product["sold_recaps"] < PRODUCT_MIN_RECAPS:
            continue
        share = round(product["sold"] / product["samples"] * 100, 1)
        if share > PRODUCT_MAX_SOLD_PCT:
            continue
        recaps = plural(product["sold_recaps"], "approved recap")
        out.append(
            {
                "kind": "product_no_sales",
                "key": f"product:{product['label'].casefold()}",
                "severity": 1.0 + min(1.0, product["samples"] / 1000),
                "template": (
                    f"{product['label']}: {n(product['samples'])} samples handed "
                    f"out and {n(product['sold'])} sold across {recaps}."
                ),
                "facts": (
                    f"Product: {product['label']}.\n"
                    f"Samples handed out: {n(product['samples'])}.\n"
                    f"Units sold: {n(product['sold'])}, from {recaps}."
                ),
                "numbers": [
                    _number("Samples handed out", n(product["samples"])),
                    _number("Units sold", n(product["sold"])),
                    _number("Approved recaps with a sold count", n(product["sold_recaps"])),
                ],
                "link": {"store": None, "state": None, "start": start, "end": end},
            }
        )
    return out[:1]


def _week_drop_callouts(tenant_id: int, anchor: date) -> list[dict]:
    # The last full Monday-to-Sunday week, and the one before it.
    # The week in progress is never judged: its recaps are still coming in.
    last_monday = week_start(anchor) - timedelta(days=7)
    earlier_monday = last_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    rows = recap_facts(tenant_id, earlier_monday, last_sunday)

    def week(monday: date) -> tuple[int, int]:
        mine = [
            r
            for r in rows
            if r["consumers"] is not None and week_start(r["day"]) == monday
        ]
        return len(mine), sum(r["consumers"] for r in mine)

    last_recaps, last_total = week(last_monday)
    earlier_recaps, earlier_total = week(earlier_monday)
    if min(last_recaps, earlier_recaps) < DROP_MIN_RECAPS:
        return []
    if earlier_total < DROP_MIN_CONSUMERS:
        return []
    drop = round((earlier_total - last_total) / earlier_total * 100)
    if drop < DROP_MIN_PCT:
        return []
    return [
        {
            "kind": "week_drop",
            "key": f"week:{last_monday.isoformat()}",
            "severity": drop / DROP_MIN_PCT,
            "template": (
                f"Consumers sampled fell {drop}% in the week of "
                f"{_day(last_monday)}, to {n(last_total)} from "
                f"{n(earlier_total)} the week before."
            ),
            "facts": (
                f"Week of {_day(last_monday)}: {n(last_total)} consumers "
                f"sampled, {plural(last_recaps, 'approved recap')}.\n"
                f"Week of {_day(earlier_monday)}: {n(earlier_total)} consumers "
                f"sampled, {plural(earlier_recaps, 'approved recap')}.\n"
                f"Consumers sampled fell {drop}% from one week to the next."
            ),
            "directions": {"consumers": "down"},
            "numbers": [
                _number(f"Week of {_day(last_monday)}", n(last_total)),
                _number(f"Week of {_day(earlier_monday)}", n(earlier_total)),
                _number("Approved recaps, latest week", n(last_recaps)),
                _number("Approved recaps, week before", n(earlier_recaps)),
            ],
            "link": {
                "store": None,
                "state": None,
                "start": last_monday,
                "end": last_sunday,
            },
        }
    ]


def compute_callouts(
    tenant_id: int, start: date | None, end: date | None, today: date
) -> list[dict]:
    """Every callout the rules raise for this window, most significant
    first, capped at :data:`MAX_CALLOUTS`."""
    anchor = min(end, today) if end is not None else today
    if start is None or end is None:
        start, end = anchor - timedelta(days=DEFAULT_DAYS - 1), anchor
    found = []
    found += _store_callouts(recap_facts(tenant_id, start, end), start, end)
    found += _quiet_state_callouts(tenant_id, anchor)
    found += _product_callouts(tenant_id, start, end)
    found += _week_drop_callouts(tenant_id, anchor)
    found.sort(key=lambda c: (-c["severity"], c["key"]))
    return found[:MAX_CALLOUTS]


def draft_callouts(callouts: list[dict]) -> list[Draft]:
    return [
        Draft(
            key=c["key"],
            template=c["template"],
            facts=c["facts"],
            directions=c.get("directions", {}),
        )
        for c in callouts
    ]
