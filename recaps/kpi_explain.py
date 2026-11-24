"""Why a Program KPI tile shows the number it shows.

The dashboard's KPI tiles (``tenantKpis``) are sums over approved recaps,
read from custom recap fields matched by name (or by a staff confirmation,
:mod:`recaps.metric_mapping`). When a client's wording did not match, the
tile read 0 or "—" and nobody could tell why. This module answers, for one
tile:

* which fields on which forms feed it, and whether each was confirmed by
  Ignite or picked up from its wording;
* how many recaps it counts, and how many counted recaps had no answer for it;
* what the recaps it leaves out (waiting for approval, kept out of totals)
  would add.

Every number comes from the SAME aggregation the tile uses —
:func:`recaps.tenant_overview._custom_kpis_window` /
:func:`recaps.tenant_overview._legacy_kpis_window`, or
:func:`recaps.tenant_overview.sales_program_metrics` for sales programs —
run once per recap scope. The per-field split re-reads each recap's
``(name, value)`` pairs with the same matchers from :mod:`recaps.types`.
So the explanation's total is the tile's total by construction (and a test
holds it there).

Read lazily, when a tile is opened: nothing here runs on the dashboard's
own query.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recaps import metric_mapping

# Tile metric key -> (label, the metric_mapping "missing" key that means no
# form field feeds it, program-metrics key when the tenant is a sales program).
METRICS: dict[str, tuple[str, str | None, str | None]] = {
    "products_sold": ("Products sold", "units_sold", "units_sold"),
    "consumers_reached": ("Consumers sampled", metric_mapping.CONSUMERS_SAMPLED, "consumers_sampled"),
    "samples_distributed": ("Samples handed out", metric_mapping.SAMPLES_GIVEN, None),
    "total_engagements": ("Total engagements", None, None),
    "first_time_consumers": ("First-time consumers", metric_mapping.FIRST_TIME, "first_time_consumers"),
    "brand_aware_consumers": ("Brand-aware", metric_mapping.BRAND_AWARE, "brand_aware_consumers"),
    "willing_to_purchase": ("Willing to purchase", metric_mapping.WILLING_TO_BUY, "willing_to_purchase"),
}
PROGRAM_LABELS = {"products_sold": "Units sold"}

HOW_CONFIRMED = "confirmed"
HOW_WORDING = "wording"
HOW_STANDARD = "standard"

LEGACY_SOURCE_NAME = "Standard recap form"


@dataclass
class FieldLine:
    label: str
    how: str
    field_id: int | None = None
    value: int | None = 0


@dataclass
class Source:
    template_id: int | None
    template_name: str
    recaps: int = 0
    value: int | None = 0
    fields: dict = field(default_factory=dict)  # label -> FieldLine


# ---------------------------------------------------------------------------
# Per-recap attribution. Values always come from the shared matchers.
# ---------------------------------------------------------------------------


def _how(name) -> str:
    return HOW_CONFIRMED if metric_mapping.confirmed_metric_of(name) else HOW_WORDING


def _audience_key_of(name) -> str | None:
    """Which audience total a field name adds to, by the tile's own rule."""
    from recaps.tenant_overview import _add_audience_from_pairs

    probe = {"first_time_consumers": 0, "brand_aware_consumers": 0, "willing_to_purchase": 0}
    _add_audience_from_pairs([(name, "1")], probe)
    return next((k for k, v in probe.items() if v), None)


