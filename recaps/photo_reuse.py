"""Flag the same stored photo on a different recap.

This is a check, not a score. It compares SHA-256 digests and stored file
paths. It does not call a model, and it does not approve, reject, or edit a
recap.

Co-activation slices share photos on purpose (one activation, one file copied
onto each brand). Those copies are the same family and are not flagged.
"""

from __future__ import annotations

import re
from datetime import datetime

from recaps import models as rmodels

_SHA_RE = re.compile(r"^[a-f0-9]{64}$")


def normalize_content_sha256(raw: object) -> str:
    """64 lowercase hex chars, or empty when the client didn't send a real hash."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip().lower()
    return text if _SHA_RE.fullmatch(text) else ""


def _stored_path(file_field) -> str:
    if not file_field:
        return ""
    name = getattr(file_field, "name", None)
    text = (name if isinstance(name, str) else str(file_field) or "").strip()
    return text


def _event_day(event) -> str:
    start = getattr(event, "start_time", None) if event is not None else None
    if isinstance(start, datetime):
        return start.date().isoformat()
    return ""


def _family_ids(recap: rmodels.CustomRecap) -> set[int]:
    """This recap plus every co-activation slice of the same activation."""
    ids = {recap.id}
    key = recap.co_activation_key
    if key:
        ids.update(
            rmodels.CustomRecap.objects.filter(co_activation_key=key).values_list(
                "id", flat=True
            )
        )
    parent_id = recap.co_activation_parent_id
    if parent_id:
        ids.add(parent_id)
        ids.update(
            rmodels.CustomRecap.objects.filter(
                co_activation_parent_id=parent_id
            ).values_list("id", flat=True)
        )
    ids.update(
        rmodels.CustomRecap.objects.filter(co_activation_parent_id=recap.id).values_list(
            "id", flat=True
        )
    )
    return ids


def _label(day: str, own_day: str, count: int) -> str:
    if count > 1:
        return f"Same photo on {count} other recaps."
    if day and own_day and day != own_day:
        return f"Same photo on a recap from {day}."
    if day and own_day and day == own_day:
        return "Same photo on another recap the same day."
    return "Same photo on another recap."


def _matches_for(
    *,
    digest: str,
    path: str,
    own_custom_ids: set[int],
    own_legacy_id: int | None,
    own_day: str,
) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(kind: str, recap_id: int, day: str, file_url: str) -> None:
        key = (kind, recap_id)
        if key in seen:
            return
        seen.add(key)
        found.append(
            {
                "recapId": str(recap_id),
                "eventDate": day,
                "fileUrl": file_url,
            }
        )

    if digest:
        custom_hash = (
            rmodels.CustomRecapFile.objects.filter(content_sha256=digest)
            .exclude(custom_recap_id__in=own_custom_ids or {0})
            .select_related("custom_recap__event")
        )
        for row in custom_hash:
            if row.custom_recap_id is None:
                continue
            add(
                "custom",
                row.custom_recap_id,
                _event_day(row.custom_recap.event),
                _stored_path(row.url),
            )
        legacy_hash = rmodels.RecapFile.objects.filter(content_sha256=digest).select_related(
            "recap__event"
        )
        if own_legacy_id is not None:
            legacy_hash = legacy_hash.exclude(recap_id=own_legacy_id)
        for row in legacy_hash:
            if row.recap_id is None:
                continue
            add("legacy", row.recap_id, _event_day(row.recap.event), _stored_path(row.file))

    if path:
        custom_url = (
            rmodels.CustomRecapFile.objects.filter(url=path)
            .exclude(custom_recap_id__in=own_custom_ids or {0})
            .select_related("custom_recap__event")
        )
        for row in custom_url:
            if row.custom_recap_id is None:
                continue
            add(
                "custom",
                row.custom_recap_id,
                _event_day(row.custom_recap.event),
                _stored_path(row.url),
            )
        legacy_url = rmodels.RecapFile.objects.filter(file=path).select_related("recap__event")
        if own_legacy_id is not None:
            legacy_url = legacy_url.exclude(recap_id=own_legacy_id)
        for row in legacy_url:
            if row.recap_id is None:
                continue
            add("legacy", row.recap_id, _event_day(row.recap.event), _stored_path(row.file))

    # Stable, date-aware label is built by the caller from `found`.
    del own_day
    return found


def photo_reuse_checks(recap_id: int, *, is_custom: bool) -> dict:
    """One check per photo on this recap. Read-only. No score."""
    items: list[dict] = []
    if is_custom:
        recap = (
            rmodels.CustomRecap.objects.filter(id=recap_id)
            .select_related("event")
            .first()
        )
        if recap is None:
            return {"items": []}
        family = _family_ids(recap)
        own_day = _event_day(recap.event)
        files = list(rmodels.CustomRecapFile.objects.filter(custom_recap_id=recap.id))
        for photo in files:
            path = _stored_path(photo.url)
            digest = normalize_content_sha256(photo.content_sha256)
            matches = _matches_for(
                digest=digest,
                path=path,
                own_custom_ids=family,
                own_legacy_id=None,
                own_day=own_day,
            )
            day = matches[0]["eventDate"] if matches else ""
            items.append(
                {
                    "fileId": str(photo.id),
                    "fileUrl": path,
                    "status": "reused" if matches else "clear",
                    "label": (
                        _label(day, own_day, len(matches)) if matches else "Not reused"
                    ),
                    "matches": matches,
                }
            )
        return {"items": items}

    recap = rmodels.Recap.objects.filter(id=recap_id).select_related("event").first()
    if recap is None:
        return {"items": []}
    own_day = _event_day(recap.event)
    files = list(rmodels.RecapFile.objects.filter(recap_id=recap.id))
    for photo in files:
        path = _stored_path(photo.file)
        digest = normalize_content_sha256(photo.content_sha256)
        matches = _matches_for(
            digest=digest,
            path=path,
            own_custom_ids=set(),
            own_legacy_id=recap.id,
            own_day=own_day,
        )
        day = matches[0]["eventDate"] if matches else ""
        items.append(
            {
                "fileId": str(photo.id),
                "fileUrl": path,
                "status": "reused" if matches else "clear",
                "label": _label(day, own_day, len(matches)) if matches else "Not reused",
                "matches": matches,
            }
        )
    return {"items": items}
