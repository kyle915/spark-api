"""What consumers mention, month by month: the card's numbers.

Counted here, in Python, from the per-recap tags
(:mod:`consumer_mentions.tagging`); the model is never asked for a number
and nothing on the card is model prose. The headline sentences are built
from the counts, and checked against them with the weekly summary's
:func:`recaps.weekly_narration.numbers_are_grounded`.

Which recaps count is decided at read time, exactly like the dashboard:
approved, not archived, under a live request, events kept on the dashboard,
bucketed by EVENT date. A recap that is later unapproved drops out without
touching its tags.

A month is a calendar month. Every count is "recaps": a recap whose
comments mention a theme or brand twice counts once. Shares are of the
recaps read that month, so a busy month does not look like a rising theme.
A theme or brand is only called rising or falling when both months have at
least :data:`MIN_MONTH_RECAPS` recaps read, the larger month has at least
:data:`MIN_TREND_MENTIONS` recaps naming it, the count moved by at least
:data:`MIN_TREND_DELTA` and the share by at least
:data:`MIN_TREND_SHARE_POINTS` points. Anything less is "steady".
"""

from __future__ import annotations

import calendar
import logging
from collections import Counter, defaultdict
from datetime import date

import strawberry
from asgiref.sync import sync_to_async

from recaps.comment_insights import is_own_name, normalise, tenant_names
from recaps.feedback_themes import _client_visible, _parse_date, _short_quote
from recaps.models import CustomRecap, Recap
from recaps.tenant_overview import _event_date_expr, _inclusive_dates_to_window
from recaps.weekly_narration import numbers_are_grounded
from utils.graphql.permissions import StrictIsAuthenticated

from consumer_mentions.vocabulary import load_themes, resolve

logger = logging.getLogger(__name__)

STATUS_READY = "ready"
STATUS_TOO_FEW = "too_few"
STATUS_NOT_TAGGED = "not_tagged"
STATUS_UNAVAILABLE = "unavailable"

DIRECTION_RISING = "rising"
DIRECTION_FALLING = "falling"
DIRECTION_STEADY = "steady"
DIRECTION_TOO_FEW = "too_few"

DEFAULT_MONTHS = 6
MAX_MONTHS = 12
MIN_RECAPS = 5
MIN_MONTH_RECAPS = 6
MIN_TREND_MENTIONS = 3
MIN_TREND_DELTA = 2
MIN_TREND_SHARE_POINTS = 5
MIN_SHOWN = 2
MAX_THEMES = 8
MAX_BRANDS = 8
MAX_HEADLINES = 3


def month_keys(end: date, months: int) -> list[tuple[int, int]]:
    """``months`` calendar months ending with ``end``'s, oldest first."""
    out = []
    year, month = end.year, end.month
    for _ in range(months):
        out.append((year, month))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return list(reversed(out))


def _month_name(key: tuple[int, int]) -> str:
    return calendar.month_name[key[1]]


def visible_recaps(tenant_id: int, start: date, end: date) -> dict[str, date]:
    """Recaps a brand may be shown with their event date, keyed ``L<id>`` /
    ``C<id>``."""
    window = _inclusive_dates_to_window(start, end)
    out: dict[str, date] = {}
    for prefix, queryset in (
        ("L", Recap.objects.filter(event__tenant_id=tenant_id)),
        ("C", CustomRecap.objects.filter(tenant_id=tenant_id)),
    ):
        rows = (
            _client_visible(queryset, "event__", "", window)
            .annotate(_day=_event_date_expr("event__"))
            .values_list("id", "_day")
        )
        for recap_id, day in rows.iterator():
            if day is None:
                continue
            out[f"{prefix}{recap_id}"] = day.date() if hasattr(day, "date") else day
    return out


