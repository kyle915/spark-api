"""The findings, worked out in code. Pure functions over visit rows.

Each finding carries a finished sentence (``template``) and the lines of
numbers it was built from (``facts_text``). The model may reword the
sentence later and nothing else (:mod:`brand_insights.service`).

Recommendations ("where to go next"), over the last :data:`LOOKBACK_DAYS`:

* ``top_store``: a store that converted at least :data:`TOP_MIN_RATIO`
  times the brand's rate, with at least :data:`STORE_MIN_VISITS` visits and
  :data:`STORE_MIN_SAMPLED` people sampled in the rate, so one good day
  does not make a top store. At most :data:`TOP_MAX`.
* ``low_sales``: a store that samples well above the brand's per-visit
  figure but converts at most :data:`LOW_MAX_RATIO` of its rate: check price
  and placement. Same minimums. At most :data:`LOW_MAX`.
* ``repeat_no_product``: a store where the product was not there on at
  least :data:`REPEAT_MIN` visits in the last :data:`NO_PRODUCT_DAYS` days.

The brand's rate needs :data:`BRAND_MIN_RATED` visits behind it before any
store is compared with it. Visits without product are left out of every
rate (Kyle's rule) and only feed ``repeat_no_product`` / ``no_product_week``.

Alerts, about one finished week (Monday to Sunday):

* ``no_product_week``: the product was not found at one or more stores.
* ``conversion_drop``: the brand's conversion over the
  :data:`DROP_DAYS` days to that Sunday fell to at most
  :data:`DROP_MAX_RATIO` of the :data:`DROP_DAYS` days before, and by at
  least :data:`DROP_MIN_POINTS` points. Each side needs
  :data:`BRAND_DROP_MIN_RECAPS` visits.
* ``chain_conversion_drop``: the same for one retailer, with
  :data:`DROP_MIN_RECAPS` visits and :data:`DROP_MIN_SAMPLED` people sampled
  on each side. At most :data:`DROP_MAX`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from brand_insights.facts import Visit
from recaps.explain_text import n, pct, plural

LOOKBACK_DAYS = 90
NO_PRODUCT_DAYS = 30

BRAND_MIN_RATED = 10
STORE_MIN_VISITS = 2
STORE_MIN_SAMPLED = 60

TOP_MIN_RATIO = 1.5
TOP_MAX = 5

LOW_MAX_RATIO = 0.5
LOW_MIN_SAMPLED_RATIO = 1.25
LOW_MAX = 3

REPEAT_MIN = 2
REPEAT_MAX = 5

DROP_DAYS = 28
BRAND_DROP_MIN_RECAPS = 10
DROP_MIN_RECAPS = 3
DROP_MIN_SAMPLED = 100
DROP_MAX_RATIO = 0.7
DROP_MIN_POINTS = 5.0
DROP_MAX = 3

MISSING_LIST_MAX = 8

KIND_RECOMMENDATION = "recommendation"
KIND_ALERT = "alert"

CODE_TOP_STORE = "top_store"
CODE_LOW_SALES = "low_sales"
CODE_REPEAT_NO_PRODUCT = "repeat_no_product"
CODE_NO_PRODUCT_WEEK = "no_product_week"
CODE_CONVERSION_DROP = "conversion_drop"
CODE_CHAIN_CONVERSION_DROP = "chain_conversion_drop"

# Recommendations the card lists under "Go back" and under "Fix".
GO_BACK_CODES = (CODE_TOP_STORE,)
FIX_CODES = (CODE_REPEAT_NO_PRODUCT, CODE_LOW_SALES)


@dataclass
class Finding:
    kind: str
    code: str
    scope_key: str
    subject: str
    rank: int
    template: str
    facts_text: str
    facts: dict
    directions: dict[str, str] = field(default_factory=dict)


def rate(units: int, base: int) -> float:
    return round(units / base * 100, 1) if base else 0.0


def _ratio(a: float, b: float) -> str:
    return f"{a / b:.1f}"


@dataclass
class _Tally:
    key: str
    label: str
    visits: int = 0
    rated: int = 0
    base: int = 0
    units: int = 0
    no_product_days: list[date] = field(default_factory=list)
    shelf_checked: int = 0
    price_tag_seen: int = 0

    def add(self, v: Visit) -> None:
        self.visits += 1
        if v.no_product:
            self.no_product_days.append(v.day)
            return
        if v.price_tag:
            self.shelf_checked += 1
            if v.price_tag == "yes":
                self.price_tag_seen += 1
        if v.rated:
            self.rated += 1
            self.base += int(v.base or 0)
            self.units += int(v.units or 0)

    @property
    def rate(self) -> float:
        return rate(self.units, self.base)


def _brand_rate(visits: list[Visit], min_rated: int) -> tuple[float | None, int, int, int]:
    """``(rate or None, rated visits, base, units)``."""
    rated = [v for v in visits if v.rated]
    base = sum(int(v.base or 0) for v in rated)
    units = sum(int(v.units or 0) for v in rated)
    if len(rated) < min_rated or base <= 0 or units <= 0:
        return None, len(rated), base, units
    return rate(units, base), len(rated), base, units


def _by_store(visits: list[Visit]) -> dict[str, _Tally]:
    stores: dict[str, _Tally] = {}
    for v in visits:
        if not v.store_key:
            continue
        stores.setdefault(v.store_key, _Tally(v.store_key, v.store_label)).add(v)
    return stores


def recommendations(visits: list[Visit], today: date) -> list[Finding]:
    """Where to go back to and what to fix, from the last
    :data:`LOOKBACK_DAYS` days of visits (the caller's window)."""
    out: list[Finding] = []
    average, rated_n, brand_base, _units = _brand_rate(visits, BRAND_MIN_RATED)
    stores = _by_store(visits)

    if average:
        eligible = [
            s for s in stores.values() if s.rated >= STORE_MIN_VISITS and s.base >= STORE_MIN_SAMPLED
        ]
        top = sorted(
            (s for s in eligible if s.rate >= average * TOP_MIN_RATIO),
            key=lambda s: (-s.rate, -s.base, s.label.casefold()),
        )[:TOP_MAX]
        for i, s in enumerate(top):
            times = _ratio(s.rate, average)
            out.append(
                Finding(
                    kind=KIND_RECOMMENDATION,
                    code=CODE_TOP_STORE,
                    scope_key=f"store:{s.key}",
                    subject=s.label,
                    rank=i,
                    template=(
                        f"{s.label} converted {pct(s.rate)}% over "
                        f"{plural(s.rated, 'visit')}, {times}x your {pct(average)}% "
                        f"average. Worth going back."
                    ),
                    facts_text=(
                        f"Store: {s.label}.\n"
                        f"Its conversion rate: {pct(s.rate)}% ({n(s.units)} sold, "
                        f"{n(s.base)} sampled, {plural(s.rated, 'visit')} with product).\n"
                        f"Brand average conversion rate: {pct(average)}% over "
                        f"{plural(rated_n, 'visit')}.\n"
                        f"Its rate divided by the average: {times}.\n"
                        f"Window: the last {LOOKBACK_DAYS} days."
                    ),
                    facts={
                        "store": s.label,
                        "conversion_percent": s.rate,
                        "units_sold": s.units,
                        "sampled": s.base,
                        "visits": s.rated,
                        "brand_conversion_percent": average,
                        "brand_visits": rated_n,
                        "times_average": float(times),
                        "window_days": LOOKBACK_DAYS,
                    },
                )
            )

        per_visit = brand_base / rated_n if rated_n else 0
        low = sorted(
            (
                s
                for s in eligible
                if s.rate <= average * LOW_MAX_RATIO
                and s.base / s.rated >= per_visit * LOW_MIN_SAMPLED_RATIO
            ),
            key=lambda s: (s.rate, -s.base, s.label.casefold()),
        )[:LOW_MAX]
        for i, s in enumerate(low):
            store_per_visit = round(s.base / s.rated)
            brand_per_visit = round(per_visit)
            tag = ""
            tag_line = ""
            if s.shelf_checked and s.price_tag_seen < s.shelf_checked:
                tag = (
                    f" A price tag was seen on {n(s.price_tag_seen)} of "
                    f"{plural(s.shelf_checked, 'shelf-checked visit')}."
                )
                tag_line = (
                    f"\nShelf-checked visits: {s.shelf_checked}; with a price tag "
                    f"seen: {s.price_tag_seen}."
                )
            out.append(
                Finding(
                    kind=KIND_RECOMMENDATION,
                    code=CODE_LOW_SALES,
                    scope_key=f"store:{s.key}",
                    subject=s.label,
                    rank=i,
                    template=(
                        f"{s.label} sampled {n(store_per_visit)} people a visit, "
                        f"against your {n(brand_per_visit)}, but converted "
                        f"{pct(s.rate)}% against your {pct(average)}% average. "
                        f"Check price and placement.{tag}"
                    ),
                    facts_text=(
                        f"Store: {s.label}.\n"
                        f"People sampled per visit there: {store_per_visit} "
                        f"({n(s.base)} over {plural(s.rated, 'visit')}).\n"
                        f"Brand people sampled per visit: {brand_per_visit}.\n"
                        f"Its conversion rate: {pct(s.rate)}% ({n(s.units)} sold).\n"
                        f"Brand average conversion rate: {pct(average)}%."
                        f"{tag_line}\n"
                        f"Window: the last {LOOKBACK_DAYS} days."
                    ),
                    facts={
                        "store": s.label,
                        "sampled_per_visit": store_per_visit,
                        "brand_sampled_per_visit": brand_per_visit,
                        "conversion_percent": s.rate,
                        "units_sold": s.units,
                        "sampled": s.base,
                        "visits": s.rated,
                        "brand_conversion_percent": average,
                        "shelf_checked_visits": s.shelf_checked,
                        "price_tag_seen_visits": s.price_tag_seen,
                        "window_days": LOOKBACK_DAYS,
                    },
                )
            )

    since = today - timedelta(days=NO_PRODUCT_DAYS - 1)
    repeat = []
    for s in stores.values():
        recent = [d for d in s.no_product_days if d >= since]
        if len(recent) >= REPEAT_MIN:
            repeat.append((s, recent))
    repeat.sort(key=lambda pair: (-len(pair[1]), -max(pair[1]).toordinal(), pair[0].label.casefold()))
    for i, (s, recent) in enumerate(repeat[:REPEAT_MAX]):
        last = max(recent)
        out.append(
            Finding(
                kind=KIND_RECOMMENDATION,
                code=CODE_REPEAT_NO_PRODUCT,
                scope_key=f"store:{s.key}",
                subject=s.label,
                rank=i,
                template=(
                    f"No product on the shelf at {s.label} on "
                    f"{plural(len(recent), 'visit')} in the last {NO_PRODUCT_DAYS} days. "
                    f"Check stock and distribution before the next visit."
                ),
                facts_text=(
                    f"Store: {s.label}.\n"
                    f"Visits with no product in the last {NO_PRODUCT_DAYS} days: {len(recent)}.\n"
                    f"Most recent one: {last.strftime('%b %-d')}."
                ),
                facts={
                    "store": s.label,
                    "no_product_visits": len(recent),
                    "window_days": NO_PRODUCT_DAYS,
                    "last_no_product": last.isoformat(),
                },
            )
        )
    return out


def alerts(visits: list[Visit], week_start: date, week_end: date) -> list[Finding]:
    """Notices about the week ``week_start``..``week_end``. ``visits`` must
    cover the :data:`DROP_DAYS` * 2 days up to ``week_end``."""
    out: list[Finding] = []

    in_week = [v for v in visits if week_start <= v.day <= week_end]
    missing: dict[str, str] = {}
    for v in sorted(in_week, key=lambda v: v.day):
        if not v.no_product:
            continue
        key = v.store_key or (f"chain:{v.chain_key}" if v.chain_key else "")
        label = v.store_label or v.chain_label
        if key and label:
            missing.setdefault(key, label)
    if missing:
        names = sorted(set(missing.values()), key=str.casefold)
        shown = names[:MISSING_LIST_MAX]
        rest = len(names) - len(shown)
        # Semicolons: a store name can hold a comma ("Sprouts (Austin, TX)").
        listed = "; ".join(shown) + (f"; and {n(rest)} more" if rest else "")
        count = len(names)
        out.append(
            Finding(
                kind=KIND_ALERT,
                code=CODE_NO_PRODUCT_WEEK,
                scope_key="brand",
                subject="",
                rank=0,
                template=(
                    f"Product not found at {plural(count, 'store')} last week: {listed}."
                ),
                facts_text=(
                    f"Stores where visits found no product last week: {count}.\n"
                    f"Their names: {listed}."
                ),
                facts={"stores": names, "store_count": count},
            )
        )

    cur_start = week_end - timedelta(days=DROP_DAYS - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=DROP_DAYS - 1)
    current = [v for v in visits if cur_start <= v.day <= week_end]
    previous = [v for v in visits if prev_start <= v.day <= prev_end]
    weeks = DROP_DAYS // 7

    def dropped(cur: list[Visit], prev: list[Visit], min_recaps: int, min_sampled: int):
        now, now_n, now_base, now_units = _brand_rate(cur, min_recaps)
        was, was_n, was_base, was_units = _brand_rate(prev, min_recaps)
        if now is None or was is None or now_base < min_sampled or was_base < min_sampled:
            # A rate of 0 is a real drop too, when there was volume.
            if was is not None and now is None and now_n >= min_recaps and now_base >= min_sampled:
                now = 0.0
            else:
                return None
        if now > was * DROP_MAX_RATIO or was - now < DROP_MIN_POINTS:
            return None
        return {
            "now": now,
            "was": was,
            "now_visits": now_n,
            "was_visits": was_n,
            "now_sampled": now_base,
            "was_sampled": was_base,
            "now_sold": now_units,
            "was_sold": was_units,
        }

    def drop_finding(code, scope, subject, rank, who, d) -> Finding:
        return Finding(
            kind=KIND_ALERT,
            code=code,
            scope_key=scope,
            subject=subject,
            rank=rank,
            template=(
                f"Conversion{who} dropped from {pct(d['was'])}% to {pct(d['now'])}% "
                f"in the last {weeks} weeks, compared with the {weeks} weeks before."
            ),
            facts_text=(
                f"Conversion{who}, last {weeks} weeks: {pct(d['now'])}% "
                f"({n(d['now_sold'])} sold, {n(d['now_sampled'])} sampled, "
                f"{plural(d['now_visits'], 'visit')}).\n"
                f"Conversion{who}, the {weeks} weeks before: {pct(d['was'])}% "
                f"({n(d['was_sold'])} sold, {n(d['was_sampled'])} sampled, "
                f"{plural(d['was_visits'], 'visit')})."
            ),
            facts={
                "subject": subject,
                "conversion_percent": d["now"],
                "previous_conversion_percent": d["was"],
                "visits": d["now_visits"],
                "previous_visits": d["was_visits"],
                "sampled": d["now_sampled"],
                "previous_sampled": d["was_sampled"],
                "weeks": weeks,
            },
            directions={"conversion": "down"},
        )

    brand = dropped(current, previous, BRAND_DROP_MIN_RECAPS, DROP_MIN_SAMPLED)
    if brand:
        out.append(drop_finding(CODE_CONVERSION_DROP, "brand", "", 1, "", brand))

    chains: dict[str, str] = {}
    for v in current + previous:
        if v.chain_key and v.rated:
            chains.setdefault(v.chain_key, v.chain_label)
    drops = []
    for key, label in chains.items():
        d = dropped(
            [v for v in current if v.chain_key == key],
            [v for v in previous if v.chain_key == key],
            DROP_MIN_RECAPS,
            DROP_MIN_SAMPLED,
        )
        if d:
            drops.append((key, label, d))
    if brand and len(chains) == 1:
        # One retailer: its drop is the brand's, already said.
        drops = []
    drops.sort(key=lambda t: (-(t[2]["was"] - t[2]["now"]), t[1].casefold()))
    for i, (key, label, d) in enumerate(drops[:DROP_MAX]):
        out.append(
            drop_finding(
                CODE_CHAIN_CONVERSION_DROP, f"chain:{key}", label, 2 + i, f" at {label}", d
            )
        )
    return out
