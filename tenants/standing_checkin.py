"""Standing Check-In Studio write path.

Admin UI on ``/walkups`` edits photo buckets, BA resources (PDFs/links),
location mode, and which programs the standing link offers. The standing
``checkin_code`` is never accepted as input and is never rotated here —
minting stays on ``ensure_checkin_code`` / ``issueCheckinCode``.
"""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction

from tenants.models import (
    MAX_CHECKIN_RESOURCES,
    Tenant,
    normalize_checkin_resources,
)

SENTINEL_CATEGORY_NAMES = ("Sampling photos", "Receipts")


def _norm(name: str | None) -> str:
    """Fold a label for comparison — case and punctuation dropped."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def email_training_url(resources: list[dict]) -> str:
    """URL for ``Tenant.checkin_training_url`` (confirmation-email CTA).

    Prefers the first pdf, then the first link. Image-only resources (QR
    codes) are not useful in an email button.
    """
    for kind in ("pdf", "link"):
        for row in resources:
            if row.get("kind") == kind and (row.get("url") or "").strip():
                return str(row["url"]).strip()
    return ""


def _public_bucket(entry: dict) -> dict:
    item: dict[str, Any] = {"name": entry["name"]}
    helper = str(entry.get("helper") or "").strip()
    if helper:
        item["helper"] = helper[:200]
    try:
        min_n = int(entry.get("min") or 0)
    except (TypeError, ValueError):
        min_n = 0
    if min_n > 0:
        item["min"] = min_n
    accept = str(entry.get("accept") or "").strip()
    if accept:
        item["accept"] = accept[:120]
    return item


def parse_photo_buckets(raw: Any) -> dict[str, list[dict]] | list[dict] | None:
    """Normalize studio photo-bucket JSON into the model shape.

    Accepts a flat list of ``{name, helper?, min?, accept?}`` or a
    program-keyed map of the same. Returns ``None`` when the payload is
    empty / clearing. Raises ``ValueError`` on malformed input.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"photoBuckets is not valid JSON: {exc}") from exc

    def _entries(items: Any, label: str) -> list[dict]:
        if not isinstance(items, list):
            raise ValueError(f"photoBuckets {label} must be a list of bucket objects.")
        out: list[dict] = []
        for item in items:
            if isinstance(item, str):
                name = item.strip()
                if not name:
                    continue
                out.append({"name": name[:100]})
                continue
            if not isinstance(item, dict):
                raise ValueError("Each photo bucket must be an object with a name.")
            name = str(item.get("name") or "").strip()[:100]
            if not name:
                raise ValueError("Each photo bucket needs a name.")
            entry = {"name": name}
            helper = str(item.get("helper") or "").strip()
            if helper:
                entry["helper"] = helper[:200]
            if item.get("min") not in (None, "", 0, "0"):
                try:
                    entry["min"] = max(0, int(item["min"]))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Bucket {name!r} min must be a number.") from exc
            accept = str(item.get("accept") or "").strip()
            if accept:
                entry["accept"] = accept[:120]
            aliases = item.get("aliases")
            if isinstance(aliases, list):
                cleaned = [str(a).strip() for a in aliases if str(a).strip()]
                if cleaned:
                    entry["aliases"] = cleaned[:8]
            out.append(entry)
        return out

    if isinstance(raw, list):
        entries = _entries(raw, "list")
        return entries or None

    if isinstance(raw, dict):
        if not raw:
            return None
        wanted: dict[str, list[dict]] = {}
        for key, items in raw.items():
            program = str(key or "").strip()
            if not program:
                raise ValueError("photoBuckets map keys must be program names.")
            wanted[program] = _entries(items, f"[{program}]")
        if not any(wanted.values()):
            return None
        return wanted

    raise ValueError("photoBuckets must be a list or a program-keyed object.")


def ensure_bucket_categories(
    tenant: Tenant, wanted: dict[str, list[dict]] | list[dict]
) -> dict[str, list[dict]] | list[dict]:
    """Create/relabel ``FileRecapCategory`` rows and return public bucket config.

    Same rules as ``setup_tenant_checkin``: match case/punctuation-insensitively,
    relabel in place (keep photo history), never rename sentinel categories.
    """
    from recaps.models import FileRecapCategory

    if isinstance(wanted, list):
        programs_map: dict[str, list[dict]] = {"_flat": wanted}
        flat = True
    else:
        programs_map = wanted
        flat = False

    existing = list(
        FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id")
    )
    by_norm: dict[str, Any] = {}
    for cat in existing:
        by_norm.setdefault(_norm(cat.name), cat)

    plan: dict[str, dict] = {}
    for entries in programs_map.values():
        for spec in entries:
            name = spec["name"]
            key = _norm(name)
            if key in plan:
                continue
            match = by_norm.get(key)
            if match is None:
                for alias in spec.get("aliases") or []:
                    alias_hit = by_norm.get(_norm(alias))
                    if alias_hit is not None:
                        match = alias_hit
                        break
            if match is not None and any(
                _norm(match.name) == _norm(n) for n in SENTINEL_CATEGORY_NAMES
            ):
                # Never rename a sentinel out from under the upload path.
                match = None
            plan[key] = {**spec, "category": match}

    creator = getattr(tenant, "created_by", None)
    for entry in plan.values():
        name = entry["name"]
        cat = entry["category"]
        if cat is None:
            cat = FileRecapCategory.objects.create(
                name=name, tenant_id=tenant.id, created_by=creator
            )
            entry["category"] = cat
        elif cat.name != name:
            cat.name = name
            cat.save(update_fields=["name", "updated_at"])

    if flat:
        return [_public_bucket(e) for e in wanted]  # type: ignore[arg-type]
    return {
        key: [_public_bucket(e) for e in entries]
        for key, entries in programs_map.items()
    }


