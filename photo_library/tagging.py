"""Tag every recap photo once, for the photo library.

Per photo, in order, cheapest first:

1. No download, no model: a photo in a receipt / spend bucket, one the
   receipt reader read, or one the scorer called a receipt is stored as
   ``excluded`` (a receipt is never in the library).
2. No model: the same file (content hash) already tagged for the brand, or
   a photo whose tags the best-photo scorer already wrote from its own call
   (``recaps/photo_picks.py`` uses the same vision call, see
   :mod:`photo_library.vision`), is copied, not looked at again.
3. Downloaded, still no model: a near copy (difference hash) of a photo on
   the same recap that is already tagged takes that photo's tags.
4. Only then one vision call on a downscaled copy.

A photo is tagged again only when its file changes (stored path or content
hash). Each run is capped by model calls and seconds; what is left waits for
the next run. Dry run by default: counts what would be done, downloads
nothing, calls nothing, stores nothing.

Tags are written for photos on every live recap (approved or not, never
archived), so staff can search the whole field. Who may see what is decided
at search time (:mod:`photo_library.search`), not here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.contrib.postgres.search import SearchVector
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.functions import Lower
from django.utils import timezone

from photo_library.vision import (
    SYSTEM,
    USER,
    catalog_labels,
    library_tags,
    schema_for,
    stored_reply,
)
from photo_library.vocabulary import (
    CROWD_LABELS,
    SETTING_LABELS,
    SHOW_LABELS,
    US_STATES,
    labels_for,
)
from utils.ai_text import AiUnavailable, generate_vision_json

logger = logging.getLogger(__name__)

DEFAULT_RUN_CALLS = 100
MAX_RUN_CALLS = 500
DEFAULT_RUN_SECONDS = 240
# Files looked at per run (most need no call once the backlog is done).
DEFAULT_RUN_FILES = 400
MAX_CONSECUTIVE_FAILURES = 3
VISION_MAX_DIM = 1024
VISION_QUALITY = 72
NEAR_COPY_BITS = 5

ORIGIN_VISION = "vision"
ORIGIN_COPIED = "copied"
ORIGIN_RULES = "rules"


# ─── Search text ────────────────────────────────────────────────────


def _location_words(recap) -> list[str]:
    """Store, chain, city and state words for one recap: what a person
    would type to find it. Never the ambassador or the file name."""
    from recaps.client_recap_draft import _store_name

    event = getattr(recap, "event", None)
    words: list[str] = []
    for value in (
        getattr(getattr(recap, "retailer", None), "name", None),
        _store_name(event) if event is not None else "",
        getattr(recap, "typed_store_name", ""),
        getattr(getattr(recap, "location", None), "name", None),
    ):
        value = " ".join(str(value or "").split())
        if value and value not in words:
            words.append(value)
    if getattr(recap, "store_number", ""):
        words.append(f"#{recap.store_number}")
    for state in (getattr(recap, "state", None), getattr(event, "state", None)):
        code = (getattr(state, "code", "") or "").strip().upper()
        if code:
            words += [code, US_STATES.get(code, getattr(state, "name", "") or "")]
            break
    return [w for w in words if w]


def search_text_for(recap_file, values: dict) -> str:
    recap = recap_file.custom_recap
    parts = [
        values.get("caption") or "",
        " ".join(labels_for(values.get("shows"), SHOW_LABELS)),
        CROWD_LABELS.get(values.get("crowd_size") or "", "") if values.get("people_present") else "",
        "crowd consumers shoppers" if values.get("crowd_size") in ("group", "crowd") else "",
        SETTING_LABELS.get(values.get("setting") or "", ""),
        " ".join(values.get("product_names") or []),
        " ".join(_location_words(recap)),
        getattr(getattr(recap_file, "file_recap_category", None), "name", None) or "",
    ]
    return " ".join(" ".join(p.split()) for p in parts if p).strip()[:2000]


# ─── Writes ─────────────────────────────────────────────────────────


def source_of(recap_file) -> tuple[str, str]:
    from recaps.photo_picks import _stored_path

    return _stored_path(recap_file), (recap_file.content_sha256 or "").strip().lower()


def save_tags(recap_file, values: dict, *, origin: str, vision: dict | None = None, **extra):
    """Store one photo's tags and rebuild its search vector."""
    from photo_library.models import RecapPhotoTag

    path, sha = source_of(recap_file)
    status = RecapPhotoTag.STATUS_EXCLUDED if values.get("excluded") else RecapPhotoTag.STATUS_TAGGED
    defaults = {
        "tenant_id": recap_file.custom_recap.tenant_id,
        "status": extra.pop("status", status),
        "excluded_reason": values.get("excluded_reason") or "",
        "shows": values.get("shows") or [],
        "product_ids": values.get("product_ids") or [],
        "product_names": values.get("product_names") or [],
        "people_present": values.get("people_present"),
        "crowd_size": values.get("crowd_size") or "",
        "setting": values.get("setting") or "",
        "issues": values.get("issues") or [],
        "quality_score": values.get("quality_score"),
        "caption": (values.get("caption") or "")[:140],
        "source_path": path[:1024],
        "source_sha": sha,
        "origin": origin,
        "vision": vision or {},
        **extra,
    }
    defaults["search_text"] = (
        "" if defaults["status"] != RecapPhotoTag.STATUS_TAGGED else search_text_for(recap_file, values)
    )
    row, _ = RecapPhotoTag.objects.update_or_create(
        custom_recap_file=recap_file, defaults=defaults
    )
    RecapPhotoTag.objects.filter(pk=row.pk).update(
        search_vector=SearchVector("search_text", config="english")
    )
    return row


