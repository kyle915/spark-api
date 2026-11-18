"""Pick the photos on an approved recap that belong in a client report.

A background job looks at each approved recap's photos once and stores, per
photo, a score, a short caption and whether it is one of the recap's best
three or four (:class:`recaps.models.RecapPhotoScore`). Staff can pin or
unpin any photo; their choice is stored on its own row
(:class:`recaps.models.RecapPhotoStaffPick`) and always wins.

Most photos never reach a model. In order, per recap:

1. No download: a photo in a receipt / spend bucket, or one the receipt
   reader already read, is set aside (a receipt is never a pick). So is the
   same file twice on the recap (stored hash or path), and a photo that also
   sits on another recap (:func:`recaps.photo_reuse.photo_reuse_checks`).
2. At most :data:`MAX_PHOTOS_PER_RECAP` of what is left are opened, spread
   across the template's photo buckets. The rest are set aside as over the
   limit.
3. Opened, still no model: too small to print, or a near-copy of a shot
   already kept (difference hash).
4. Only then one vision call per photo, on a downscaled copy, through
   :func:`utils.ai_text.generate_vision_json`.

The model is asked about the setup, the product and the setting. It is told
not to identify or describe anyone, and a caption that mentions a person is
replaced with a plain one before it is stored.

Without an API key nothing is scored and nothing is stored for the photos
that needed the model: the manual path (staff pinning photos) is all there
is, and a later run picks the photos up. This module sends nothing and
changes no recap.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from io import BytesIO

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from photo_library import vision as shared_vision
from recaps.photo_check import _KIND_WORDS
from recaps.scoped_ai import _ba_names, _hide_names, _live_request_q
from utils.ai_text import AiUnavailable, generate_vision_json

logger = logging.getLogger(__name__)

# ─── Caps ───────────────────────────────────────────────────────────

# Photos opened (downloaded) per recap, ever. Bounds both storage reads and
# vision calls: a recap costs at most this many calls.
MAX_PHOTOS_PER_RECAP = 12
MAX_PICKS_PER_RECAP = 4
# A photo scoring under this is never an automatic pick.
MIN_PICK_SCORE = 55
# Shorter side, in pixels. Under this a photo prints soft at deck size.
MIN_SIDE_PX = 600
# Longest side sent to the model.
VISION_MAX_DIM = 1024
VISION_QUALITY = 72
# Bits (of 64) two difference hashes may differ by and still be one shot.
NEAR_DUPLICATE_BITS = 5

# Per run of the job.
DEFAULT_RUN_PHOTOS = 60
MAX_RUN_PHOTOS = 200
DEFAULT_RUN_SECONDS = 200
DEFAULT_RUN_RECAPS = 40
DEFAULT_SINCE_DAYS = 45
# Model replies that came back unusable in a row before the run gives up.
MAX_CONSECUTIVE_FAILURES = 3

STATUS_SCORED = "scored"
STATUS_SKIPPED = "skipped"
STATUS_UNREADABLE = "unreadable"
STATUS_NOT_SCORED = "not_scored"

SKIP_RECEIPT = "receipt"
SKIP_DUPLICATE = "duplicate"
SKIP_REUSED = "reused"
SKIP_OVER_LIMIT = "over_limit"
SKIP_LOW_RESOLUTION = "low_resolution"
SKIP_NEAR_DUPLICATE = "near_duplicate"
SKIP_SCREENSHOT = "screenshot"
SKIP_NOT_AN_IMAGE = "not_an_image"
SKIP_MISSING_FILE = "missing_file"

SKIP_LABELS = {
    SKIP_RECEIPT: "Receipt",
    SKIP_DUPLICATE: "Same file twice on this recap",
    SKIP_REUSED: "Also on another recap",
    SKIP_OVER_LIMIT: "Not looked at: over the photo limit",
    SKIP_LOW_RESOLUTION: "Too small to print well",
    SKIP_NEAR_DUPLICATE: "Near copy of another photo",
    SKIP_SCREENSHOT: "Screenshot",
    SKIP_NOT_AN_IMAGE: "Could not be opened",
    SKIP_MISSING_FILE: "File not found",
}

# Rows that used one of the recap's MAX_PHOTOS_PER_RECAP openings.
_OPENED_SKIPS = (SKIP_LOW_RESOLUTION, SKIP_NEAR_DUPLICATE, SKIP_SCREENSHOT, SKIP_NOT_AN_IMAGE)

KINDS = (
    "table_setup",
    "product_display",
    "shelf",
    "sampling",
    "signage",
    "store_exterior",
    "receipt",
    "screenshot",
    "other",
)
_NEVER_A_PICK = frozenset({"receipt", "screenshot"})

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")

# One vision call per photo serves both this scorer and the photo library
# (see photo_library/vision.py): the prompt and schema live there, so the
# reply also carries the library tags and the library never calls again.
PHOTO_SYSTEM = shared_vision.SYSTEM
PHOTO_USER = shared_vision.USER

KIND_CAPTIONS = {
    "table_setup": "Sampling table set up",
    "product_display": "Product display",
    "shelf": "Product on the shelf",
    "sampling": "Sampling in progress",
    "signage": "Branded signage",
    "store_exterior": "Store front",
    "other": "Field photo",
}

# A caption that talks about a person is thrown away, whatever else it says.
_PERSON_RE = re.compile(
    r"\b("
    r"person|people|man|men|woman|women|girl|girls|boy|boys|lady|ladies|guy|guys|"
    r"gentleman|child|children|kid|kids|baby|teen|teens|adult|adults|"
    r"he|she|him|her|his|hers|they|them|their|someone|somebody|"
    r"ambassador|ambassadors|ba|employee|employees|worker|workers|staff|rep|"
    r"customer|customers|shopper|shoppers|consumer|consumers|crowd|"
    r"smil\w*|laugh\w*|wearing|dressed|posing|poses|"
    r"hair|haired|face|faces|beard|young|elderly|male|female|blonde|brunette"
    r")\b",
    re.IGNORECASE,
)
MAX_CAPTION_CHARS = 90


# ─── Pure helpers ───────────────────────────────────────────────────


def category_kind(name: str | None) -> str:
    """The photo bucket's name as one of the scorer's broad kinds."""
    lowered = (name or "").strip().lower()
    if not lowered:
        return "other"
    for kind in ("receipt", "sampling", "table_setup", "shelf"):
        if any(word in lowered for word in _KIND_WORDS[kind]):
            return kind
    return "other"


def clean_caption(raw, kind: str, names: list[str] | None = None) -> str:
    """The model's caption, or a plain one for ``kind`` when it is unusable.

    Unusable: empty, or it mentions a person. A known ambassador's name is
    replaced first, so a caption can never carry one.
    """
    fallback = KIND_CAPTIONS.get(kind, KIND_CAPTIONS["other"])
    text = " ".join(str(raw or "").split()).strip(" .\"'“”")
    if not text:
        return fallback
    if names:
        hidden = _hide_names(text, names)
        if hidden != text:
            return fallback
    if _PERSON_RE.search(text):
        return fallback
    if len(text) > MAX_CAPTION_CHARS:
        text = text[: MAX_CAPTION_CHARS - 1].rstrip() + "…"
    return text[0].upper() + text[1:]


def interpret_score(parsed: dict | None, names: list[str] | None = None) -> dict | None:
    """Model output → ``{"status", "skip_reason", "score", "kind", "caption"}``.

    None when the reply is not usable. The score is the model's usefulness
    with ceilings applied here, so a blurry or dark photo cannot rank high
    whatever number came back.
    """
    if not isinstance(parsed, dict):
        return None
    kind = parsed.get("kind")
    if kind not in KINDS:
        return None
    try:
        usefulness = int(parsed.get("usefulness"))
    except (TypeError, ValueError):
        return None
    if kind in _NEVER_A_PICK:
        return {
            "status": STATUS_SKIPPED,
            "skip_reason": SKIP_RECEIPT if kind == "receipt" else SKIP_SCREENSHOT,
            "score": 0,
            "kind": kind,
            "caption": "",
        }
    score = max(0, min(100, usefulness))
    if parsed.get("sharp") is not True:
        score = min(score, 30)
    if parsed.get("wellLit") is not True:
        score = min(score, 50)
    if parsed.get("productVisible") is not True and parsed.get("brandingVisible") is not True:
        score = min(score, 60)
    return {
        "status": STATUS_SCORED,
        "skip_reason": "",
        "score": score,
        "kind": kind,
        "caption": clean_caption(parsed.get("caption"), kind, names),
    }


def difference_hash(image) -> str:
    """64-bit dHash of a PIL image as 16 hex chars."""
    from PIL import Image

    small = image.convert("L").resize((9, 8), Image.LANCZOS)
    pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def hash_distance(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def choose_picks(rows: list[dict]) -> list[int]:
    """Ids of the recap's best photos, at most :data:`MAX_PICKS_PER_RECAP`.

    ``rows`` are ``{"id", "score", "kind"}`` for scored photos. The best of
    each kind goes first, so four table shots do not crowd out the shelf;
    then the highest scores fill what is left.
    """
    eligible = sorted(
        (
            r
            for r in rows
            if r.get("score") is not None
            and int(r["score"]) >= MIN_PICK_SCORE
            and r.get("kind") not in _NEVER_A_PICK
        ),
        key=lambda r: (-int(r["score"]), r["id"]),
    )
    chosen: list[dict] = []
    kinds: set[str] = set()
    for row in eligible:
        if row.get("kind") in kinds:
            continue
        kinds.add(row.get("kind"))
        chosen.append(row)
    chosen = chosen[:MAX_PICKS_PER_RECAP]
    taken = {r["id"] for r in chosen}
    for row in eligible:
        if len(chosen) >= MAX_PICKS_PER_RECAP:
            break
        if row["id"] not in taken:
            chosen.append(row)
            taken.add(row["id"])
    return [r["id"] for r in chosen]


def spread_across_buckets(files: list, limit: int) -> tuple[list, list]:
    """``(to open, over the limit)``. Takes turns across photo buckets,
    sampling / table / shelf buckets first, oldest photo first in each."""
    if limit <= 0:
        return [], list(files)
    order = {"sampling": 0, "table_setup": 1, "shelf": 2, "other": 3, "receipt": 4}
    groups: dict[tuple[int, str], list] = {}
    for f in files:
        name = getattr(getattr(f, "file_recap_category", None), "name", None) or ""
        key = (order.get(category_kind(name), 3), name.lower())
        groups.setdefault(key, []).append(f)
    queues = [groups[key] for key in sorted(groups)]
    keep: list = []
    while len(keep) < limit and any(queues):
        for queue in queues:
            if queue and len(keep) < limit:
                keep.append(queue.pop(0))
    rest = [f for queue in queues for f in queue]
    return keep, rest


# ─── Reads ──────────────────────────────────────────────────────────


def _image_q(prefix: str = "") -> Q:
    q = Q()
    for suffix in _IMAGE_SUFFIXES:
        q |= Q(**{f"{prefix}url__iendswith": suffix})
    return q


def _stored_path(recap_file) -> str:
    name = getattr(getattr(recap_file, "url", None), "name", None)
    return (name if isinstance(name, str) else str(recap_file.url or "")).strip()


def _related(obj, attr: str):
    """A reverse one-to-one row, or None when there is none."""
    try:
        return getattr(obj, attr)
    except Exception:  # noqa: BLE001 — RelatedObjectDoesNotExist
        return None


def _is_paperwork(recap_file) -> bool:
    from recaps.types import _PAPERWORK_CATEGORY_RE

    name = getattr(getattr(recap_file, "file_recap_category", None), "name", None)
    return bool(name and _PAPERWORK_CATEGORY_RE.search(name))


def is_receipt(recap_file) -> bool:
    """A receipt by its bucket, by what the receipt reader found, or by what
    the scorer saw. A receipt is never a best photo, pinned or not."""
    if _is_paperwork(recap_file):
        return True
    reading = _related(recap_file, "receipt_reading")
    if reading is not None and reading.status == "read":
        return True
    score = _related(recap_file, "photo_score")
    return bool(score is not None and score.kind == "receipt")


def effective_pick_q(prefix: str = "") -> Q:
    """Photos that count as a best photo: pinned by staff, or chosen by the
    scorer and not unpinned by staff. Never a receipt bucket.

    ``prefix`` is the ORM path to ``CustomRecapFile`` (``""`` on the file).
    """
    from recaps.types import _PAPERWORK_CATEGORY_RE

    staff = f"{prefix}staff_pick__choice"
    pinned = Q(**{staff: "pinned"})
    by_model = Q(**{f"{prefix}photo_score__model_pick": True}) & (
        Q(**{f"{staff}__isnull": True}) | Q(**{staff: ""})
    )
    not_paperwork = ~Q(
        **{
            f"{prefix}file_recap_category__name__iregex": _PAPERWORK_CATEGORY_RE.pattern
        }
    ) | Q(**{f"{prefix}file_recap_category__isnull": True})
    not_a_receipt = ~Q(**{f"{prefix}photo_score__kind": "receipt"}) | Q(
        **{f"{prefix}photo_score__isnull": True}
    )
    return (pinned | by_model) & not_paperwork & not_a_receipt


def is_effective_pick(recap_file) -> bool:
    """Python twin of :func:`effective_pick_q` for one loaded file."""
    if is_receipt(recap_file):
        return False
    staff = _related(recap_file, "staff_pick")
    choice = staff.choice if staff is not None else ""
    if choice == "pinned":
        return True
    if choice == "unpinned":
        return False
    score = _related(recap_file, "photo_score")
    return bool(score is not None and score.model_pick)


def recap_photo_picks(recap_id: int) -> list[dict]:
    """Every image on one custom recap with what the scorer and staff said.
    For the staff recap view; the caller has checked who is asking."""
    from recaps.heic_conversion import display_blob_name, is_heic_blob
    from recaps.models import CustomRecapFile
    from utils.gcs import extract_blob_name_from_url, public_url

    files = (
        CustomRecapFile.objects.filter(custom_recap_id=recap_id)
        .filter(_image_q())
        .select_related("file_recap_category", "photo_score", "staff_pick", "receipt_reading")
        .order_by("id")
    )
    items: list[dict] = []
    for f in files:
        blob = extract_blob_name_from_url(_stored_path(f)) or ""
        if not blob:
            continue
        shown = display_blob_name(blob) if is_heic_blob(blob) else blob
        score = _related(f, "photo_score")
        staff = _related(f, "staff_pick")
        receipt = is_receipt(f)
        status = score.status if score is not None else STATUS_NOT_SCORED
        reason = score.skip_reason if score is not None else ""
        if receipt and not reason:
            reason = SKIP_RECEIPT
        items.append(
            {
                "file_id": str(f.id),
                "url": public_url(shown) or "",
                "category": getattr(f.file_recap_category, "name", None) or "",
                "status": status,
                "note": SKIP_LABELS.get(reason, "") if (reason and status != STATUS_SCORED) else "",
                "score": score.score if score is not None and status == STATUS_SCORED else None,
                "caption": (score.caption if score is not None else "") or "",
                "model_pick": bool(score is not None and score.model_pick),
                "staff_choice": (staff.choice if staff is not None else "") or "",
                "is_pick": is_effective_pick(f),
                "can_pick": not receipt,
            }
        )
    return items


def set_staff_pick(file_id: int, choice: str, user) -> dict:
    """Record a staff decision on one photo. ``choice`` is ``pinned``,
    ``unpinned`` or ``""`` (hand it back to the scorer).

    Returns ``{"ok", "reason"}``. A receipt cannot be pinned.
    """
    from recaps.models import CustomRecapFile, RecapPhotoStaffPick

    if choice not in ("pinned", "unpinned", ""):
        return {"ok": False, "reason": "Unknown choice."}
    recap_file = (
        CustomRecapFile.objects.filter(id=file_id)
        .filter(_image_q())
        .select_related("file_recap_category", "photo_score", "receipt_reading")
        .first()
    )
    if recap_file is None or recap_file.custom_recap_id is None:
        return {"ok": False, "reason": "Photo not found."}
    if choice == "pinned" and is_receipt(recap_file):
        return {"ok": False, "reason": "A receipt can't be a best photo."}
    RecapPhotoStaffPick.objects.update_or_create(
        custom_recap_file=recap_file,
        defaults={"choice": choice, "decided_by": user},
    )
    return {"ok": True, "reason": ""}


# ─── The job ────────────────────────────────────────────────────────


@dataclass
class RunBudget:
    """What one run may spend: model calls and wall-clock seconds."""

    photos: int = DEFAULT_RUN_PHOTOS
    seconds: float = DEFAULT_RUN_SECONDS
    started: float = field(default_factory=lambda: time.monotonic())
    used: int = 0

    def spent(self) -> bool:
        if self.used >= self.photos:
            return True
        return bool(self.seconds and time.monotonic() - self.started > self.seconds)


class _StopRun(Exception):
    """The run cannot go on (no API key, or the model keeps failing)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def pending_recaps(*, tenant_id: int | None = None, since_days: int = DEFAULT_SINCE_DAYS):
    """Approved, live custom recaps with at least one image that has no
    score row yet, newest first."""
    from recaps.models import CustomRecap, CustomRecapFile

    unscored = CustomRecapFile.objects.filter(
        custom_recap_id=OuterRef("pk"), photo_score__isnull=True
    ).filter(_image_q())
    qs = (
        CustomRecap.objects.filter(approved=True, archived_at__isnull=True)
        .filter(_live_request_q("event__"))
        .filter(Exists(unscored))
    )
    if tenant_id:
        qs = qs.filter(tenant_id=tenant_id)
    if since_days:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=since_days))
    return qs.select_related("ambassador__user").order_by("-id")


