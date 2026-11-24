"""Clean vs Needs Eyes triage for the admin Needs-review queue.

Combines already-computed signals into one advisory label per recap so
ops can approve clean packs in one pass and only open anomalous visits:

* Rule review checks (:mod:`recaps.recap_review_checks`) — GPS, photo
  buckets, receipt math, duplicates, absurd engagement rates, …
* Deterministic quality flags (:func:`recaps.recap_quality.deterministic_quality_flags`)
  at high/medium severity — missing photos, blank feedback, KPI typos
* Filed photo scores (:class:`recaps.models.RecapPhotoScore`) — blur /
  dark / unreadable / screenshot (non-receipt)
* Shelf readings — out-of-stock gap or missing price tag, via
  :func:`recaps.shelf_check.effective_answers` so staff
  :class:`~recaps.models.ShelfReadingCorrection` rows always win

Prefer rules over new model calls. Never auto-approves, rejects, or
mutates a recap. Staff-only GraphQL surface; clients must not see this
label or filter.
"""

from __future__ import annotations

from recaps.recap_quality import deterministic_quality_flags
from recaps.recap_review_checks import (
    MAX_REVIEW_FLAG_BATCH,
    flagged_review_checks,
)

LABEL_CLEAN = "clean"
LABEL_NEEDS_EYES = "needs_eyes"

# Photo usefulness at or under the blur ceiling from photo_picks is worth
# a human glance (sharp=false clamps score ≤ 30).
PHOTO_NEEDS_EYES_MAX_SCORE = 30

# Quality nits (low severity) alone should not force Needs Eyes — the
# review-check layer already covers the must-look cases.
_QUALITY_SEVERITIES = frozenset({"high", "medium"})

# Photo score skip reasons that mean "ops should look" (receipts are
# expected paperwork, not a triage exception).
_PHOTO_SKIP_NEEDS_EYES = frozenset(
    {
        "screenshot",
        "low_resolution",
        "not_an_image",
        "missing_file",
    }
)


def _reason(code: str, label: str, source: str) -> dict:
    return {"code": code, "label": label, "source": source}


def _review_reasons(recap_id: int, is_custom: bool) -> list[dict]:
    out: list[dict] = []
    # Triage is served only to Ignite staff (``IsSparkAdminAccess`` on
    # recapReviewTriage), so it includes the BA's submit-time findings.
    for item in flagged_review_checks(
        recap_id, is_custom=is_custom, include_staff_only=True
    ):
        code, label = item.get("code"), item.get("label")
        if isinstance(code, str) and code and isinstance(label, str) and label:
            out.append(_reason(code, label, "review_check"))
    return out


def _quality_reasons(recap_id: int, is_custom: bool) -> list[dict]:
    out: list[dict] = []
    for item in deterministic_quality_flags(recap_id, is_custom=is_custom):
        if not isinstance(item, dict):
            continue
        if item.get("severity") not in _QUALITY_SEVERITIES:
            continue
        code, label = item.get("code"), item.get("label")
        if isinstance(code, str) and code and isinstance(label, str) and label:
            out.append(_reason(code, label, "quality"))
    return out


def _photo_score_reasons(recap_id: int, is_custom: bool) -> list[dict]:
    """Needs-eyes signals from persisted RecapPhotoScore rows (custom only)."""
    if not is_custom:
        return []
    try:
        from recaps.models import RecapPhotoScore
    except Exception:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    try:
        rows = RecapPhotoScore.objects.filter(
            custom_recap_file__custom_recap_id=recap_id
        ).values_list("status", "skip_reason", "score", "kind")
    except Exception:
        return []

    for status, skip_reason, score, kind in rows:
        reason_code: str | None = None
        reason_label: str | None = None
        if status == RecapPhotoScore.STATUS_UNREADABLE:
            reason_code = "photo_unreadable"
            reason_label = "A filed photo could not be read"
        elif skip_reason in _PHOTO_SKIP_NEEDS_EYES:
            reason_code = f"photo_{skip_reason}"
            reason_label = {
                "screenshot": "A filed photo looks like a screenshot",
                "low_resolution": "A filed photo is too small to use",
                "not_an_image": "A filed file is not a usable image",
                "missing_file": "A filed photo file is missing",
            }.get(skip_reason, "A filed photo needs a look")
        elif (
            status == RecapPhotoScore.STATUS_SCORED
            and score is not None
            and int(score) <= PHOTO_NEEDS_EYES_MAX_SCORE
            and kind not in ("receipt",)
        ):
            reason_code = "photo_low_quality"
            reason_label = "A filed photo looks blurry, dark, or off"

        if not reason_code or reason_code in seen:
            continue
        seen.add(reason_code)
        out.append(_reason(reason_code, reason_label or reason_code, "photo_score"))
    return out