def _direction(cur: int, prev: int, n_cur: int, n_prev: int) -> str:
    if max(cur, prev) < MIN_TREND_MENTIONS:
        return DIRECTION_STEADY if max(cur, prev) else DIRECTION_TOO_FEW
    points = 100 * (cur / n_cur - prev / n_prev)
    if cur - prev >= MIN_TREND_DELTA and points >= MIN_TREND_SHARE_POINTS:
        return DIRECTION_RISING
    if prev - cur >= MIN_TREND_DELTA and points <= -MIN_TREND_SHARE_POINTS:
        return DIRECTION_FALLING
    return DIRECTION_STEADY


def _share(count: int, base: int) -> int:
    return round(100 * count / base) if base else 0


def _pick_quote(quotes: list[tuple[date, str]], prefer: tuple[int, int] | None) -> str:
    """A comment as written: from the compared month when there is one,
    newest first, leaving out any with a redacted name."""
    clean = [(day, text) for day, text in quotes if text and "[removed]" not in text]
    if not clean:
        return ""
    clean.sort(key=lambda dq: dq[0], reverse=True)
    if prefer:
        for day, text in clean:
            if (day.year, day.month) == prefer:
                return _short_quote(text)
    return _short_quote(clean[0][1])


def summarize(
    recaps: dict[str, date],
    tags: dict[str, dict],
    themes: list[dict],
    months: list[tuple[int, int]],
    *,
    brand: str = "",
    product_names: list[str] | None = None,
) -> dict:
    """The card from tags. Pure.

    ``recaps`` are the visible recaps and their event dates; ``tags`` are
    stored tags keyed like them (``{"themes": [...], "brands": [...]}``);
    only recaps in both, in ``months``, are counted.
    """
    product_names = product_names or []
    index = {key: i for i, key in enumerate(months)}
    read = [0] * len(months)
    theme_counts: dict[str, list[int]] = defaultdict(lambda: [0] * len(months))
    theme_quotes: dict[str, list[tuple[date, str]]] = defaultdict(list)
    brand_counts: dict[str, list[int]] = defaultdict(lambda: [0] * len(months))
    brand_names: dict[str, Counter] = defaultdict(Counter)
    other = 0
    by_key = {t["key"]: t for t in themes}

    for recap, day in recaps.items():
        tag = tags.get(recap)
        slot = index.get((day.year, day.month))
        if tag is None or slot is None:
            continue
        read[slot] += 1
        seen: set[str] = set()
        has_other = False
        for item in tag.get("themes") or []:
            theme = resolve(item, themes)
            if theme is None:
                has_other = has_other or (isinstance(item, dict) and item.get("theme") == "other")
                continue
            theme_quotes[theme["key"]].append((day, str(item.get("quote") or "")))
            if theme["key"] not in seen:
                seen.add(theme["key"])
                theme_counts[theme["key"]][slot] += 1
        other += int(has_other and not seen)
        seen_brands: set[str] = set()
        for item in tag.get("brands") or []:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            name = str(item.get("name") or "")
            if is_own_name(normalise(name), brand, product_names):
                continue
            key = item["key"]
            brand_names[key][name] += 1
            if key not in seen_brands:
                seen_brands.add(key)
                brand_counts[key][slot] += 1

    # The latest month with enough recaps read, against the month before it.
    compare = next(
        (
            i
            for i in range(len(months) - 1, 0, -1)
            if read[i] >= MIN_MONTH_RECAPS and read[i - 1] >= MIN_MONTH_RECAPS
        ),
        None,
    )
    if compare is not None and read[-1] >= MIN_MONTH_RECAPS and compare != len(months) - 1:
        compare = None  # the latest month has enough on its own: never skip it
    compare_month = months[compare] if compare is not None else None

    def _series(key: str, label: str, counts: list[int], quotes, polarity: str) -> dict:
        cur = counts[compare] if compare is not None else counts[-1]
        prev = counts[compare - 1] if compare is not None else (counts[-2] if len(counts) > 1 else 0)
        direction = (
            _direction(cur, prev, read[compare], read[compare - 1])
            if compare is not None
            else DIRECTION_TOO_FEW
        )
        return {
            "key": key,
            "label": label,
            "polarity": polarity,
            "counts": counts,
            "total": sum(counts),
            "current": cur,
            "previous": prev,
            "current_share": _share(cur, read[compare]) if compare is not None else 0,
            "previous_share": _share(prev, read[compare - 1]) if compare is not None else 0,
            "direction": direction,
            "quote": _pick_quote(quotes, compare_month),
        }

    def _rank(series: list[dict], cap: int) -> list[dict]:
        shown = [s for s in series if s["total"] >= MIN_SHOWN]
        shown.sort(key=lambda s: (-s["total"], s["label"].casefold()))
        return shown[:cap]

    theme_series = _rank(
        [
            _series(key, by_key[key]["label"], counts, theme_quotes[key], by_key[key]["polarity"])
            for key, counts in theme_counts.items()
        ],
        MAX_THEMES,
    )
    brand_series = _rank(
        [
            _series(
                key,
                min(brand_names[key].most_common(), key=lambda nc: (-nc[1], len(nc[0])))[0],
                counts,
                [],  # a brand's row shows counts only, no quote
                "",
            )
            for key, counts in brand_counts.items()
        ],
        MAX_BRANDS,
    )

    return {
        "months": [f"{y:04d}-{m:02d}" for y, m in months],
        "recap_counts": read,
        "compare_month": f"{compare_month[0]:04d}-{compare_month[1]:02d}" if compare_month else None,
        "prior_month": (
            f"{months[compare - 1][0]:04d}-{months[compare - 1][1]:02d}"
            if compare is not None
            else None
        ),
        "themes": theme_series,
        "brands": brand_series,
        "other_count": other,
        "headlines": headlines(theme_series, brand_series, read, months, compare),
    }


