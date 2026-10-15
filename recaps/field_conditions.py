"""Show-when rules for custom recap fields, set per template.

``CustomRecapTemplate.layout["fieldConditions"]`` maps a field name to the
choice field that reveals it::

    {"4oz pours served": {"field": "Sample format", "includes": "4oz pour",
                          "required": true}}

The walk-up shows the field only while the named choice field has that
option picked, and ``required`` applies only while it shows. The field row
itself stays optional, so every other recap form treats it as an ordinary
optional field.
"""

from __future__ import annotations

from dataclasses import dataclass

FIELD_CONDITIONS_KEY = "fieldConditions"


@dataclass(frozen=True)
class FieldCondition:
    field: str
    includes: str
    required: bool


def field_conditions(template) -> dict[str, FieldCondition]:
    """{lower-cased field name: condition} from the template's layout."""
    layout = getattr(template, "layout", None)
    raw = layout.get(FIELD_CONDITIONS_KEY) if isinstance(layout, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, FieldCondition] = {}
    for name, rule in raw.items():
        if not isinstance(rule, dict):
            continue
        source = str(rule.get("field") or "").strip()
        option = str(rule.get("includes") or "").strip()
        if not str(name).strip() or not source or not option:
            continue
        out[str(name).strip().lower()] = FieldCondition(
            field=source, includes=option, required=bool(rule.get("required"))
        )
    return out


def apply_conditions(template, fields: list[dict]) -> None:
    """Add ``showWhen`` to serialized walk-up fields (``id``/``name`` keys).

    A rule whose source field isn't on the template is dropped, so the field
    renders plainly rather than staying hidden forever.
    """
    rules = field_conditions(template)
    if not rules:
        return
    ids = {(f.get("name") or "").strip().lower(): f.get("id") for f in fields}
    for f in fields:
        rule = rules.get((f.get("name") or "").strip().lower())
        source_id = ids.get(rule.field.lower()) if rule else None
        if rule is None or source_id is None:
            continue
        f["showWhen"] = {"fieldId": source_id, "includes": rule.includes}
        f["required"] = rule.required
