"""What changed: the selected window against the one before it.

One or two sentences under the KPIs. Every figure and every "mostly from"
is worked out here; :mod:`recaps.explain_text` only words the result.

Says nothing (returns None) when either window is too thin to compare:
fewer than :data:`MIN_RECAPS` approved recaps with consumers sampled on
either side, the same floor the period-comparison chips use.
"""

from __future__ import annotations

from datetime import date

from recaps.explain_facts import (
    previous_window,
    product_facts,
    recap_facts,
    state_names,
)
from recaps.explain_text import Draft, n, pct, plural
from recaps.tenant_overview import SPARSE_BASE_MIN_RECAPS

# Fewer approved recaps than this in either window: say nothing.
MIN_RECAPS = SPARSE_BASE_MIN_RECAPS
# A move smaller than this reads as "about level".
LEVEL_PCT = 5
# A state, a store or a group of new stores is named as the reason only
# when it accounts for at least this share of the whole move.
MOSTLY_SHARE = 0.5
# Conversion is mentioned only when it moved by at least this many points.
CONVERSION_MIN_POINTS = 2.0
# A store is named for a conversion move only with this much behind it, in
# both windows.
STORE_MIN_RECAPS = 3
STORE_MIN_BASE = 30
# A product is mentioned only with this many samples in one of the windows
# and a move of at least this size.
PRODUCT_MIN_SAMPLES = 50
PRODUCT_MIN_PCT = 25


def _conv(units: int, base: int) -> float | None:
    return round(units / base * 100, 1) if base > 0 else None


def _sum_by(rows: list[dict], key) -> dict:
    out: dict = {}
    for row in rows:
        k = key(row)
        out[k] = out.get(k, 0) + row["consumers"]
    return out


def _store_id(row: dict) -> tuple[str, str]:
    """One physical store: the retailer name plus the street address."""
    return row["store"].casefold(), row["place"]


def _driver(cur: list[dict], prev: list[dict], delta: int) -> dict | None:
    """What accounts for most of the move in consumers sampled, or None."""
    up = delta > 0

    def share(part: int) -> float:
        return part / delta if delta else 0.0

    cur_state, prev_state = _sum_by(cur, lambda r: r["state"]), _sum_by(prev, lambda r: r["state"])
    cur_place, prev_place = _sum_by(cur, _store_id), _sum_by(prev, _store_id)
    place_state = {_store_id(r): r["state"] for r in prev}
    place_state.update({_store_id(r): r["state"] for r in cur})
    # A recap with neither a store name nor an address is not a store.
    known = [p for p in set(cur_place) | set(prev_place) if p[0] or p[1]]
    new = [p for p in known if cur_place.get(p, 0) > 0 and prev_place.get(p, 0) == 0]
    gone = [p for p in known if prev_place.get(p, 0) > 0 and cur_place.get(p, 0) == 0]
    changed = new if up else gone

    def changed_delta(places) -> int:
        return sum(cur_place.get(p, 0) - prev_place.get(p, 0) for p in places)

    states = {s for s in set(cur_state) | set(prev_state) if s}
    if len(states) >= 2:
        deltas = {s: cur_state.get(s, 0) - prev_state.get(s, 0) for s in states}
        top = max(deltas, key=lambda s: (deltas[s] if up else -deltas[s], s))
        if share(deltas[top]) >= MOSTLY_SHARE:
            in_state = [p for p in changed if place_state.get(p) == top]
            part = changed_delta(in_state)
            if in_state and deltas[top] and part / deltas[top] >= MOSTLY_SHARE:
                return {
                    "kind": "new_stores_in_state" if up else "gone_stores_in_state",
                    "state": top,
                    "stores": len(in_state),
                    "amount": abs(part),
                }
            return {"kind": "state", "state": top, "amount": abs(deltas[top])}

    cur_store, prev_store = _sum_by(cur, lambda r: r["store"].casefold()), _sum_by(prev, lambda r: r["store"].casefold())
    labels = {r["store"].casefold(): r["store"] for r in prev + cur if r["store"]}
    if len(labels) >= 2:
        deltas = {s: cur_store.get(s, 0) - prev_store.get(s, 0) for s in labels}
        top = max(deltas, key=lambda s: (deltas[s] if up else -deltas[s], s))
        if share(deltas[top]) >= MOSTLY_SHARE:
            return {"kind": "store", "store": labels[top], "amount": abs(deltas[top])}

    part = changed_delta(changed)
    if changed and share(part) >= MOSTLY_SHARE:
        return {
            "kind": "new_stores" if up else "gone_stores",
            "stores": len(changed),
            "amount": abs(part),
        }
    return None


