"""Executive daily retail-sampling roll-up (Torch first).

One email per morning covering every Retail recap APPROVED during the previous
calendar day in the roll-up's timezone, whatever the event date (a recap
approved yesterday for last Friday's demo is still "yesterday's approved
recap"; each row carries its own event date).

Every number comes from the canonical conversion set
(:func:`recaps.tenant_overview.conversion_window_totals` /
:func:`recaps.tenant_overview.sales_program_metrics` with
``approved_between``): approved, non-archived, Retail / On-Premise, 3rd-party
and dashboard-excluded recaps dropped, sold and base paired per recap, dry
demos based on People engaged. Month to date and program to date read the same
helpers by EVENT date, so they match Insights. Recap values are shown exactly
as submitted — nothing is rescaled.

3rd-party (``exclude_from_aggregates``) Retail recaps approved the same day are
listed in their own "3rd party · not in totals" section with their own
numbers, so the reader sees them without them touching any total.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from django.db.models import Q, Sum, TextField
from django.db.models.functions import Coalesce

from events.market import market_for
from recaps.models import CustomFieldValue, CustomRecap, CustomRecapProductSample, Recap
from recaps.pdf import _event_date
from recaps.share_render import _ba_name
from recaps.tenant_overview import (
    _choice_values,
    _retail_onprem_type_q,
    conversion_window_totals,
    custom_conversion_rows,
    sales_program_metrics,
)

DEFAULT_ROLLUP_TIMEZONE = "America/Los_Angeles"

_SAMPLE_FORMAT_RE = re.compile(r"^\s*sample format", re.IGNORECASE)
_PRODUCTS_RE = re.compile(r"^\s*products? sampled", re.IGNORECASE)
_COMPETITOR_RE = re.compile(r"competitor products", re.IGNORECASE)
_QUOTES_RE = re.compile(r"quotes from consumers", re.IGNORECASE)
_STORIES_RE = re.compile(r"positive stories", re.IGNORECASE)
_DISPLAY_FIELDS_RE = (
    r"^\s*sample format|^\s*products? sampled|competitor products"
    r"|quotes from consumers|positive stories"
)

_MAX_QUOTES = 3
_QUOTE_MAX_CHARS = 220
_COMPETITOR_MAX_CHARS = 90
# "No", "N/A", "none" — a blank answer, not a quote worth surfacing.
_MIN_QUOTE_CHARS = 12
_EMPTY_ANSWERS = frozenset({"", "none", "n/a", "na", "no", "-", "—", "nothing"})


@dataclass(frozen=True)
class RollupRow:
    kind: str
    recap_id: int
    store: str
    event_date: datetime.date | None
    market: str
    ba: str
    consumers_sampled: int | None
    units_sold: int
    dry_demo: bool
    counted: bool
    sample_format: str
    products: list[str]
    competitor: str
    third_party: bool = False

    @property
    def conversion_pct(self) -> float | None:
        if not self.counted or not self.consumers_sampled:
            return None
        return round(self.units_sold / self.consumers_sampled * 100, 1)


@dataclass(frozen=True)
class RollupQuote:
    text: str
    ba: str
    store: str


@dataclass(frozen=True)
class DailyRetailRollup:
    tenant_id: int
    report_date: datetime.date
    tz_name: str
    day: dict
    month_to_date: dict
    program_to_date: dict
    rows: list[RollupRow] = field(default_factory=list)
    third_party_rows: list[RollupRow] = field(default_factory=list)
    top_store: RollupRow | None = None
    top_sku: tuple[str, int, str] | None = None
    quotes: list[RollupQuote] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """True when at least one canonical Retail recap was approved that day."""
        return bool(self.rows)

    @property
    def uncounted(self) -> int:
        return sum(1 for r in self.rows if not r.counted)


def approval_window(
    report_date: datetime.date, tz_name: str = DEFAULT_ROLLUP_TIMEZONE
) -> tuple[datetime.datetime, datetime.datetime]:
    """Half-open ``[00:00, next 00:00)`` of ``report_date`` in ``tz_name``."""
    tz = ZoneInfo(tz_name)
    start = datetime.datetime.combine(report_date, datetime.time.min, tzinfo=tz)
    end = datetime.datetime.combine(
        report_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz
    )
    return start, end


def previous_day(
    now: datetime.datetime, tz_name: str = DEFAULT_ROLLUP_TIMEZONE
) -> datetime.date:
    """The calendar day before ``now`` in ``tz_name``."""
    return now.astimezone(ZoneInfo(tz_name)).date() - datetime.timedelta(days=1)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _excerpt(value: str | None, limit: int) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


def _is_answer(value: str | None) -> bool:
    return _clean(value).lower().strip(".!") not in _EMPTY_ANSWERS


def _display_fields(custom_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    out: dict[int, list[tuple[str, str]]] = {}
    for recap_id, name, value in (
        CustomFieldValue.objects.filter(
            custom_recap_id__in=custom_ids,
            custom_field__name__iregex=_DISPLAY_FIELDS_RE,
        )
        .order_by("custom_recap_id", "custom_field__order", "id")
        .values_list("custom_recap_id", "custom_field__name", "value")
    ):
        out.setdefault(recap_id, []).append((name or "", value or ""))
    return out


def _first(pairs: list[tuple[str, str]], pattern: re.Pattern) -> str:
    for name, value in pairs:
        if pattern.search(name) and _clean(value):
            return value
    return ""


def _structured_samples(custom_ids: list[int]) -> dict[int, list[tuple[str, int]]]:
    out: dict[int, list[tuple[str, int]]] = {}
    for row in (
        CustomRecapProductSample.objects.filter(custom_recap_id__in=custom_ids)
        .values("custom_recap_id", "product__name")
        .annotate(qty=Sum("quantity"))
        .order_by("custom_recap_id", "-qty")
    ):
        name = _clean(row["product__name"])
        if name:
            out.setdefault(row["custom_recap_id"], []).append((name, int(row["qty"] or 0)))
    return out


def _custom_rows(
    conv_rows: list[dict], *, third_party: bool = False
) -> tuple[list[RollupRow], dict[int, list[tuple[str, str]]], dict[int, list[tuple[str, int]]]]:
    ids = [int(r["id"]) for r in conv_rows]
    recaps = {
        r.id: r
        for r in CustomRecap.objects.filter(id__in=ids).select_related(
            "event",
            "event__request",
            "event__location__state",
            "event__state",
            "ambassador__user",
        )
    }
    fields_by_recap = _display_fields(ids)
    structured = _structured_samples(ids)
    rows: list[RollupRow] = []
    for conv in conv_rows:
        recap = recaps.get(int(conv["id"]))
        if recap is None:
            continue
        pairs = fields_by_recap.get(recap.id, [])
        products = [name for name, _qty in structured.get(recap.id, [])] or _choice_values(
            _first(pairs, _PRODUCTS_RE)
        )
        sample_format = ", ".join(_choice_values(_first(pairs, _SAMPLE_FORMAT_RE)))
        competitor = _first(pairs, _COMPETITOR_RE)
        event_date = _event_date(recap)
        rows.append(
            RollupRow(
                kind="custom",
                recap_id=recap.id,
                store=_clean(recap.name) or "Retail demo",
                event_date=event_date.date() if hasattr(event_date, "date") else event_date,
                market=market_for(recap.event).label or "",
                ba=_ba_name(recap) or _clean(recap.source_label),
                consumers_sampled=conv.get("base"),
                units_sold=int(conv.get("units") or 0),
                dry_demo=bool(conv.get("dry")),
                counted=bool(conv.get("counted")),
                sample_format=sample_format,
                products=products,
                competitor=(
                    _excerpt(competitor, _COMPETITOR_MAX_CHARS) if _is_answer(competitor) else ""
                ),
                third_party=third_party,
            )
        )
    return rows, fields_by_recap, structured


def _legacy_rows(conv_rows: list[dict]) -> list[RollupRow]:
    ids = [int(r["id"]) for r in conv_rows]
    recaps = {
        r.id: r
        for r in Recap.objects.filter(id__in=ids).select_related(
            "event", "event__request", "event__location__state", "event__state",
            "ambassador__user",
        )
    }
    rows: list[RollupRow] = []
    for conv in conv_rows:
        recap = recaps.get(int(conv["id"]))
        if recap is None:
            continue
        event_date = _event_date(recap)
        rows.append(
            RollupRow(
                kind="legacy",
                recap_id=recap.id,
                store=_clean(recap.name) or "Retail demo",
                event_date=event_date.date() if hasattr(event_date, "date") else event_date,
                market=market_for(recap.event).label or "",
                ba=_ba_name(recap),
                consumers_sampled=conv.get("base"),
                units_sold=int(conv.get("units") or 0),
                dry_demo=False,
                counted=bool(conv.get("counted")),
                sample_format="",
                products=[],
                competitor="",
            )
        )
    return rows


def _third_party_conv_rows(tenant_id: int, window: tuple) -> list[dict]:
    """Per-recap numbers for 3rd-party Retail recaps approved in ``window``."""
    ids = list(
        CustomRecap.objects.filter(
            tenant_id=tenant_id,
            approved=True,
            archived_at__isnull=True,
            exclude_from_aggregates=True,
            approved_at__gte=window[0],
            approved_at__lt=window[1],
        )
        .filter(Q(event__request__isnull=True) | Q(event__request__deleted_at__isnull=True))
        .annotate(
            _conv_type=Coalesce(
                "event__request__request_type__name",
                "event__event_type__name",
                "custom_recap_template__name",
                output_field=TextField(),
            )
        )
        .filter(_retail_onprem_type_q("_conv_type"))
        .values_list("id", flat=True)
    )
    return [{"kind": "custom", **row} for row in custom_conversion_rows(ids)]


def _sort_key(row: RollupRow) -> tuple:
    return (row.event_date or datetime.date.min, row.store.lower())


def _top_sku(
    rows: list[RollupRow], structured: dict[int, list[tuple[str, int]]]
) -> tuple[str, int, str] | None:
    """Most-sampled SKU: by structured sample qty when logged, else by demos."""
    qty: Counter[str] = Counter()
    for items in structured.values():
        for name, n in items:
            qty[name] += n
    if qty:
        name, n = qty.most_common(1)[0]
        return name, n, "samples"
    demos: Counter[str] = Counter()
    for row in rows:
        for name in dict.fromkeys(row.products):
            demos[name] += 1
    if not demos:
        return None
    name, n = sorted(demos.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return name, n, "demos"


def _quotes(
    rows: list[RollupRow], fields_by_recap: dict[int, list[tuple[str, str]]]
) -> list[RollupQuote]:
    out: list[RollupQuote] = []
    for pattern in (_QUOTES_RE, _STORIES_RE):
        for row in rows:
            if len(out) >= _MAX_QUOTES:
                return out
            if any(q.store == row.store and q.ba == row.ba for q in out):
                continue
            raw = _first(fields_by_recap.get(row.recap_id, []), pattern)
            if not _is_answer(raw) or len(_clean(raw)) < _MIN_QUOTE_CHARS:
                continue
            out.append(
                RollupQuote(text=_excerpt(raw, _QUOTE_MAX_CHARS), ba=row.ba, store=row.store)
            )
    return out


def build_daily_retail_rollup(
    tenant_id: int,
    report_date: datetime.date,
    tz_name: str = DEFAULT_ROLLUP_TIMEZONE,
) -> DailyRetailRollup:
    """Build the roll-up for Retail recaps approved on ``report_date`` in ``tz_name``."""
    window = approval_window(report_date, tz_name)
    conv = conversion_window_totals(
        tenant_id, None, None, detail=True, approved_between=window
    )
    custom_conv = [r for r in conv["recaps"] if r["kind"] == "custom"]
    legacy_conv = [r for r in conv["recaps"] if r["kind"] == "legacy"]
    custom_rows, fields_by_recap, structured = _custom_rows(custom_conv)
    rows = sorted(custom_rows + _legacy_rows(legacy_conv), key=_sort_key)
    third_party_rows, _, _ = _custom_rows(
        _third_party_conv_rows(tenant_id, window), third_party=True
    )

    top_store = None
    for row in rows:
        if row.counted and row.units_sold > (top_store.units_sold if top_store else 0):
            top_store = row
    month_start = report_date.replace(day=1)
    return DailyRetailRollup(
        tenant_id=tenant_id,
        report_date=report_date,
        tz_name=tz_name,
        day=sales_program_metrics(tenant_id, None, None, approved_between=window),
        month_to_date=sales_program_metrics(tenant_id, month_start, report_date),
        program_to_date=sales_program_metrics(tenant_id, None, None),
        rows=rows,
        third_party_rows=sorted(third_party_rows, key=_sort_key),
        top_store=top_store,
        top_sku=_top_sku(rows, structured),
        quotes=_quotes(rows, fields_by_recap),
    )
