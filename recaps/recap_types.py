"""Recap type (Retail / On-Premise / Event / Guerilla / Seeding) for a recap.

One classifier for the Recaps list filter, its type counts, and Torch recap
routing. The template the BA filed on wins, then its program, then the event's
program / template, then the linked request's type. Anything unrecognised is
retail.

The patterns are plain enough to run unchanged in Python ``re`` and Postgres
``~*``, so ``recap_type_expression`` classifies in SQL exactly like
``recap_type_key`` does in Python.
"""

from __future__ import annotations

import re

from django.db.models import (
    Case,
    CharField,
    Count,
    Model,
    QuerySet,
    Value,
    When,
)

RECAP_TYPE_RETAIL = "retail"
RECAP_TYPE_ONPREM = "onprem"
RECAP_TYPE_EVENT = "event"
RECAP_TYPE_GUERILLA = "guerilla"
RECAP_TYPE_SEEDING = "seeding"

# Chip order on the Recaps list.
RECAP_TYPES: tuple[tuple[str, str], ...] = (
    (RECAP_TYPE_RETAIL, "Retail"),
    (RECAP_TYPE_ONPREM, "On-Premise"),
    (RECAP_TYPE_EVENT, "Event"),
    (RECAP_TYPE_GUERILLA, "Guerilla"),
    (RECAP_TYPE_SEEDING, "Seeding"),
)
RECAP_TYPE_LABELS: dict[str, str] = dict(RECAP_TYPES)

# CONV (sold ÷ sampled) only means something for these.
CONVERSION_RECAP_TYPES = frozenset({RECAP_TYPE_RETAIL, RECAP_TYPE_ONPREM})

# First match wins: "Product Seeding" and "Guerilla Activation" must not read
# as events.
_PATTERNS: tuple[tuple[str, str], ...] = (
    (RECAP_TYPE_SEEDING, r"seeding"),
    (RECAP_TYPE_GUERILLA, r"guerr?ill?a"),
    (RECAP_TYPE_RETAIL, r"retail"),
    (RECAP_TYPE_ONPREM, r"on[-\s]?prem"),
    (RECAP_TYPE_EVENT, r"event|activation|festival|pop[-\s]?up"),
)
_COMPILED = tuple((key, re.compile(pattern, re.I)) for key, pattern in _PATTERNS)

# Name sources in precedence order. Legacy Recap has no template of its own,
# so its paths start at the event.
_SOURCES: tuple[str, ...] = (
    "custom_recap_template__name",
    "custom_recap_template__event_type__name",
    "event__event_type__name",
    "event__custom_recap_template__name",
    "event__request__request_type__name",
)


def normalize_recap_type(value: str | None) -> str | None:
    key = (value or "").strip().lower()
    return key if key in RECAP_TYPE_LABELS else None


def recap_type_for_name(name: str | None) -> str | None:
    text = name or ""
    for key, pattern in _COMPILED:
        if pattern.search(text):
            return key
    return None


def _related_name(obj, path: str) -> str | None:
    try:
        for attr in path.split("__"):
            obj = getattr(obj, attr, None)
            if obj is None:
                return None
    except Exception:
        return None
    return obj if isinstance(obj, str) else None


def recap_type_key(recap) -> str:
    """Recap type for a loaded Recap or CustomRecap."""
    for path in _SOURCES:
        key = recap_type_for_name(_related_name(recap, path))
        if key is not None:
            return key
    return RECAP_TYPE_RETAIL


def _model_sources(model: type[Model]) -> tuple[str, ...]:
    names = {f.name for f in model._meta.get_fields()}
    return tuple(path for path in _SOURCES if path.split("__", 1)[0] in names)


def recap_type_expression(model: type[Model]) -> Case:
    """SQL twin of ``recap_type_key`` for ``model`` (Recap or CustomRecap)."""
    whens = [
        When(**{f"{path}__iregex": pattern}, then=Value(key))
        for path in _model_sources(model)
        for key, pattern in _PATTERNS
    ]
    return Case(*whens, default=Value(RECAP_TYPE_RETAIL), output_field=CharField())


def with_recap_type(queryset: QuerySet, recap_type: str | None = None) -> QuerySet:
    """Annotate ``_recap_type``; narrow to ``recap_type`` when it is a known key."""
    queryset = queryset.annotate(_recap_type=recap_type_expression(queryset.model))
    key = normalize_recap_type(recap_type)
    return queryset if key is None else queryset.filter(_recap_type=key)


def recap_type_counts(queryset: QuerySet) -> dict[str, int]:
    rows = (
        with_recap_type(queryset.select_related(None).prefetch_related(None))
        .order_by()
        .values("_recap_type")
        .annotate(n=Count("pk", distinct=True))
    )
    return {row["_recap_type"]: int(row["n"]) for row in rows}
