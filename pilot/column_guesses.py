"""Column-map guesses for an agency CSV. Propose, never apply a model guess.

Rule matches (preset, saved map, known header) are marked ``applied`` so a
preview can keep working the way it does today. A model guess for a header
Spark does not recognize is returned with ``applied`` false. Callers that
pass a confirmed map use :func:`lock_column_map` and skip the model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from django.core.cache import cache

logger = logging.getLogger(__name__)

FIELD_LABELS: dict[str, str] = {
    "provider_event_id": "Event id",
    "visit_date": "Visit date",
    "start_time": "Start time",
    "end_time": "End time",
    "shift_key": "Shift",
    "retailer_name": "Store banner",
    "store_number": "Store number",
    "external_store_id": "External store id",
    "address": "Street address",
    "city": "City",
    "state_code": "State",
    "market": "Market",
    "program_name": "Program",
    "sku_name": "SKU",
    "samples_distributed": "Samples distributed",
    "consumers_sampled": "Consumers sampled",
    "rep_observed_units": "Units sold",
    "demo_fee": "Demo fee",
    "product_spend": "Product spend",
    "notes": "Notes",
}

_CACHE_SECONDS = 14 * 24 * 3600
_MAX_REASON = 160
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["columns"],
    "properties": {
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["header", "field", "reason"],
                "properties": {
                    "header": {"type": "string"},
                    "field": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

_SYSTEM = (
    "You map spreadsheet column headers from an agency field-marketing CSV "
    "onto a fixed list of Spark fields. For each header return the field key "
    "or \"none\" when it is not one of those fields. Give a reason of at most "
    "twelve words about the header wording only. Do not invent headers, "
    "fields, or numbers. When unsure, answer none."
)


def lock_column_map(
    headers: list[str],
    explicit_map: dict[str, str] | None,
    allowed: set[str],
) -> dict[str, str]:
    """Use only the map a person confirmed. No synonym fill, no model."""
    lookup = {
        str(h).strip().lower(): str(h)
        for h in headers
        if h is not None and str(h).strip()
    }
    resolved: dict[str, str] = {}
    used: set[str] = set()
    for field, col in (explicit_map or {}).items():
        if field not in allowed or not col:
            continue
        actual = lookup.get(str(col).strip().lower())
        if not actual or actual in used:
            continue
        resolved[field] = actual
        used.add(actual)
    return resolved


def _rule_reason(
    field: str,
    header: str,
    *,
    synonyms: dict[str, tuple[str, ...]],
    presets: dict[str, dict[str, str]],
    preset_key: str | None,
    explicit_map: dict[str, str] | None,
) -> tuple[str, str]:
    """``(reason, source)`` for a header that is already in the resolved map."""
    explicit = (explicit_map or {}).get(field)
    if explicit and str(explicit).strip().lower() == header.strip().lower():
        return "Saved column choice.", "rules"
    preset_cols = presets.get(preset_key or "") or {}
    preset_col = preset_cols.get(field)
    if preset_col and str(preset_col).strip().lower() == header.strip().lower():
        return f"The {preset_key} preset uses this column.", "rules"
    for syn in synonyms.get(field, ()):
        if str(syn).strip().lower() == header.strip().lower():
            return f"Header matches “{syn}”.", "rules"
    return "Matched this header.", "rules"


def _clean_reason(reason: str, header: str) -> str:
    text = " ".join(str(reason or "").split())[:_MAX_REASON]
    if not text:
        return "Suggested from the header wording."
    allowed = set(_NUMBER_RE.findall(header))
    for found in _NUMBER_RE.findall(text):
        if found not in allowed and found.replace(",", "") not in allowed:
            return "Suggested from the header wording."
    return text


def _model_suggestions(
    leftovers: list[str],
    available: list[str],
) -> dict[str, tuple[str, str]]:
    """``{header: (field, reason)}`` for guesses that must stay unapplied."""
    from django.conf import settings

    from utils.ai_text import AiUnavailable, generate_json

    if not leftovers or not available:
        return {}
    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        return {}
    raw_key = json.dumps({"h": leftovers, "f": available}, sort_keys=True)
    key = "csv_col_guess:" + hashlib.sha256(raw_key.encode()).hexdigest()[:40]
    hit = cache.get(key)
    if isinstance(hit, dict):
        return {
            str(h): (str(pair[0]), str(pair[1]))
            for h, pair in hit.items()
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        }
    lines = ["Allowed fields:"]
    for field in available:
        lines.append(f"- {field}: {FIELD_LABELS.get(field, field)}")
    lines.append("Headers:")
    for header in leftovers:
        lines.append(f"- {header}")
    try:
        raw = generate_json(
            _SYSTEM,
            "\n".join(lines),
            schema=_SCHEMA,
            max_tokens=1200,
            small=True,
            timeout=8,
        )
    except AiUnavailable:
        return {}
    except Exception:
        logger.info("column guess failed", exc_info=True)
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("columns"), list):
        return {}
    by_header = {h.strip().lower(): h for h in leftovers}
    taken: set[str] = set()
    out: dict[str, tuple[str, str]] = {}
    for item in raw["columns"]:
        if not isinstance(item, dict):
            continue
        header = by_header.get(str(item.get("header") or "").strip().lower())
        field = str(item.get("field") or "").strip()
        if not header or header in out:
            continue
        if field == "none" or field not in available or field in taken:
            continue
        taken.add(field)
        out[header] = (field, _clean_reason(str(item.get("reason") or ""), header))
    cache.set(
        key,
        {h: [field, reason] for h, (field, reason) in out.items()},
        _CACHE_SECONDS,
    )
    return out


def describe_column_map(
    headers: list[str],
    resolved_map: dict[str, str],
    *,
    synonyms: dict[str, tuple[str, ...]],
    presets: dict[str, dict[str, str]],
    preset_key: str | None,
    explicit_map: dict[str, str] | None,
    include_model: bool,
    confirmed: bool = False,
) -> dict:
    """Guess payload for the confirm screen. Model rows have ``applied`` false."""
    header_of = {header: field for field, header in resolved_map.items()}
    guesses: list[dict] = []
    for header in headers:
        if header is None or not str(header).strip():
            continue
        header = str(header)
        field = header_of.get(header)
        if field:
            if confirmed:
                reason, source = "Confirmed for this import.", "confirmed"
            else:
                reason, source = _rule_reason(
                    field,
                    header,
                    synonyms=synonyms,
                    presets=presets,
                    preset_key=preset_key,
                    explicit_map=explicit_map,
                )
            guesses.append(
                {
                    "header": header,
                    "field": field,
                    "field_label": FIELD_LABELS.get(field, field),
                    "reason": reason,
                    "source": source,
                    "applied": True,
                }
            )
    leftovers = [
        str(h)
        for h in headers
        if h is not None and str(h).strip() and str(h) not in header_of
    ]
    available = [key for key in FIELD_LABELS if key not in resolved_map]
    suggested: dict[str, tuple[str, str]] = {}
    if include_model and not confirmed:
        suggested = _model_suggestions(leftovers, available)
    for header in leftovers:
        pick = suggested.get(header)
        if pick:
            field, reason = pick
            guesses.append(
                {
                    "header": header,
                    "field": field,
                    "field_label": FIELD_LABELS.get(field, field),
                    "reason": reason,
                    "source": "model",
                    "applied": False,
                }
            )
            continue
        guesses.append(
            {
                "header": header,
                "field": "",
                "field_label": "",
                "reason": "Spark doesn't recognize this header. It stays unused until you map it.",
                "source": "none",
                "applied": False,
            }
        )
    return {
        "guesses": guesses,
        "fields": [{"key": key, "label": label} for key, label in FIELD_LABELS.items()],
    }
