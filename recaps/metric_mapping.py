"""Explicit recap-field → standard-metric mapping.

Spark's standard numbers (consumers sampled, units sold, purchase intent,
first-time, brand-aware, samples given) were found by matching a custom
field's NAME against a fixed vocabulary in :mod:`recaps.types`. A client
whose form says the same thing in other words read "—", or a wrong number.

This module is the stored, staff-confirmed alternative:

* :class:`recaps.models.RecapFieldMetricMapping` holds one decision per
  template field.
* A confirmed decision travels with the field NAME: :func:`tag_name` returns
  a :class:`MappedName` (a ``str`` that also carries the metric). The
  matchers in :mod:`recaps.types` look at that tag FIRST and use the name
  vocabulary only for names that carry none.
* A field nobody has confirmed is a plain ``str``, so every matcher takes
  exactly the path it took before this module existed. Nothing changes for
  a tenant until staff confirm.

The model only ever PROPOSES (:func:`suggest_mapping`): it is sent field
labels, types and choice options — never a recap answer or a person's name —
and its proposal is validated in code and returned, not stored. Rows are
written by :func:`save_mapping`, on a staff member's click.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from django.utils import timezone

logger = logging.getLogger(__name__)

CONSUMERS_SAMPLED = "consumers_sampled"
UNITS_SOLD_SINGLE = "units_sold_single"
UNITS_SOLD_PACK = "units_sold_pack"
WILLING_TO_BUY = "willing_to_buy"
NOT_WILLING = "not_willing"
FIRST_TIME = "first_time"
BRAND_AWARE = "brand_aware"
SAMPLES_GIVEN = "samples_given"
NONE = "none"

# (key, plain label). Order is the order of the staff dropdown.
METRIC_OPTIONS: tuple[tuple[str, str], ...] = (
    (CONSUMERS_SAMPLED, "Consumers sampled"),
    (UNITS_SOLD_SINGLE, "Units sold (singles)"),
    (UNITS_SOLD_PACK, "Units sold (packs)"),
    (WILLING_TO_BUY, "Willing to buy"),
    (NOT_WILLING, "Not willing to buy"),
    (FIRST_TIME, "First-time consumers"),
    (BRAND_AWARE, "Knew the brand"),
    (SAMPLES_GIVEN, "Samples given out"),
    (NONE, "Not a standard number"),
)
METRIC_LABELS = dict(METRIC_OPTIONS)
METRIC_KEYS = frozenset(METRIC_LABELS)
SOLD_METRICS = frozenset({UNITS_SOLD_SINGLE, UNITS_SOLD_PACK})
# Every metric but "none" is a count.
NUMERIC_METRICS = METRIC_KEYS - {NONE}
# One field per template for these. Sold metrics may repeat: a form lists
# one line per product.
SINGLE_VALUED_METRICS = NUMERIC_METRICS - SOLD_METRICS

# The standard numbers a client sees, for the "no field on this template"
# warning. Singles and packs are one number on a dashboard.
_WARN_METRICS: tuple[tuple[str, str, frozenset], ...] = (
    (CONSUMERS_SAMPLED, "Consumers sampled", frozenset({CONSUMERS_SAMPLED})),
    ("units_sold", "Units sold", SOLD_METRICS),
    (WILLING_TO_BUY, "Willing to buy", frozenset({WILLING_TO_BUY})),
    (FIRST_TIME, "First-time consumers", frozenset({FIRST_TIME})),
    (BRAND_AWARE, "Knew the brand", frozenset({BRAND_AWARE})),
    (SAMPLES_GIVEN, "Samples given out", frozenset({SAMPLES_GIVEN})),
)

MAX_PACK_SIZE = 1000
MAX_REASON_CHARS = 160


# ---------------------------------------------------------------------------
# The tag the matchers read.
# ---------------------------------------------------------------------------


class MappedName(str):
    """A field name that carries staff's confirmed metric for that field.

    Still a ``str`` — it prints, compares, hashes and serializes as the
    name — so it passes through every ``(name, value)`` pair unchanged.
    """

    metric: str
    pack_size: int | None

    def __new__(cls, name, metric: str, pack_size: int | None = None):
        obj = super().__new__(cls, name or "")
        obj.metric = metric
        obj.pack_size = pack_size
        return obj


def confirmed_metric_of(name) -> str | None:
    """The confirmed metric riding on ``name``, or None when undecided."""
    return name.metric if isinstance(name, MappedName) else None


def split_decided(pairs: Iterable[tuple]) -> tuple[list[tuple], list[tuple]]:
    """Split ``(name, value)`` pairs into (staff-decided, undecided).

    With no decided pair the second list is the input, in order — the
    vocabulary matchers then see exactly what they always saw.
    """
    decided: list[tuple] = []
    undecided: list[tuple] = []
    for pair in pairs:
        (decided if isinstance(pair[0], MappedName) else undecided).append(pair)
    return decided, undecided


def confirmed_metrics(
    *,
    tenant_id: int | None = None,
    field_ids: Iterable[int] | None = None,
) -> dict[int, tuple[str, int | None]]:
    """``{custom_field_id: (metric, pack_size)}`` for confirmed fields only.

    One small query. Scope by ``tenant_id`` or ``field_ids`` when the caller
    has them; with neither, every confirmed row is returned (the table holds
    one row per confirmed template field, so it stays small).
    """
    from recaps.models import RecapFieldMetricMapping

    qs = RecapFieldMetricMapping.objects.filter(
        confirmed_at__isnull=False, metric__isnull=False
    )
    if tenant_id is not None:
        qs = qs.filter(custom_field__custom_recap_template__tenant_id=tenant_id)
    if field_ids is not None:
        ids = [i for i in set(field_ids) if i is not None]
        if not ids:
            return {}
        qs = qs.filter(custom_field_id__in=ids)
    return {
        field_id: (metric, pack_size)
        for field_id, metric, pack_size in qs.values_list(
            "custom_field_id", "metric", "pack_size"
        )
        if metric in METRIC_KEYS
    }


def tag_name(name, field_id, confirmed: dict | None):
    """``name`` as a :class:`MappedName` when its field is confirmed."""
    if not confirmed or field_id is None:
        return name
    hit = confirmed.get(field_id)
    if hit is None:
        return name
    return MappedName(name, hit[0], hit[1])


def tagged_pairs(field_values, confirmed: dict | None = None) -> list[tuple]:
    """``(name, value)`` pairs for ``CustomFieldValue`` objects, tagged.

    ``confirmed`` may be passed by a caller that already loaded it (a list
    view does that once per request); otherwise it is loaded here for the
    fields at hand.
    """
    rows = []
    for v in field_values:
        cf = getattr(v, "custom_field", None)
        # The loaded field's own pk: never a deferred column.
        field_id = getattr(cf, "pk", None)
        rows.append((field_id, getattr(cf, "name", None), getattr(v, "value", None)))
    if confirmed is None:
        confirmed = confirmed_metrics(field_ids=[field_id for field_id, _, _ in rows])
    return [
        (tag_name(name, field_id, confirmed), value) for field_id, name, value in rows
    ]


def kpi_name_gate(pattern: str, confirmed: dict | None):
    """SQL filter for "rows the KPI matchers need".

    Several readers fetch only value rows whose field NAME matches the KPI
    vocabulary. A confirmed field can be worded any way at all, so its rows
    must come through that gate too — otherwise the confirmed number would be
    dropped in SQL before a matcher ever saw it.
    """
    from django.db.models import Q

    gate = Q(custom_field__name__iregex=pattern)
    if confirmed:
        gate |= Q(custom_field_id__in=list(confirmed))
    return gate


# ---------------------------------------------------------------------------
# What a template counts today, and what is missing.
# ---------------------------------------------------------------------------

_PACK_NAME_RE = re.compile(r"\bpacks?\b", re.IGNORECASE)


def vocabulary_metrics(names: Iterable[str | None]) -> dict[str, str]:
    """``{field name: metric}`` Spark derives from wording alone.

    Runs the real matchers from :mod:`recaps.types` over the template's field
    names (with a placeholder count), so this shows what the dashboards do —
    including the tiering, where a "sold" field only counts when the template
    has no cans / packs line.
    """
    from recaps import types as recap_types

    clean = [n for n in names if n]
    out: dict[str, str] = {}

    for name in clean:
        low = name.lower()
        if "first time" in low:
            out[name] = FIRST_TIME
        elif "knew about" in low:
            out[name] = BRAND_AWARE
        elif "willing to purchase" in low and "not" not in low:
            out[name] = WILLING_TO_BUY
        if recap_types._SAMPLES_GIVEN_RE.search(name):
            out[name] = SAMPLES_GIVEN

    for name, _count in recap_types._sold_unit_lines((n, "1") for n in clean):
        out[name] = UNITS_SOLD_PACK if _PACK_NAME_RE.search(name) else UNITS_SOLD_SINGLE

    headline = next(
        (
            n
            for n in clean
            if recap_types._CONSUMERS_SAMPLED_RE.search(n)
            and not recap_types._SAMPLED_DESC_EXCLUDE_RE.search(n)
        ),
        None,
    )
    if headline is not None:
        out[headline] = CONSUMERS_SAMPLED
    else:
        for name in clean:
            if recap_types._SAMPLED_TOTAL_RE.search(name):
                out[name] = CONSUMERS_SAMPLED
    return out


def _field_kind(type_name: str | None) -> str:
    """Canonical kind for a ``CustomRecapFieldType`` name (the same fuzzy
    rules the form renderers use)."""
    low = (type_name or "").strip().lower()
    if any(t in low for t in ("image", "photo", "img", "file", "video")):
        return "image"
    if low == "multiselect" or "multi" in low:
        return "multiselect"
    if low == "select" or "dropdown" in low:
        return "select"
    if low == "number" or "num" in low:
        return "number"
    if "long" in low or "textarea" in low or "paragraph" in low:
        return "longtext"
    if low == "text":
        return "text"
    return low or "other"


_CLEAN_COUNT_RE = re.compile(r"\s*\d[\d,\s]*")
_TEXT_SAMPLE = 200
_TEXT_COUNT_SHARE = 0.9


def countable_field_ids(fields) -> set[int]:
    """Fields a count may be read from.

    A number field always. A short-text field only when the answers already
    filed in it are plain numbers — older forms collect counts in text boxes,
    and that is fine, but a free-text box whose answers are sentences must
    never be read as a number (digits picked out of prose once produced a
    2,208-consumer total). Nothing here is sent to a model.
    """
    from recaps.models import CustomFieldValue

    out: set[int] = set()
    text_ids: list[int] = []
    for field in fields:
        kind = _field_kind(getattr(getattr(field, "custom_field_type", None), "name", None))
        if kind == "number":
            out.add(field.id)
        elif kind == "text":
            text_ids.append(field.id)
    for field_id in text_ids:
        values = [
            v
            for v in CustomFieldValue.objects.filter(custom_field_id=field_id)
            .order_by("-id")
            .values_list("value", flat=True)[:_TEXT_SAMPLE]
            if (v or "").strip()
        ]
        if not values:
            continue
        clean = sum(1 for v in values if _CLEAN_COUNT_RE.fullmatch(str(v)))
        if clean / len(values) >= _TEXT_COUNT_SHARE:
            out.add(field_id)
    return out


def _template_fields(template_id: int) -> list:
    from recaps.models import CustomField

    return list(
        CustomField.objects.filter(custom_recap_template_id=template_id)
        .select_related("custom_field_type", "recap_section")
        .order_by("order", "id")
    )


def template_mapping(template_id: int) -> dict | None:
    """Everything the staff screen shows for one template, or None."""
    from recaps.models import CustomRecapTemplate, RecapFieldMetricMapping

    template = CustomRecapTemplate.objects.filter(id=template_id).first()
    if template is None:
        return None
    fields = _template_fields(template_id)
    rows_by_field = {
        row.custom_field_id: row
        for row in RecapFieldMetricMapping.objects.filter(
            custom_field_id__in=[f.id for f in fields]
        ).select_related("confirmed_by")
    }
    by_wording = vocabulary_metrics(f.name for f in fields)
    countable = countable_field_ids(fields)

    out_fields = []
    effective: set[str] = set()
    for field in fields:
        row = rows_by_field.get(field.id)
        confirmed = (
            row.metric
            if row is not None and row.confirmed_at and row.metric in METRIC_KEYS
            else None
        )
        current = by_wording.get(field.name)
        counted_as = confirmed if confirmed is not None else current
        if counted_as and counted_as != NONE:
            effective.add(counted_as)
        confirmer = getattr(row, "confirmed_by", None) if confirmed else None
        out_fields.append(
            {
                "field_id": field.id,
                "name": field.name,
                "field_type": getattr(field.custom_field_type, "name", "") or "",
                "section": getattr(field.recap_section, "name", "") or "",
                "current_metric": current,
                "confirmed_metric": confirmed,
                "pack_size": row.pack_size if confirmed == UNITS_SOLD_PACK else None,
                "confirmed_by": _display_name(confirmer),
                "confirmed_at": row.confirmed_at if confirmed else None,
                "can_count": field.id in countable,
            }
        )

    missing = []
    for key, label, covers in _WARN_METRICS:
        if effective & covers:
            continue
        if key == SAMPLES_GIVEN and (
            CONSUMERS_SAMPLED in effective or template.product_samples
        ):
            # Samples fall back to consumers sampled, or to the product
            # rows this template tracks: there is a number to show.
            continue
        missing.append({"key": key, "label": label})

    return {
        "template_id": template.id,
        "tenant_id": template.tenant_id,
        "fields": out_fields,
        "missing": missing,
    }


def _display_name(user) -> str:
    if user is None:
        return ""
    full = " ".join(
        p for p in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if p
    ).strip()
    return full or (getattr(user, "email", "") or "")


# ---------------------------------------------------------------------------
# Validation shared by the proposal and the save.
# ---------------------------------------------------------------------------


def _clean_pack_size(metric: str | None, raw) -> int | None:
    if metric != UNITS_SOLD_PACK or raw is None:
        return None
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return None
    return size if 1 <= size <= MAX_PACK_SIZE else None


def validate_choices(
    choices: list[dict],
    fields: list,
    countable: set[int],
    *,
    standing: dict[str, int] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Apply the mapping rules. Returns ``(kept, rejected)``.

    ``choices`` are ``{"field_id", "metric", "pack_size", ...}``. Rules:

    * the field must be on this template and the metric a known one;
    * a count may only come from a field a count can be read from
      (:func:`countable_field_ids`);
    * at most one field per single-valued metric — only the sold metrics
      may repeat, one line per product.

    ``standing`` is ``{metric: field_id}`` for single-valued metrics already
    confirmed on fields that are not part of ``choices``; they keep their
    place. A rejected entry carries ``"problem"``, a plain sentence.
    """
    known = {f.id: f for f in fields}
    kept: list[dict] = []
    rejected: list[dict] = []
    taken: dict[str, int] = dict(standing or {})
    order = {f.id: i for i, f in enumerate(fields)}
    for choice in sorted(choices, key=lambda c: order.get(c.get("field_id"), 10**9)):
        field_id = choice.get("field_id")
        metric = choice.get("metric")
        field = known.get(field_id)
        if field is None:
            rejected.append({**choice, "problem": "That field is not on this template."})
            continue
        if metric is None:
            kept.append({**choice, "pack_size": None})
            continue
        if metric not in METRIC_KEYS:
            rejected.append({**choice, "problem": "That is not a standard number."})
            continue
        if metric in NUMERIC_METRICS and field_id not in countable:
            rejected.append(
                {
                    **choice,
                    "problem": (
                        f"“{field.name}” does not hold a number, so it cannot be "
                        f"counted as {METRIC_LABELS[metric].lower()}."
                    ),
                }
            )
            continue
        if metric in SINGLE_VALUED_METRICS:
            if metric in taken:
                rejected.append(
                    {
                        **choice,
                        "problem": (
                            f"Only one field can be {METRIC_LABELS[metric].lower()}; "
                            f"“{known[taken[metric]].name}” already is."
                        ),
                    }
                )
                continue
            taken[metric] = field_id
        kept.append({**choice, "pack_size": _clean_pack_size(metric, choice.get("pack_size"))})
    return kept, rejected