def _conversion_move(cur: list[dict], prev: list[dict]) -> dict | None:
    def counted(rows):
        return [r for r in rows if r["base"]]

    cur_c, prev_c = counted(cur), counted(prev)
    if len(cur_c) < MIN_RECAPS or len(prev_c) < MIN_RECAPS:
        return None
    now = _conv(sum(r["units"] for r in cur_c), sum(r["base"] for r in cur_c))
    before = _conv(sum(r["units"] for r in prev_c), sum(r["base"] for r in prev_c))
    if now is None or before is None:
        return None
    points = round(now - before, 1)
    if abs(points) < CONVERSION_MIN_POINTS:
        return None
    up = points > 0

    def per_store(rows):
        out: dict[str, dict] = {}
        for r in rows:
            if not r["store"]:
                continue
            b = out.setdefault(
                r["store"].casefold(),
                {"label": r["store"], "recaps": 0, "base": 0, "units": 0},
            )
            b["recaps"] += 1
            b["base"] += r["base"]
            b["units"] += r["units"]
        return out

    cur_s, prev_s = per_store(cur_c), per_store(prev_c)
    best = None
    for key, c in cur_s.items():
        p = prev_s.get(key)
        if p is None:
            continue
        if min(c["recaps"], p["recaps"]) < STORE_MIN_RECAPS:
            continue
        if min(c["base"], p["base"]) < STORE_MIN_BASE:
            continue
        c_rate, p_rate = _conv(c["units"], c["base"]), _conv(p["units"], p["base"])
        change = round(c_rate - p_rate, 1)
        if (change > 0) != up or abs(change) < CONVERSION_MIN_POINTS:
            continue
        weight = abs(change) * c["base"]
        if best is None or weight > best["weight"]:
            best = {
                "store": c["label"],
                "now": c_rate,
                "before": p_rate,
                "weight": weight,
            }
    return {"now": now, "before": before, "up": up, "store": best}


def _product_move(cur: list[dict], prev: list[dict]) -> dict | None:
    before = {p["label"]: p["samples"] for p in prev}
    best = None
    for product in cur:
        was = before.get(product["label"], 0)
        now = product["samples"]
        if was <= 0 or max(was, now) < PRODUCT_MIN_SAMPLES:
            continue
        change = round((now - was) / was * 100)
        if abs(change) < PRODUCT_MIN_PCT:
            continue
        if best is None or abs(now - was) > abs(best["now"] - best["before"]):
            best = {"label": product["label"], "now": now, "before": was, "pct": change}
    return best


def compute_what_changed(tenant_id: int, start: date | None, end: date | None) -> dict | None:
    """The figures behind the sentence, or None when there is nothing
    honest to say (no window, or too few approved recaps on either side)."""
    if start is None or end is None or end < start:
        return None
    prev_start, prev_end = previous_window(start, end)
    cur_all = recap_facts(tenant_id, start, end)
    prev_all = recap_facts(tenant_id, prev_start, prev_end)
    cur = [r for r in cur_all if r["consumers"] is not None]
    prev = [r for r in prev_all if r["consumers"] is not None]
    if len(cur) < MIN_RECAPS or len(prev) < MIN_RECAPS:
        return None
    now = sum(r["consumers"] for r in cur)
    before = sum(r["consumers"] for r in prev)
    if before <= 0:
        return None
    change = round((now - before) / before * 100)
    level = abs(change) < LEVEL_PCT
    conversion = _conversion_move(cur_all, prev_all)
    product = None
    if conversion is None:
        product = _product_move(
            product_facts(tenant_id, start, end),
            product_facts(tenant_id, prev_start, prev_end),
        )
    return {
        "days": (end - start).days + 1,
        "previous_start": prev_start,
        "previous_end": prev_end,
        "consumers": now,
        "consumers_before": before,
        "pct": change,
        "level": level,
        "recaps": len(cur),
        "recaps_before": len(prev),
        "driver": None if level else _driver(cur, prev, now - before),
        "conversion": conversion,
        "product": product,
    }