def headlines(themes, brands, read, months, compare) -> list[str]:
    """Up to three sentences about what moved, built from the counts."""
    if compare is None:
        return []
    now, before = _month_name(months[compare]), _month_name(months[compare - 1])
    moved = [
        (s, quoted)
        for group, quoted in ((brands, False), (themes, True))
        for s in group
        if s["direction"] in (DIRECTION_RISING, DIRECTION_FALLING)
    ]
    moved.sort(key=lambda sq: -abs(sq[0]["current_share"] - sq[0]["previous_share"]))
    out = []
    for series, quoted in moved[:MAX_HEADLINES]:
        name = f"“{series['label']}”" if quoted else series["label"]
        way = "up" if series["direction"] == DIRECTION_RISING else "down"
        out.append(
            f"{name} came up in {series['current']} of {read[compare]} recaps in {now}, "
            f"{way} from {series['previous']} of {read[compare - 1]} in {before}."
        )
    facts = {
        "read": read,
        "series": [[s["current"], s["previous"]] for s in [*themes, *brands]],
    }
    return [line for line in out if numbers_are_grounded(line, facts)]


def get_mention_trends(tenant_id: int, end: date | None = None, months: int = DEFAULT_MONTHS) -> dict:
    """The card for a brand: ``status`` is ``ready``, ``not_tagged`` (no
    recap has been read yet), ``too_few`` (under :data:`MIN_RECAPS` recaps
    read in the months shown) or ``unavailable``."""
    from consumer_mentions.models import RecapMentionTag
    from events.live_board import _ops_today

    end = end or _ops_today()
    months = max(2, min(MAX_MONTHS, int(months or DEFAULT_MONTHS)))
    keys = month_keys(end, months)
    start = date(keys[0][0], keys[0][1], 1)
    recaps = visible_recaps(tenant_id, start, end)
    rows = RecapMentionTag.objects.filter(tenant_id=tenant_id, recap_key__in=list(recaps))
    tags: dict[str, dict] = {}
    tagged_at = None
    for key, themes, brands, when in rows.values_list("recap_key", "themes", "brands", "tagged_at"):
        tags[key] = {"themes": themes, "brands": brands}
        tagged_at = max(tagged_at, when) if tagged_at else when
    brand, products = tenant_names(tenant_id)
    data = summarize(
        recaps,
        tags,
        load_themes(tenant_id),
        keys,
        brand=brand,
        product_names=[name for _pid, name in products],
    )
    read = sum(data["recap_counts"])
    if not tags:
        status = STATUS_NOT_TAGGED
    elif read < MIN_RECAPS:
        status = STATUS_TOO_FEW
    else:
        status = STATUS_READY
    data.update(status=status, tagged_at=tagged_at.isoformat() if tagged_at else None)
    return data