# ---------------------------------------------------------------------------
# Proposal. Labels, types and options only.
# ---------------------------------------------------------------------------

_SUGGEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fields"],
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ref", "metric", "pack_size", "reason"],
                "properties": {
                    "ref": {"type": "string"},
                    "metric": {"type": "string", "enum": [k for k, _ in METRIC_OPTIONS]},
                    "pack_size": {"type": ["integer", "null"]},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

_SUGGEST_SYSTEM = (
    "You map the fields of a field-marketing recap form to a fixed list of "
    "standard numbers. You are given only each field's label, its type and, "
    "for choice fields, its options. For every field return exactly one "
    "metric:\n"
    "- consumers_sampled: how many people tried or were handed the product.\n"
    "- units_sold_single: single units sold (cans, bottles, items).\n"
    "- units_sold_pack: packs or cases sold. Set pack_size to the units per "
    "pack only when the label states it; otherwise null.\n"
    "- willing_to_buy: how many people said they would buy.\n"
    "- not_willing: how many people said they would not buy.\n"
    "- first_time: how many people were trying the product for the first time.\n"
    "- brand_aware: how many people already knew the brand.\n"
    "- samples_given: how many samples were handed out (units, not people).\n"
    "- none: anything else, including notes, photos, names, dates, stores, "
    "percentages, money, and any field that describes people rather than "
    "counting them.\n"
    "Only number fields can hold a count. A per-product sales line (one "
    "label per product or flavour) is units_sold_single or units_sold_pack; "
    "a 'total' line that repeats those is none. Use each of the other "
    "metrics for at most one field. When unsure, answer none. Give a reason "
    "of at most fifteen words, about the label only."
)


def _suggest_prompt(fields: list) -> str:
    lines = ["Fields:"]
    for i, field in enumerate(fields):
        kind = _field_kind(getattr(field.custom_field_type, "name", None))
        options = [str(o) for o in (field.options or []) if str(o).strip()][:20]
        line = f"- ref f{i} | type {kind} | label: {(field.name or '').strip()[:200]}"
        if options and kind in ("select", "multiselect"):
            line += " | options: " + "; ".join(o[:60] for o in options)
        lines.append(line)
    return "\n".join(lines)


def suggest_mapping(template_id: int) -> dict:
    """Ask the model for a metric per field. Stores nothing.

    Returns ``{"ok", "reason", "suggestions"}``. ``ok`` is False with a plain
    reason when the model is not configured or did not answer; the screen
    then stays fully usable by hand.
    """
    from utils.ai_text import AiUnavailable, generate_json

    fields = _template_fields(template_id)
    if not fields:
        return {"ok": False, "reason": "This template has no fields yet.", "suggestions": []}

    try:
        raw = generate_json(
            _SUGGEST_SYSTEM,
            _suggest_prompt(fields),
            schema=_SUGGEST_SCHEMA,
            max_tokens=3000,
        )
    except AiUnavailable:
        raw = None
    except Exception:
        logger.warning("metric mapping suggestion failed template=%s", template_id)
        raw = None
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), list):
        return {
            "ok": False,
            "reason": "Suggestions are not available right now. Choose each field by hand.",
            "suggestions": [],
        }

    by_ref = {f"f{i}": field for i, field in enumerate(fields)}
    choices: list[dict] = []
    seen: set[int] = set()
    for item in raw["fields"]:
        if not isinstance(item, dict):
            continue
        field = by_ref.get(str(item.get("ref") or ""))
        if field is None or field.id in seen:
            continue
        seen.add(field.id)
        reason = " ".join(str(item.get("reason") or "").split())[:MAX_REASON_CHARS]
        choices.append(
            {
                "field_id": field.id,
                "metric": item.get("metric"),
                "pack_size": item.get("pack_size"),
                "reason": reason,
            }
        )

    kept, rejected = validate_choices(choices, fields, countable_field_ids(fields))
    suggestions = [
        {
            "field_id": c["field_id"],
            "metric": c["metric"],
            "pack_size": c["pack_size"],
            "reason": c.get("reason") or "",
        }
        for c in kept
        if c.get("metric")
    ]
    # A proposal that broke a rule becomes "none", with the rule as the reason.
    suggestions += [
        {
            "field_id": c["field_id"],
            "metric": NONE,
            "pack_size": None,
            "reason": c["problem"],
        }
        for c in rejected
        if c.get("field_id") in {f.id for f in fields}
    ]
    order = {f.id: i for i, f in enumerate(fields)}
    suggestions.sort(key=lambda s: order.get(s["field_id"], 10**9))
    return {"ok": True, "reason": "", "suggestions": suggestions}