def _save(recap_file, **values) -> None:
    from recaps.models import RecapPhotoScore

    RecapPhotoScore.objects.update_or_create(custom_recap_file=recap_file, defaults=values)


def _skip(recap_file, reason: str, counts: dict, **extra) -> None:
    _save(
        recap_file,
        status=STATUS_SKIPPED,
        skip_reason=reason,
        score=0 if reason in (SKIP_RECEIPT, SKIP_SCREENSHOT) else None,
        kind="receipt" if reason == SKIP_RECEIPT else "",
        **extra,
    )
    counts["skipped"] += 1
    counts["skipped_by_reason"][reason] = counts["skipped_by_reason"].get(reason, 0) + 1


def recompute_picks(recap_id: int) -> list[int]:
    """Set ``model_pick`` on the recap's best scored photos. Staff rows are
    not touched. Returns the chosen ``CustomRecapFile`` ids."""
    from recaps.models import RecapPhotoScore

    scored = RecapPhotoScore.objects.filter(
        custom_recap_file__custom_recap_id=recap_id, status=STATUS_SCORED
    )
    rows = [
        {"id": file_id, "score": score, "kind": kind}
        for file_id, score, kind in scored.values_list("custom_recap_file_id", "score", "kind")
    ]
    chosen = choose_picks(rows)
    everything = RecapPhotoScore.objects.filter(custom_recap_file__custom_recap_id=recap_id)
    everything.exclude(custom_recap_file_id__in=chosen).filter(model_pick=True).update(
        model_pick=False
    )
    everything.filter(custom_recap_file_id__in=chosen, model_pick=False).update(model_pick=True)
    return chosen