def _recap_lines(metric: str, pairs: list[tuple]) -> tuple[int | None, list[tuple]]:
    """``(recap value or None, [(field name, value)])`` for one custom recap.

    ``recap value`` is exactly what the tile adds for this recap.
    """
    from recaps import types as rt
    from recaps.tenant_overview import _add_audience_from_pairs, _leading_int

    if metric == "products_sold":
        lines = rt._sold_unit_lines(pairs)
        return (sum(n for _, n in lines) if lines else None), lines

    if metric == "consumers_reached":
        value = rt._consumers_sampled_from_fields(pairs)
        if value is None:
            return None, []
        hits = [
            (name, rt._consumers_sampled_from_fields([(name, val)]))
            for name, val in pairs
        ]
        hits = [(n, v) for n, v in hits if v is not None]
        confirmed = [
            (n, v)
            for n, v in hits
            if metric_mapping.confirmed_metric_of(n) == metric_mapping.CONSUMERS_SAMPLED
        ]
        if confirmed:
            return value, confirmed
        headline = [
            (n, v)
            for n, v in hits
            if metric_mapping.confirmed_metric_of(n) is None
            and rt._CONSUMERS_SAMPLED_RE.search(n)
            and not rt._SAMPLED_DESC_EXCLUDE_RE.search(n)
        ]
        if headline:
            return value, headline[:1]
        totals = [
            (n, v)
            for n, v in hits
            if metric_mapping.confirmed_metric_of(n) is None and rt._SAMPLED_TOTAL_RE.search(n)
        ]
        return value, totals

    if metric == "samples_given":
        value = rt._samples_given_from_fields(pairs)
        if value is None:
            return None, []
        lines = [(n, rt._samples_given_from_fields([(n, v)])) for n, v in pairs]
        return value, [(n, v) for n, v in lines if v is not None]

    if metric in ("first_time_consumers", "brand_aware_consumers", "willing_to_purchase"):
        total = {"first_time_consumers": 0, "brand_aware_consumers": 0, "willing_to_purchase": 0}
        _add_audience_from_pairs(pairs, total)
        lines = []
        for name, val in pairs:
            if _audience_key_of(name) != metric:
                continue
            parsed = _leading_int(val)
            if parsed is not None:
                lines.append((name, parsed))
        return (total[metric] if lines else None), lines

    return None, []


# ---------------------------------------------------------------------------
# One recap scope (counted / pending / kept out), through the tile's code.
# ---------------------------------------------------------------------------


@dataclass
class ScopeResult:
    total: int
    sources: list[Source]
    recaps_with_value: int
    recaps_in_scope: int
    note: str | None = None


def _template_of_recaps(recap_ids) -> dict[int, tuple[int | None, str]]:
    from recaps.models import CustomRecap

    ids = list(recap_ids)
    if not ids:
        return {}
    return {
        rid: (tid, name or "")
        for rid, tid, name in CustomRecap.objects.filter(id__in=ids).values_list(
            "id", "custom_recap_template_id", "custom_recap_template__name"
        )
    }


@dataclass
class _ScopeData:
    """One pass of the tile's aggregation over one recap scope."""

    custom: dict
    legacy: dict
    trace: dict
    custom_recaps: list
    recap_template: dict
    legacy_count: int


def _scope_data(tenant_id: int, window, scope: str) -> _ScopeData:
    from recaps.models import CustomRecap, Recap
    from recaps.tenant_overview import (
        SCOPE_NON_AGGREGATE,
        _custom_kpis_window,
        _legacy_kpis_window,
        _scoped_rows,
    )

    trace: dict = {}
    custom = _custom_kpis_window(tenant_id, window, scope=scope, trace=trace)
    legacy = _legacy_kpis_window(tenant_id, window, scope=scope)
    custom_recaps = list(
        _scoped_rows(
            CustomRecap.objects.filter(tenant_id=tenant_id), "event__", window, "", scope
        ).values_list(
            "id", "custom_recap_template_id", "custom_recap_template__name", "total_engagements"
        )
    )
    recap_template = {rid: (tid, name or "") for rid, tid, name, _te in custom_recaps}
    per_recap = trace.get("per_recap") or {}
    missing = [rid for rid in per_recap if rid not in recap_template]
    recap_template.update(_template_of_recaps(missing))
    legacy_count = 0
    if scope != SCOPE_NON_AGGREGATE:
        legacy_count = _scoped_rows(
            Recap.objects.filter(event__tenant_id=tenant_id), "event__", window, "", scope
        ).count()
    return _ScopeData(
        custom=custom,
        legacy=legacy,
        trace=trace,
        custom_recaps=custom_recaps,
        recap_template=recap_template,
        legacy_count=legacy_count,
    )


