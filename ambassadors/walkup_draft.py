"""Unsubmitted walk-up recap draft.

Device autosave is not enough when the browser is cleared or the BA opens
the standing link on another phone. This stores the answers for the shift
without filing the recap: no ``submitted_at``, no ``approved=True``, no
field values, no photos on the recap row, and no email.

An empty clock-out stub may be linked so we know which row the draft
belongs to. Linking does not write that row. A filed recap is never the
link target and is never updated.
"""

from __future__ import annotations

import json

from django.db import connection, transaction

SHIFT_SCOPE = "shift"
ADDITIONAL_SCOPE = "additional"
_SCOPES = (SHIFT_SCOPE, ADDITIONAL_SCOPE)
_MAX_BYTES = 200_000


def _scope(raw: str | None) -> str:
    return raw if raw in _SCOPES else SHIFT_SCOPE


def _clip(value, limit: int) -> str:
    return str(value if value is not None else "")[:limit]


def _str_map(raw, *, key_limit: int = 80, value_limit: int = 8000, max_keys: int = 400) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in list(raw.items())[:max_keys]:
        name = _clip(key, key_limit)
        if not name:
            continue
        if isinstance(value, list):
            out[name] = [_clip(item, value_limit) for item in value[:80]]
        elif value is None:
            continue
        else:
            out[name] = _clip(value, value_limit)
    return out


def _co_brands(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    enabled = raw.get("enabled")
    if enabled not in (True, False, None):
        enabled = None
    selected = raw.get("selected")
    values = raw.get("values") if isinstance(raw.get("values"), dict) else {}
    samples = raw.get("samples") if isinstance(raw.get("samples"), dict) else {}
    return {
        "enabled": enabled,
        "selected": [_clip(item, 64) for item in selected[:20]] if isinstance(selected, list) else [],
        "values": {
            _clip(brand, 64): _str_map(brand_values, max_keys=80)
            for brand, brand_values in list(values.items())[:12]
            if isinstance(brand_values, dict)
        },
        "samples": {
            _clip(brand, 64): _str_map(brand_samples, value_limit=32, max_keys=80)
            for brand, brand_samples in list(samples.items())[:12]
            if isinstance(brand_samples, dict)
        },
    }


def _photos(raw, *, event) -> list[dict] | None:
    """None means the client has not finished restoring photos — keep the stored list."""
    if raw is None or "photos" not in raw:
        return None
    photos = raw.get("photos")
    if not isinstance(photos, list):
        return []
    prefix = f"recap_files/checkin/{event.uuid}/"
    out = []
    for photo in photos[:100]:
        if not isinstance(photo, dict):
            continue
        photo_id = _clip(photo.get("id"), 80)
        if not photo_id:
            continue
        blob = photo.get("blobName")
        if not isinstance(blob, str) or not blob.startswith(prefix):
            blob = None
        category = photo.get("category")
        out.append(
            {
                "id": photo_id,
                "blobName": blob,
                "category": category if isinstance(category, str) else None,
                "name": _clip(photo.get("name"), 200),
                "type": _clip(photo.get("type"), 80),
            }
        )
    return out


def clean_walkup_draft(raw, *, event, previous_photos: list | None = None) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    used = raw.get("usedCorpoCard")
    if used not in (True, False, None):
        used = None
    store = raw.get("store") if isinstance(raw.get("store"), dict) else None
    try:
        saved_at = int(raw.get("savedAt"))
    except (TypeError, ValueError):
        saved_at = 0
    photos = _photos(raw, event=event)
    payload = {
        "v": 1,
        "savedAt": saved_at,
        "values": _str_map(raw.get("values")),
        "samples": _str_map(raw.get("samples"), value_limit=32),
        "engagements": _clip(raw.get("engagements"), 32),
        "usedCorpoCard": used,
        "photos": previous_photos if photos is None else photos,
    }
    if store:
        payload["store"] = {
            "name": _clip(store.get("name"), 255),
            "number": _clip(store.get("number"), 64),
            "noNumber": bool(store.get("noNumber")),
        }
    co_brands = _co_brands(raw.get("coBrands"))
    if co_brands is not None and raw.get("coBrands") is not None:
        payload["coBrands"] = co_brands
    encoded = json.dumps(payload)
    if len(encoded) > _MAX_BYTES:
        payload.pop("coBrands", None)
        payload["photos"] = []
        encoded = json.dumps(payload)
    if len(encoded) > _MAX_BYTES:
        raise ValueError("draft too large")
    return payload


def _unfiled_recap(event, ambassador):
    """The empty stub for this shift, if one exists. Never a filed recap."""
    from recaps.filed import custom_filed_q, legacy_filed_q
    from recaps.models import CustomRecap, Recap

    custom = (
        CustomRecap.objects.filter(event=event, ambassador=ambassador)
        .exclude(custom_filed_q())
        .order_by("-id")
        .first()
    )
    if custom is not None:
        return custom, None
    legacy = (
        Recap.objects.filter(event=event, ambassador=ambassador)
        .exclude(legacy_filed_q())
        .order_by("-id")
        .first()
    )
    return None, legacy


def save_walkup_recap_draft(*, event, ambassador, scope: str | None, draft: dict) -> dict:
    """Persist answers. Does not submit, approve, or email."""
    from ambassadors.models import WalkupRecapDraft

    scope = _scope(scope)
    custom, legacy = _unfiled_recap(event, ambassador)
    with transaction.atomic():
        # Read the row we're about to write so a save that hasn't restored
        # photos yet cannot wipe blob names a parallel save just stored.
        qs = WalkupRecapDraft.objects.filter(
            event=event, ambassador=ambassador, scope=scope
        )
        if connection.vendor != "sqlite":
            qs = qs.select_for_update()
        existing = qs.first()
        previous_photos = None
        if existing is not None and isinstance(existing.payload, dict):
            previous_photos = existing.payload.get("photos") or []
        payload = clean_walkup_draft(
            draft, event=event, previous_photos=previous_photos
        )
        WalkupRecapDraft.objects.update_or_create(
            event=event,
            ambassador=ambassador,
            scope=scope,
            defaults={
                "payload": payload,
                "custom_recap": custom,
                "legacy_recap": legacy,
            },
        )
    return {
        "saved": True,
        "savedAt": payload["savedAt"],
        "filed": False,
        "approved": False,
        "linkedRecap": custom is not None or legacy is not None,
    }


def clear_walkup_recap_draft(*, event, ambassador, force_new: bool = False) -> None:
    """Drop the draft after a real submit so it cannot resurrect filed answers."""
    from ambassadors.models import WalkupRecapDraft

    scope = ADDITIONAL_SCOPE if force_new else SHIFT_SCOPE
    WalkupRecapDraft.objects.filter(
        event_id=event.id,
        ambassador_id=ambassador.id,
        scope=scope,
    ).delete()


def drafts_for_session(*, event, ambassador) -> dict:
    """Payloads to hand back on the check-in context. Omitted scopes are absent."""
    from ambassadors.models import WalkupRecapDraft

    rows = WalkupRecapDraft.objects.filter(event=event, ambassador=ambassador)
    out = {}
    for row in rows:
        if row.scope in _SCOPES and isinstance(row.payload, dict) and row.payload.get("v") == 1:
            out[row.scope] = row.payload
    return out