def apply_resources(
    tenant: Tenant, resources: list[dict] | None, *, clear: bool
) -> list[str]:
    """Write ``checkin_resources`` (+ sync training URL). Returns changed fields."""
    if clear:
        if tenant.checkin_resources is None:
            return []
        tenant.checkin_resources = None
        return ["checkin_resources"]

    if resources is None:
        return []

    cleaned = normalize_checkin_resources(resources)
    if len(cleaned) != len(resources):
        raise ValueError(
            f"{len(resources) - len(cleaned)} of {len(resources)} resources were "
            "rejected (need a label, kind link|pdf|image, and an http(s) or "
            "root-relative url)."
        )
    if len(cleaned) > MAX_CHECKIN_RESOURCES:
        raise ValueError(
            f"{len(cleaned)} resources exceeds the {MAX_CHECKIN_RESOURCES} "
            "the check-in page will render."
        )

    changed: list[str] = []
    current = normalize_checkin_resources(getattr(tenant, "checkin_resources", None))
    desired = cleaned or None
    if current != (desired or []):
        tenant.checkin_resources = desired
        changed.append("checkin_resources")

    if cleaned:
        email_url = email_training_url(cleaned)
        if email_url and (tenant.checkin_training_url or "").strip() != email_url:
            tenant.checkin_training_url = email_url
            changed.append("checkin_training_url")
    return changed


def apply_location_mode(tenant: Tenant, mode: str | None) -> list[str]:
    if mode is None:
        return []
    cleaned = str(mode).strip().lower()
    allowed = {Tenant.CHECKIN_LOCATION_ADDRESS, Tenant.CHECKIN_LOCATION_MARKET}
    if cleaned not in allowed:
        raise ValueError("locationMode must be 'address' or 'market'.")
    if getattr(tenant, "checkin_timesheet_only", False) and cleaned == Tenant.CHECKIN_LOCATION_MARKET:
        raise ValueError(
            "Market location mode is not available on a timesheet-only check-in."
        )
    if tenant.checkin_location_mode == cleaned:
        return []
    tenant.checkin_location_mode = cleaned
    return ["checkin_location_mode"]


def apply_event_types(tenant: Tenant, event_type_ids: list[int] | None) -> list[str]:
    """Pin default + replace selectable programs. First id = default."""
    if event_type_ids is None:
        return []
    if not event_type_ids:
        raise ValueError("Pick at least one program for the standing link.")

    from events.models import EventType

    # Preserve caller order (first = default pin).
    ordered: list[Any] = []
    seen: set[int] = set()
    for pk in event_type_ids:
        if pk in seen:
            continue
        seen.add(pk)
        et = EventType.objects.filter(id=pk, tenant_id=tenant.id).first()
        if et is None:
            raise ValueError(f"Program id {pk} is not on this brand.")
        ordered.append(et)

    changed: list[str] = []
    default = ordered[0]
    if tenant.checkin_event_type_id != default.id:
        tenant.checkin_event_type = default
        changed.append("checkin_event_type")
    # M2M set always — compare before write to avoid noisy updated_at.
    current_ids = list(
        tenant.checkin_event_types.order_by("id").values_list("id", flat=True)
    )
    desired_ids = [et.id for et in ordered]
    if current_ids != desired_ids:
        tenant.checkin_event_types.set(ordered)
        # M2M is not a Tenant column; signal via synthetic name for callers.
        changed.append("checkin_event_types")
    return changed


def update_standing_checkin_config(
    tenant: Tenant,
    *,
    location_mode: str | None = None,
    photo_buckets: Any = None,
    resources: list[dict] | None = None,
    clear_resources: bool = False,
    event_type_ids: list[int] | None = None,
    photo_buckets_provided: bool = False,
) -> dict[str, Any]:
    """Apply Studio edits. Never touches ``checkin_code``.

    ``photo_buckets_provided`` distinguishes "omit" from "clear to generic
    grid" (pass ``[]`` / ``{}`` with the flag True).
    """
    with transaction.atomic():
        # Re-fetch under lock so concurrent CLI/setup cannot race a remint
        # into this write window — we still never write the code column.
        locked = Tenant.objects.select_for_update().get(pk=tenant.pk)
        changed: list[str] = []
        changed.extend(apply_location_mode(locked, location_mode))

        if photo_buckets_provided:
            parsed = parse_photo_buckets(photo_buckets)
            if parsed is None:
                if locked.checkin_photo_buckets is not None:
                    locked.checkin_photo_buckets = None
                    changed.append("checkin_photo_buckets")
            else:
                config = ensure_bucket_categories(locked, parsed)
                if locked.checkin_photo_buckets != config:
                    locked.checkin_photo_buckets = config
                    changed.append("checkin_photo_buckets")

        changed.extend(
            apply_resources(locked, resources, clear=clear_resources)
        )
        et_changed = apply_event_types(locked, event_type_ids)
        # Persist FK/scalar columns; M2M already written inside apply_event_types.
        scalar = [c for c in changed + et_changed if c != "checkin_event_types"]
        if scalar:
            # Keep updated_at in sync with other tenant writes.
            if "updated_at" not in scalar and hasattr(locked, "updated_at"):
                scalar.append("updated_at")
            locked.save(update_fields=list(dict.fromkeys(scalar)))
        elif "checkin_event_types" in et_changed:
            # Touch updated_at so admin UIs refetch a fresh stamp.
            if hasattr(locked, "updated_at"):
                locked.save(update_fields=["updated_at"])

        locked.refresh_from_db()
        return {
            "tenant": locked,
            "changed": list(dict.fromkeys(changed + et_changed)),
            "checkin_code": (locked.checkin_code or "").strip() or None,
        }