def _attribute(
    tenant_id: int, window, scope: str, data: _ScopeData, metric: str
) -> ScopeResult:
    """Split one metric's scope total by form and field."""
    custom, legacy, trace = data.custom, data.legacy, data.trace
    total = int(custom.get(metric, 0)) + int(legacy.get(metric, 0))
    per_recap: dict = trace.get("per_recap") or {}
    recap_template = data.recap_template
    sources: dict = {}

    def _source(tid, tname) -> Source:
        if tid not in sources:
            sources[tid] = Source(template_id=tid, template_name=tname)
        return sources[tid]

    def _add(src: Source, label: str, how: str, value: int) -> None:
        line = src.fields.get(label)
        if line is None:
            line = src.fields[label] = FieldLine(label=label, how=how, value=0)
        line.value += int(value or 0)

    with_value = 0
    note = None
    sub_metric = metric
    if metric == "samples_distributed":
        # Samples handed out = consumers sampled (one sample per person),
        # else a "samples given out" count, else product sample rows: the
        # tile's own fallback order.
        if custom.get("consumers_reached"):
            sub_metric = "consumers_reached"
            note = "One sample per consumer sampled, so this matches consumers sampled."
        elif trace.get("samples_given_total"):
            sub_metric = "samples_given"
        else:
            sub_metric = "structured_samples"

    if metric == "total_engagements":
        for _rid, tid, tname, te in data.custom_recaps:
            if te is None:
                continue
            src = _source(tid, tname)
            src.recaps += 1
            src.value += int(te)
            _add(src, "Total engagements", HOW_STANDARD, te)
            with_value += 1
    elif sub_metric == "structured_samples":
        if int(trace.get("structured_samples") or 0):
            from recaps.models import CustomRecapProductSample
            from recaps.tenant_overview import _scoped_rows

            rows = _scoped_rows(
                CustomRecapProductSample.objects.filter(custom_recap__tenant_id=tenant_id),
                "custom_recap__event__",
                window,
                "custom_recap__",
                scope,
            ).values_list("custom_recap_id", "quantity")
            seen: set = set()
            for rid, qty in rows:
                tid, tname = recap_template.get(rid, (None, ""))
                src = _source(tid, tname)
                if rid not in seen:
                    src.recaps += 1
                    seen.add(rid)
                src.value += int(qty or 0)
                _add(src, "Product samples", HOW_STANDARD, qty)
            with_value = len(seen)
    else:
        for rid, pairs in per_recap.items():
            value, lines = _recap_lines(sub_metric, pairs)
            if value is None:
                continue
            tid, tname = recap_template.get(rid, (None, ""))
            src = _source(tid, tname)
            src.recaps += 1
            src.value += int(value)
            with_value += 1
            for name, v in lines:
                _add(src, str(name), _how(name), v)

    # Older recaps filed on the standard (non-custom) form.
    legacy_value = int(legacy.get(metric, 0))
    if data.legacy_count and (legacy_value or metric != "samples_distributed"):
        label = METRICS[metric][0]
        src = Source(
            template_id=None,
            template_name=LEGACY_SOURCE_NAME,
            recaps=data.legacy_count,
            value=legacy_value,
        )
        src.fields[label] = FieldLine(label=label, how=HOW_STANDARD, value=legacy_value)
        sources[("legacy",)] = src
        if legacy_value:
            with_value += data.legacy_count

    return ScopeResult(
        total=total,
        sources=list(sources.values()),
        recaps_with_value=with_value,
        recaps_in_scope=len(data.custom_recaps) + data.legacy_count,
        note=note,
    )


def _scope(tenant_id: int, window, metric: str, scope: str) -> ScopeResult:
    return _attribute(tenant_id, window, scope, _scope_data(tenant_id, window, scope), metric)


# ---------------------------------------------------------------------------
# Coverage: is this number on the client's forms at all?
# ---------------------------------------------------------------------------