def _shelf_reasons(recap_id: int, is_custom: bool) -> list[dict]:
    """Needs-eyes signals from shelf answers (custom only).

    Uses :func:`recaps.shelf_check.effective_answers` so a staff
    :class:`~recaps.models.ShelfReadingCorrection` always wins over the
    raw :class:`~recaps.models.ShelfReading` — same rule as brand shelf
    summaries and the staff review page.
    """
    if not is_custom:
        return []
    try:
        from recaps.models import CustomRecapFile, ShelfReading
        from recaps.shelf_check import YES, NO, effective_answers
    except Exception:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    try:
        files = CustomRecapFile.objects.filter(custom_recap_id=recap_id).select_related(
            "shelf_reading",
            "shelf_correction",
        )
    except Exception:
        return []

    for recap_file in files:
        reading = getattr(recap_file, "shelf_reading", None)
        correction = getattr(recap_file, "shelf_correction", None)
        # Skip unread / failed readings unless staff already corrected.
        if correction is None and (
            reading is None or reading.status != ShelfReading.STATUS_READ
        ):
            continue
        answers = effective_answers(reading, correction)
        if not answers:
            continue
        gap = answers.get("out_of_stock_gap")
        tag = answers.get("price_tag_visible")
        on_shelf = answers.get("product_on_shelf")
        if gap == YES and "shelf_oos_gap" not in seen:
            seen.add("shelf_oos_gap")
            out.append(
                _reason(
                    "shelf_oos_gap",
                    "Shelf photo shows an out-of-stock gap",
                    "shelf_reading",
                )
            )
        if tag == NO and "shelf_missing_price_tag" not in seen:
            seen.add("shelf_missing_price_tag")
            out.append(
                _reason(
                    "shelf_missing_price_tag",
                    "Shelf photo has no visible price tag",
                    "shelf_reading",
                )
            )
        if on_shelf == NO and "shelf_product_missing" not in seen:
            seen.add("shelf_product_missing")
            out.append(
                _reason(
                    "shelf_product_missing",
                    "Shelf photo does not show the product on shelf",
                    "shelf_reading",
                )
            )
    return out


def _dedupe_reasons(reasons: list[dict]) -> list[dict]:
    """Keep first occurrence per code (review checks win over quality)."""
    seen: set[str] = set()
    out: list[dict] = []
    for item in reasons:
        code = item.get("code")
        if not isinstance(code, str) or not code or code in seen:
            continue
        seen.add(code)
        out.append(item)
    return out


def recap_review_triage(recap_id, is_custom: bool = False) -> dict:
    """Advisory Clean / Needs Eyes label for one recap. Never raises.

    Returns::

        {
            "label": "clean" | "needs_eyes",
            "reasons": [{"code", "label", "source"}, ...],
        }
    """
    try:
        rid = int(recap_id)
    except (TypeError, ValueError):
        return {"label": LABEL_CLEAN, "reasons": []}

    try:
        reasons = _dedupe_reasons(
            _review_reasons(rid, is_custom)
            + _quality_reasons(rid, is_custom)
            + _photo_score_reasons(rid, is_custom)
            + _shelf_reasons(rid, is_custom)
        )
    except Exception:
        return {"label": LABEL_CLEAN, "reasons": []}

    if reasons:
        return {"label": LABEL_NEEDS_EYES, "reasons": reasons}
    return {"label": LABEL_CLEAN, "reasons": []}


def recap_review_triage_for_ids(
    recap_ids: list[int], is_custom: bool = False
) -> dict[int, dict]:
    """Map each id → triage dict. Cap at :data:`MAX_REVIEW_FLAG_BATCH`."""
    out: dict[int, dict] = {}
    for rid in recap_ids[:MAX_REVIEW_FLAG_BATCH]:
        try:
            out[int(rid)] = recap_review_triage(int(rid), is_custom=is_custom)
        except (TypeError, ValueError):
            continue
    return out