def _open(recap_file):
    """``(bytes, image)`` for a stored photo. Bytes are None when the file is
    gone; the image is None when the bytes are not a picture."""
    from PIL import Image, ImageOps

    from recaps.heic_conversion import display_blob_name, is_heic_blob
    from utils.gcs import download_blob_bytes, extract_blob_name_from_url

    blob = extract_blob_name_from_url(_stored_path(recap_file)) or ""
    if not blob:
        return None, None
    source = display_blob_name(blob) if is_heic_blob(blob) else blob
    data = download_blob_bytes(source)
    if not data:
        return None, None
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:  # noqa: BLE001 — JPEG / PNG still open without it
        pass
    try:
        image = Image.open(BytesIO(data))
        image.load()
        image = ImageOps.exif_transpose(image)
    except Exception:  # noqa: BLE001
        return data, None
    return data, image


def score_recap(recap, budget: RunBudget, counts: dict, state: dict) -> bool:
    """Score one recap's unscored photos. True when every photo now has a
    row; False when the budget ran out part-way (the rest wait for the next
    run). Raises :class:`_StopRun` when the model cannot be used at all."""
    from recaps.models import CustomRecapFile
    from recaps.pdf import downscale_image_bytes
    from recaps.photo_reuse import normalize_content_sha256, photo_reuse_checks

    files = list(
        CustomRecapFile.objects.filter(custom_recap_id=recap.id)
        .filter(_image_q())
        .select_related("file_recap_category", "photo_score", "receipt_reading", "library_tag")
        .order_by("id")
    )
    done = [f for f in files if _related(f, "photo_score") is not None]
    todo = [f for f in files if _related(f, "photo_score") is None]
    if not todo:
        return True

    seen_hashes = {
        normalize_content_sha256(f.content_sha256) for f in done
    } - {""}
    seen_paths = {_stored_path(f) for f in done} - {""}
    kept_dhashes = [f.photo_score.dhash for f in done if f.photo_score.dhash]
    opened = sum(
        1
        for f in done
        if f.photo_score.status in (STATUS_SCORED, STATUS_UNREADABLE)
        or f.photo_score.skip_reason in _OPENED_SKIPS
    )

    # 1. Set aside without opening anything.
    try:
        reuse = {
            item["fileId"]: item["status"]
            for item in photo_reuse_checks(recap.id, is_custom=True)["items"]
        }
    except Exception:  # noqa: BLE001 — the reuse check is a bonus
        reuse = {}
    candidates: list = []
    for f in todo:
        digest = normalize_content_sha256(f.content_sha256)
        path = _stored_path(f)
        if is_receipt(f):
            _skip(f, SKIP_RECEIPT, counts)
        elif (digest and digest in seen_hashes) or (path and path in seen_paths):
            _skip(f, SKIP_DUPLICATE, counts)
        elif reuse.get(str(f.id)) == "reused":
            _skip(f, SKIP_REUSED, counts)
        else:
            candidates.append(f)
        if digest:
            seen_hashes.add(digest)
        if path:
            seen_paths.add(path)

    # 2. Only so many photos per recap are ever opened.
    to_open, over = spread_across_buckets(candidates, MAX_PHOTOS_PER_RECAP - opened)
    for f in over:
        _skip(f, SKIP_OVER_LIMIT, counts)

    from photo_library.tagging import reusable_reply, save_from_scorer

    names = _ba_names([recap])
    catalog = None
    complete = True
    for f in to_open:
        # The photo library may already have looked at this exact file with
        # the same call: reuse its answer, no download and no model call.
        reused = reusable_reply(f)
        tag = _related(f, "library_tag") if reused is not None else None
        if tag is not None and not (tag.width and tag.height and tag.dhash):
            reused = None
        if reused is None and budget.spent():
            complete = False
            break
        if reused is not None:
            width, height, dhash = tag.width, tag.height, tag.dhash
        else:
            try:
                data, image = _open(f)
            except Exception as exc:  # noqa: BLE001 — storage hiccup: try next run
                logger.warning("photo picks: could not fetch file %s: %s", f.id, exc)
                counts["errors"] += 1
                complete = False
                continue
            if data is None:
                _skip(f, SKIP_MISSING_FILE, counts)
                continue
            if image is None:
                _skip(f, SKIP_NOT_AN_IMAGE, counts)
                continue
            width, height = image.size
            dhash = difference_hash(image)

        # 3. Opened, still no model call.
        size = {"width": width, "height": height}
        if min(width, height) < MIN_SIDE_PX:
            _skip(f, SKIP_LOW_RESOLUTION, counts, **size)
            continue
        if any(hash_distance(dhash, other) <= NEAR_DUPLICATE_BITS for other in kept_dhashes):
            _skip(f, SKIP_NEAR_DUPLICATE, counts, dhash=dhash, **size)
            continue

        if reused is not None:
            parsed = reused
            counts["reused_library"] = counts.get("reused_library", 0) + 1
        else:
            # 4. One vision call, on a small copy. The same call tags the
            # photo for the photo library.
            if catalog is None:
                catalog = shared_vision.catalog_labels(recap.tenant_id)
            small = downscale_image_bytes(data, max_dim=VISION_MAX_DIM, quality=VISION_QUALITY)
            budget.used += 1
            counts["model_calls"] += 1
            try:
                parsed = generate_vision_json(
                    PHOTO_SYSTEM,
                    PHOTO_USER,
                    image_bytes=small or data,
                    content_type="image/jpeg",
                    schema=shared_vision.schema_for(catalog),
                    max_tokens=500,
                    # A fixed, short judgement: the smaller, cheaper model.
                    small=True,
                )
            except AiUnavailable as exc:
                raise _StopRun("ai_unavailable") from exc
        result = interpret_score(parsed, names)
        if result is None:
            # Unusable reply. Remember it, and only record the photo as
            # unreadable once another photo has proved the model is up.
            state["failed"].append((f, dhash, size))
            state["failures_in_a_row"] += 1
            counts["errors"] += 1
            complete = False
            if state["failures_in_a_row"] >= MAX_CONSECUTIVE_FAILURES and not state["worked"]:
                raise _StopRun("model_failing")
            continue
        state["worked"] = True
        state["failures_in_a_row"] = 0
        kept_dhashes.append(dhash)
        _save(f, dhash=dhash, **size, **result)
        if reused is None:
            save_from_scorer(f, parsed, names=names, width=width, height=height, dhash=dhash)
        if result["status"] == STATUS_SCORED:
            counts["scored"] += 1
        else:
            counts["skipped"] += 1
            reason = result["skip_reason"]
            counts["skipped_by_reason"][reason] = counts["skipped_by_reason"].get(reason, 0) + 1

    counts["picks"] += len(recompute_picks(recap.id))
    return complete