def _relevant_templates(tenant_id: int) -> dict[int, dict]:
    """The tenant's templates that have recaps (all of them when none do)."""
    from recaps.models import CustomRecap

    templates = metric_mapping.effective_metrics_by_template(tenant_id)
    used = set(
        CustomRecap.objects.filter(tenant_id=tenant_id)
        .values_list("custom_recap_template_id", flat=True)
        .distinct()
    )
    picked = {tid: t for tid, t in templates.items() if tid in used}
    return picked or templates


def _templates_missing(templates: dict[int, dict], metric: str) -> list[dict]:
    missing_key = METRICS[metric][1]
    if missing_key is None:
        return []
    out = []
    for tid, t in templates.items():
        keys = {
            m["key"]
            for m in metric_mapping.missing_metrics(
                t["effective"], product_samples=t["product_samples"]
            )
        }
        if missing_key in keys:
            out.append({"template_id": tid, "template_name": t["name"]})
    return out


def _has_legacy_recaps(tenant_id: int) -> bool:
    from recaps.models import Recap

    return Recap.objects.filter(event__tenant_id=tenant_id).exists()


def untracked_metrics(tenant_id: int) -> list[str]:
    """Tile metrics no form of this tenant has a field for.

    A tile showing 0 for one of these is "not tracked on this form", not a
    real zero. Names only; no recap answer is read.
    """
    if _has_legacy_recaps(tenant_id):
        return []  # the standard form carries every standard number
    templates = _relevant_templates(tenant_id)
    if not templates:
        return []
    out = []
    for metric in METRICS:
        missing = _templates_missing(templates, metric)
        if missing and len(missing) == len(templates):
            out.append(metric)
    return out


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------


def _window(year: int | None):
    from recaps.tenant_overview import _year_bounds

    return None if year is None else _year_bounds(year)


def _is_program(tenant_id: int) -> bool:
    from recaps.tenant_overview import tenant_trend_program
    from tenants.models import Tenant

    return tenant_trend_program(tenant_id) == Tenant.TREND_SERIES_SALES


def _program_total(tenant_id: int, window, metric: str) -> int:
    from recaps.tenant_overview import sales_program_metrics, window_to_inclusive_dates

    data = sales_program_metrics(tenant_id, *window_to_inclusive_dates(window))
    return int(data.get(METRICS[metric][2]) or 0)


def _plain(n: int) -> str:
    return f"{n:,}"


def _summary(label: str, total: int, counted: ScopeResult, excluded: list[dict], program: bool) -> str:
    fields = []
    for src in counted.sources:
        for line in src.fields.values():
            if line.label not in fields:
                fields.append(line.label)
    noun = "recap" if counted.recaps_with_value == 1 else "recaps"
    if not fields:
        head = f"{label} has no answers to count yet."
    else:
        quoted = " + ".join(f"“{f}”" for f in fields[:4])
        if len(fields) > 4:
            quoted += f" + {len(fields) - 4} more"
        head = (
            f"{label} = {quoted}, from {_plain(counted.recaps_with_value)} approved {noun}."
        )
    tails = []
    for bucket in excluded:
        if not bucket["recaps"]:
            continue
        rnoun = "recap" if bucket["recaps"] == 1 else "recaps"
        if bucket.get("would_add"):
            tails.append(
                f"{_plain(bucket['recaps'])} {rnoun} {bucket['phrase']} would add "
                f"{_plain(bucket['would_add'])}."
            )
        else:
            tails.append(f"{_plain(bucket['recaps'])} {rnoun} {bucket['phrase']}.")
    return " ".join([head, *tails])