def save_from_scorer(recap_file, parsed: dict, *, names, width, height, dhash) -> bool:
    """Called by the best-photo scorer after its own vision call, so the
    photo library never makes a second call for the same photo. False when
    the reply carried no library fields. Never raises."""
    try:
        catalog = catalog_labels(recap_file.custom_recap.tenant_id)
        values = library_tags(parsed, catalog, names=names, width=width, height=height)
        if values is None:
            return False
        save_tags(
            recap_file,
            values,
            origin=ORIGIN_VISION,
            vision=stored_reply(parsed),
            width=width,
            height=height,
            dhash=dhash or "",
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the library must never break scoring
        logger.warning("photo library: could not store scorer tags for %s: %s", recap_file.id, exc)
        return False


def reusable_reply(recap_file) -> dict | None:
    """The stored vision reply for this exact file, for the scorer to reuse
    instead of calling the model, with the cleaned caption."""
    from recaps.photo_picks import _related

    tag = _related(recap_file, "library_tag")
    if tag is None or tag.origin != ORIGIN_VISION or not tag.vision.get("kind"):
        return None
    path, sha = source_of(recap_file)
    if tag.source_path != path or tag.source_sha != sha:
        return None
    return {**tag.vision, "caption": tag.caption}


# ─── Reads ──────────────────────────────────────────────────────────


def _image_q(prefix: str = "") -> Q:
    from recaps.photo_picks import _image_q as image_q

    return image_q(prefix)


def pending_files(*, tenant_id: int | None = None):
    """Images on live, non-archived custom recaps that have no tags, or
    whose file changed since they were tagged. Approved recaps first, then
    newest event."""
    from photo_library.models import RecapPhotoTag
    from recaps.models import CustomRecapFile
    from recaps.scoped_ai import _live_request_q

    fresh = RecapPhotoTag.objects.filter(
        custom_recap_file_id=OuterRef("pk"),
        source_path=OuterRef("url"),
        source_sha=OuterRef("content_sha256_lower"),
    )
    qs = (
        CustomRecapFile.objects.filter(
            custom_recap__isnull=False,
            custom_recap__archived_at__isnull=True,
        )
        .filter(_live_request_q("custom_recap__event__"))
        .filter(_image_q())
        .annotate(content_sha256_lower=Lower("content_sha256"))
        .filter(~Exists(fresh))
    )
    if tenant_id:
        qs = qs.filter(custom_recap__tenant_id=tenant_id)
    return qs.order_by(
        F("custom_recap__approved").desc(),
        F("custom_recap__event__date").desc(nulls_last=True),
        "-custom_recap_id",
        "id",
    )


# ─── The run ────────────────────────────────────────────────────────


@dataclass
class Budget:
    calls: int = DEFAULT_RUN_CALLS
    seconds: float = DEFAULT_RUN_SECONDS
    started: float = field(default_factory=lambda: time.monotonic())
    used: int = 0

    def spent(self) -> bool:
        if self.used >= self.calls:
            return True
        return bool(self.seconds and time.monotonic() - self.started > self.seconds)


class _Stop(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _copy_values(tag) -> dict:
    return {
        "shows": list(tag.shows),
        "product_ids": list(tag.product_ids),
        "product_names": list(tag.product_names),
        "people_present": tag.people_present,
        "crowd_size": tag.crowd_size,
        "setting": tag.setting,
        "issues": list(tag.issues),
        "quality_score": tag.quality_score,
        "caption": tag.caption,
        "excluded": tag.status == "excluded",
        "excluded_reason": tag.excluded_reason,
    }


def _twin_by_hash(recap_file):
    """A tagged photo of the same brand with the same bytes."""
    from photo_library.models import RecapPhotoTag

    _path, sha = source_of(recap_file)
    if not sha:
        return None
    return (
        RecapPhotoTag.objects.filter(
            tenant_id=recap_file.custom_recap.tenant_id,
            source_sha=sha,
            status__in=("tagged", "excluded"),
        )
        .exclude(custom_recap_file_id=recap_file.id)
        .order_by("id")
        .first()
    )


def _near_twin(recap_file, dhash: str):
    from photo_library.models import RecapPhotoTag
    from recaps.photo_picks import hash_distance

    rows = RecapPhotoTag.objects.filter(
        custom_recap_file__custom_recap_id=recap_file.custom_recap_id,
        status="tagged",
        origin=ORIGIN_VISION,
    ).exclude(dhash="")
    for row in rows:
        if hash_distance(dhash, row.dhash) <= NEAR_COPY_BITS:
            return row
    return None


def _empty_report(dry_run: bool) -> dict:
    return {
        "dry_run": dry_run,
        "looked_at": 0,
        "tagged": 0,
        "copied": 0,
        "excluded": 0,
        "unreadable": 0,
        "model_calls": 0,
        "would_call": 0,
        "errors": 0,
        "stopped": "",
    }


def tag_photo(recap_file, *, budget: Budget, report: dict, state: dict, dry_run: bool) -> None:
    """Tag one photo, cheapest way first. Raises :class:`_Stop` when the
    model cannot be used at all."""
    from photo_library.models import RecapPhotoTag
    from recaps.pdf import downscale_image_bytes
    from recaps.photo_picks import _open, difference_hash, is_receipt
    from recaps.scoped_ai import _ba_names

    report["looked_at"] += 1
    if is_receipt(recap_file):
        report["excluded"] += 1
        if not dry_run:
            save_tags(
                recap_file,
                {"shows": ["receipt"], "excluded": True, "excluded_reason": "receipt"},
                origin=ORIGIN_RULES,
            )
        return
    twin = _twin_by_hash(recap_file)
    if twin is not None:
        report["copied"] += 1
        if not dry_run:
            save_tags(
                recap_file,
                _copy_values(twin),
                origin=ORIGIN_COPIED,
                width=twin.width,
                height=twin.height,
                dhash=twin.dhash,
            )
        return
    if dry_run:
        report["would_call"] += 1
        return

    data, image = _open(recap_file)
    if data is None or image is None:
        report["unreadable"] += 1
        save_tags(
            recap_file,
            {"excluded": True, "excluded_reason": "missing_file" if data is None else "not_an_image"},
            origin=ORIGIN_RULES,
            status=RecapPhotoTag.STATUS_UNREADABLE,
        )
        return
    width, height = image.size
    dhash = difference_hash(image)
    near = _near_twin(recap_file, dhash)
    if near is not None:
        report["copied"] += 1
        values = _copy_values(near)
        save_tags(recap_file, values, origin=ORIGIN_COPIED, width=width, height=height, dhash=dhash)
        return

    tenant_id = recap_file.custom_recap.tenant_id
    catalog = state["catalogs"].get(tenant_id)
    if catalog is None:
        catalog = state["catalogs"][tenant_id] = catalog_labels(tenant_id)
    names = state["names"].get(recap_file.custom_recap_id)
    if names is None:
        names = state["names"][recap_file.custom_recap_id] = _ba_names([recap_file.custom_recap])

    small = downscale_image_bytes(data, max_dim=VISION_MAX_DIM, quality=VISION_QUALITY)
    budget.used += 1
    report["model_calls"] += 1
    try:
        parsed = generate_vision_json(
            SYSTEM,
            USER,
            image_bytes=small or data,
            content_type="image/jpeg",
            schema=schema_for(catalog),
            max_tokens=500,
            small=True,
        )
    except AiUnavailable as exc:
        raise _Stop("ai_unavailable") from exc
    values = library_tags(parsed or {}, catalog, names=names, width=width, height=height)
    if values is None:
        # Unusable reply. Record the photo as unreadable only once another
        # photo has proved the model is up, so an outage is retried.
        report["errors"] += 1
        state["failed"].append((recap_file, width, height, dhash))
        state["failures_in_a_row"] += 1
        if state["failures_in_a_row"] >= MAX_CONSECUTIVE_FAILURES and not state["worked"]:
            raise _Stop("model_failing")
        return
    state["worked"] = True
    state["failures_in_a_row"] = 0
    save_tags(
        recap_file,
        values,
        origin=ORIGIN_VISION,
        vision=stored_reply(parsed),
        width=width,
        height=height,
        dhash=dhash,
    )
    if values.get("excluded"):
        report["excluded"] += 1
    else:
        report["tagged"] += 1


def tag_pending_photos(
    *,
    dry_run: bool = True,
    tenant_id: int | None = None,
    max_calls: int = DEFAULT_RUN_CALLS,
    max_seconds: int = DEFAULT_RUN_SECONDS,
    max_files: int = DEFAULT_RUN_FILES,
) -> dict:
    """Tag up to ``max_files`` pending photos within ``max_calls`` model
    calls and ``max_seconds``. Safe to re-run: a photo whose file has not
    changed is never looked at again."""
    budget = Budget(
        calls=max(0, min(int(max_calls), MAX_RUN_CALLS)), seconds=max(0, int(max_seconds))
    )
    report = _empty_report(dry_run)
    report["pending"] = pending_files(tenant_id=tenant_id).count()
    state = {"catalogs": {}, "names": {}, "failed": [], "failures_in_a_row": 0, "worked": False}
    files = (
        pending_files(tenant_id=tenant_id)
        .select_related(
            "file_recap_category",
            "photo_score",
            "receipt_reading",
            "custom_recap__retailer",
            "custom_recap__location",
            "custom_recap__state",
            "custom_recap__event__retailer",
            "custom_recap__event__state",
            "custom_recap__event__request__retailer",
            "custom_recap__ambassador__user",
        )[: max(1, int(max_files))]
    )
    for recap_file in files:
        if not dry_run and budget.spent():
            report["stopped"] = "budget"
            break
        if dry_run and report["would_call"] >= budget.calls:
            report["stopped"] = "budget"
            break
        try:
            tag_photo(recap_file, budget=budget, report=report, state=state, dry_run=dry_run)
        except _Stop as stop:
            report["stopped"] = stop.reason
            break
        except Exception as exc:  # noqa: BLE001 — one bad photo must not end the run
            logger.exception("photo library: file %s failed: %s", recap_file.id, exc)
            report["errors"] += 1
    if state["worked"]:
        from photo_library.models import RecapPhotoTag

        for recap_file, width, height, dhash in state["failed"]:
            save_tags(
                recap_file,
                {"excluded": True, "excluded_reason": "unusable_reply"},
                origin=ORIGIN_RULES,
                status=RecapPhotoTag.STATUS_UNREADABLE,
                width=width,
                height=height,
                dhash=dhash,
            )
            report["unreadable"] += 1
    report["budget_calls"] = budget.calls
    report["finished_at"] = timezone.now().isoformat()
    return report