# ---------------------------------------------------------------------------
# Save. Staff only (the resolver checks); nothing is ever deleted.
# ---------------------------------------------------------------------------


def save_mapping(template_id: int, choices: list[dict], user) -> dict:
    """Store staff's decisions. ``{"ok", "message"}``.

    A choice with ``metric`` None sets the field back to "not confirmed"
    (the row is kept, never deleted). All-or-nothing: one rule broken and
    nothing is written.
    """
    from django.db import transaction

    from recaps.models import RecapFieldMetricMapping

    fields = _template_fields(template_id)
    known_ids = {f.id for f in fields}
    incoming = {c.get("field_id"): c for c in choices}

    # Judge the template as it will stand after this save: a confirmed field
    # this request leaves alone still holds its single-valued metric.
    existing = {
        row.custom_field_id: row
        for row in RecapFieldMetricMapping.objects.filter(custom_field_id__in=known_ids)
    }
    standing = {
        row.metric: fid
        for fid, row in existing.items()
        if fid not in incoming
        and row.confirmed_at
        and row.metric in SINGLE_VALUED_METRICS
    }
    kept, rejected = validate_choices(
        list(incoming.values()),
        fields,
        countable_field_ids(fields),
        standing=standing,
    )
    if rejected:
        return {"ok": False, "message": rejected[0]["problem"]}

    now = timezone.now()
    with transaction.atomic():
        for choice in kept:
            field_id = choice["field_id"]
            metric = choice.get("metric")
            row = existing.get(field_id)
            if row is None:
                if metric is None:
                    continue
                row = RecapFieldMetricMapping(custom_field_id=field_id)
            row.metric = metric
            row.pack_size = choice.get("pack_size")
            row.confirmed_by = user if metric is not None else None
            row.confirmed_at = now if metric is not None else None
            row.save()
    return {"ok": True, "message": "Mapping saved."}