def _driver_phrase(driver: dict, names: dict[str, str], up: bool) -> tuple[str, str]:
    """(the clause for the sentence, the line for the facts)."""
    kind = driver["kind"]
    amount = n(driver["amount"])
    sign = "added" if up else "lost"
    if kind in ("new_stores_in_state", "gone_stores_in_state"):
        state = names.get(driver["state"], driver["state"])
        stores = plural(driver["stores"], "new store" if up else "store")
        if up:
            return (
                f"mostly from {stores} in {state}",
                f"Main reason: {stores} in {state} {sign} {amount}.",
            )
        return (
            f"mostly in {state}, where {stores} had no approved recap this time",
            f"Main reason: {stores} in {state} with no approved recap this time {sign} {amount}.",
        )
    if kind == "state":
        state = names.get(driver["state"], driver["state"])
        return f"mostly in {state}", f"Main reason: {state} {sign} {amount}."
    if kind == "store":
        return (
            f"mostly at {driver['store']}",
            f"Main reason: {driver['store']} {sign} {amount}.",
        )
    stores = plural(driver["stores"], "new store" if up else "store")
    if up:
        return f"mostly from {stores}", f"Main reason: {stores} {sign} {amount}."
    return (
        f"mostly because {stores} had no approved recap this time",
        f"Main reason: {stores} with no approved recap this time {sign} {amount}.",
    )


def draft_what_changed(facts: dict) -> Draft:
    """The template sentence(s) and the fact lines for ``facts``."""
    days = facts["days"]
    now, before = facts["consumers"], facts["consumers_before"]
    change = facts["pct"]
    up = change > 0
    names = {}
    driver = facts["driver"]
    if driver and driver.get("state"):
        names = state_names([driver["state"]])

    lines = [
        f"This period is {days} days. It is compared with the {days} days before it.",
        f"Consumers sampled: {n(now)} this period, {n(before)} before.",
    ]
    directions: dict[str, str] = {}
    if facts["level"]:
        first = (
            f"Consumers sampled is about level with the {days} days before "
            f"({n(now)} against {n(before)})."
        )
        lines.append("Consumers sampled is about level.")
    else:
        word = "up" if up else "down"
        directions["consumers"] = word
        lines.append(f"Consumers sampled is {word} {abs(change)}%.")
        first = (
            f"Consumers sampled is {word} {abs(change)}% on the {days} days before "
            f"({n(now)} against {n(before)})"
        )
        if driver:
            clause, line = _driver_phrase(driver, names, up)
            first += f", {clause}"
            lines.append(line)
        first += "."

    second = ""
    conversion = facts["conversion"]
    product = facts["product"]
    if conversion:
        word = "rose" if conversion["up"] else "fell"
        directions["conversion"] = "up" if conversion["up"] else "down"
        second = (
            f"Conversion rate {word} from {pct(conversion['before'])}% "
            f"to {pct(conversion['now'])}%"
        )
        lines.append(
            f"Conversion rate: {pct(conversion['before'])}% before, "
            f"{pct(conversion['now'])}% this period. It {word}."
        )
        store = conversion["store"]
        if store:
            second += (
                f", most at {store['store']} "
                f"({pct(store['before'])}% to {pct(store['now'])}%)"
            )
            lines.append(
                f"Conversion rate at {store['store']}: {pct(store['before'])}% "
                f"before, {pct(store['now'])}% this period."
            )
        second += "."
    elif product:
        word = "up" if product["pct"] > 0 else "down"
        directions["samples"] = word
        second = (
            f"Samples of {product['label']} handed out are {word} "
            f"{abs(product['pct'])}% ({n(product['now'])} against {n(product['before'])})."
        )
        lines.append(
            f"Samples of {product['label']} handed out: {n(product['now'])} this "
            f"period, {n(product['before'])} before, {word} {abs(product['pct'])}%."
        )

    template = f"{first} {second}".strip()
    return Draft(
        key="what_changed",
        template=template,
        facts="\n".join(lines),
        directions=directions,
    )