def score_pending_photos(
    *,
    tenant_id: int | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    limit: int = DEFAULT_RUN_RECAPS,
    max_photos: int = DEFAULT_RUN_PHOTOS,
    max_seconds: int = DEFAULT_RUN_SECONDS,
) -> dict:
    """Score photos on up to ``limit`` approved recaps, within a budget of
    ``max_photos`` model calls and ``max_seconds``. Safe to re-run: a photo
    with a row is never looked at again."""
    budget = RunBudget(
        photos=max(0, min(int(max_photos), MAX_RUN_PHOTOS)), seconds=max(0, int(max_seconds))
    )
    counts = {
        "recaps": 0,
        "scored": 0,
        "skipped": 0,
        "skipped_by_reason": {},
        "unreadable": 0,
        "errors": 0,
        "model_calls": 0,
        "picks": 0,
    }
    state = {"failed": [], "failures_in_a_row": 0, "worked": False}
    stopped = ""
    recaps = list(pending_recaps(tenant_id=tenant_id, since_days=since_days)[: max(1, int(limit))])
    for recap in recaps:
        if budget.spent():
            stopped = "budget"
            break
        try:
            score_recap(recap, budget, counts, state)
        except _StopRun as stop:
            stopped = stop.reason
            break
        except Exception as exc:  # noqa: BLE001 — one bad recap must not end the run
            logger.exception("photo picks: recap %s failed: %s", recap.id, exc)
            counts["errors"] += 1
            continue
        counts["recaps"] += 1

    # A reply that was unusable while other photos scored fine is that
    # photo's problem, not an outage: record it so it is not retried.
    if state["worked"]:
        touched: set[int] = set()
        for recap_file, dhash, size in state["failed"]:
            _save(recap_file, status=STATUS_UNREADABLE, dhash=dhash, **size)
            counts["unreadable"] += 1
            touched.add(recap_file.custom_recap_id)
        for recap_id in touched:
            recompute_picks(recap_id)

    return {
        **counts,
        "ai_unavailable": stopped == "ai_unavailable",
        "stopped": stopped,
        "budget_photos": budget.photos,
        "budget_seconds": budget.seconds,
    }