# ─── GraphQL ────────────────────────────────────────────────────────


@strawberry.type
class MentionSeries:
    """One theme or other brand by month. ``counts`` lines up with
    ``TenantMentionTrends.months`` and is recaps, not comments.
    ``current`` / ``previous`` are the compared months; ``direction`` is
    ``rising``, ``falling``, ``steady`` or ``too_few``. ``polarity`` is
    ``praise``, ``complaint`` or ``request`` for a theme, empty for a
    brand. ``quote`` is one consumer comment as written (themes only)."""

    key: str
    label: str
    polarity: str
    counts: list[int]
    total: int
    current: int
    previous: int
    current_share: int
    previous_share: int
    direction: str
    quote: str


@strawberry.type
class TenantMentionTrends:
    """What consumers mention, month over month, from approved recaps.
    ``months`` are ``YYYY-MM``; ``recap_counts`` is how many recaps with
    consumer comments were read in each. ``compare_month`` / ``prior_month``
    are the two months the directions compare, or null when no two
    neighbouring months have ``min_month_recaps`` recaps each."""

    status: str
    months: list[str]
    recap_counts: list[int]
    compare_month: str | None
    prior_month: str | None
    min_month_recaps: int
    min_trend_mentions: int
    themes: list[MentionSeries]
    brands: list[MentionSeries]
    other_count: int
    headlines: list[str]
    tagged_at: str | None


def _series_type(s: dict) -> MentionSeries:
    return MentionSeries(
        key=str(s["key"]),
        label=str(s["label"]),
        polarity=str(s.get("polarity") or ""),
        counts=[int(c) for c in s["counts"]],
        total=int(s["total"]),
        current=int(s["current"]),
        previous=int(s["previous"]),
        current_share=int(s["current_share"]),
        previous_share=int(s["previous_share"]),
        direction=str(s["direction"]),
        quote=str(s.get("quote") or ""),
    )


def _to_type(data: dict | None) -> TenantMentionTrends:
    data = data or {}
    return TenantMentionTrends(
        status=data.get("status", STATUS_UNAVAILABLE),
        months=list(data.get("months") or []),
        recap_counts=list(data.get("recap_counts") or []),
        compare_month=data.get("compare_month"),
        prior_month=data.get("prior_month"),
        min_month_recaps=MIN_MONTH_RECAPS,
        min_trend_mentions=MIN_TREND_MENTIONS,
        themes=[_series_type(s) for s in data.get("themes") or []],
        brands=[_series_type(s) for s in data.get("brands") or []],
        other_count=int(data.get("other_count") or 0),
        headlines=list(data.get("headlines") or []),
        tagged_at=data.get("tagged_at"),
    )


@strawberry.type
class MentionTrendQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_mention_trends(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        end_date: str | None = None,
        months: int | None = None,
    ) -> TenantMentionTrends:
        """Themes and other brands in consumer comments on approved recaps,
        by calendar month (event date), for the ``months`` months ending
        with ``end_date``'s (default: this month, six months). A brand user
        reads their own tenant whatever id is passed; Ignite staff may pass
        any tenant. Never raises: a failure is ``status: "unavailable"``."""
        from recaps.report_types import _CampaignReportService
        from tenants.models import Tenant

        end = _parse_date(end_date)

        def _run(target: int):
            if not Tenant.objects.filter(id=target).exists():
                return None
            return get_mention_trends(target, end, months or DEFAULT_MONTHS)

        try:
            target = await _CampaignReportService().resolve_target_tenant_id(info, tenant_id)
            if target is None:
                return _to_type(None)
            data = await sync_to_async(_run, thread_sensitive=True)(target)
        except Exception:  # noqa: BLE001
            logger.exception("tenantMentionTrends failed")
            return _to_type(None)
        return _to_type(data)