def explain_tenant_kpi(tenant_id: int, metric: str, year: int | None = None) -> dict | None:
    """The explanation for one KPI tile, or None for an unknown metric."""
    from recaps.tenant_overview import SCOPE_COUNTED, SCOPE_NON_AGGREGATE, SCOPE_PENDING

    if metric not in METRICS:
        return None
    window = _window(year)
    program = _is_program(tenant_id) and METRICS[metric][2] is not None
    label = PROGRAM_LABELS.get(metric, METRICS[metric][0]) if program else METRICS[metric][0]

    counted = _scope(tenant_id, window, metric, SCOPE_COUNTED)
    pending = _scope(tenant_id, window, metric, SCOPE_PENDING)
    kept_out = _scope(tenant_id, window, metric, SCOPE_NON_AGGREGATE)

    total = _program_total(tenant_id, window, metric) if program else counted.total
    note = counted.note
    if program:
        note = (
            "Counts approved retail and on-premise recaps only, the same recaps "
            "as Conversion."
        )

    def _bucket(key, label_text, phrase, res: ScopeResult):
        recaps = res.recaps_with_value if not program else res.recaps_in_scope
        return {
            "key": key,
            "label": label_text,
            "phrase": phrase,
            "recaps": recaps,
            # A sales program counts a subset of recaps, so a straight sum of
            # the left-out recaps would not be what the tile would add.
            "would_add": None if program else res.total,
        }

    excluded = [
        _bucket("pending_approval", "Waiting for approval", "waiting for approval", pending),
        _bucket(
            "excluded_from_totals",
            "Kept out of totals",
            "kept out of totals (filed by a third party)",
            kept_out,
        ),
    ]
    excluded = [b for b in excluded if b["recaps"]]

    templates = _relevant_templates(tenant_id)
    not_tracked = [] if _has_legacy_recaps(tenant_id) else _templates_missing(templates, metric)

    sources = []
    for src in counted.sources:
        sources.append(
            {
                "template_id": src.template_id,
                "template_name": src.template_name,
                "recaps": src.recaps,
                "value": None if program else src.value,
                "fields": [
                    {
                        "field_id": line.field_id,
                        "label": line.label,
                        "how": line.how,
                        "value": None if program else line.value,
                    }
                    for line in src.fields.values()
                ],
            }
        )

    # The web app opens a template by uuid; one lookup for every form named.
    from recaps.models import CustomRecapTemplate

    template_ids = {s["template_id"] for s in sources if s["template_id"] is not None}
    template_ids |= {t["template_id"] for t in not_tracked}
    uuids = {
        tid: str(uuid)
        for tid, uuid in CustomRecapTemplate.objects.filter(id__in=template_ids).values_list(
            "id", "uuid"
        )
    }
    for item in [*sources, *not_tracked]:
        item["template_uuid"] = uuids.get(item["template_id"])

    return {
        "metric": metric,
        "label": label,
        "total": int(total),
        "program": program,
        "summary": _summary(label, total, counted, excluded, program),
        "recaps_counted": counted.recaps_with_value,
        "recaps_in_scope": counted.recaps_in_scope,
        "recaps_without_value": max(0, counted.recaps_in_scope - counted.recaps_with_value),
        "sources": sources,
        "excluded": excluded,
        "not_tracked_templates": not_tracked,
        "not_tracked": bool(templates) and len(not_tracked) == len(templates),
        "note": note,
    }


def tenant_kpi_status(tenant_id: int, year: int | None = None) -> dict:
    """What the tiles show beside their number: untracked metrics, and how
    many recaps waiting for approval would add to each.

    One pending-scope pass of the tile aggregation (pending recaps are a
    small set) plus a field-name read; kept off the dashboard's own query.
    """
    from recaps.tenant_overview import SCOPE_PENDING

    window = _window(year)
    program = _is_program(tenant_id)
    data = _scope_data(tenant_id, window, SCOPE_PENDING)
    pending = []
    for metric in METRICS:
        res = _attribute(tenant_id, window, SCOPE_PENDING, data, metric)
        in_program = program and METRICS[metric][2] is not None
        recaps = res.recaps_in_scope if in_program else res.recaps_with_value
        if not recaps:
            continue
        pending.append(
            {
                "metric": metric,
                "recaps": recaps,
                "would_add": None if in_program else res.total,
            }
        )
    return {"untracked": untracked_metrics(tenant_id), "pending": pending}
