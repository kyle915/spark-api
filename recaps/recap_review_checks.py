"""Plain rule-based review checks for one recap (roadmap #3).

These are ADVISORY checks shown beside Approve so ops can eyeball a recap
before approving it. They are not a score, they never auto-approve or
auto-reject, and they never mutate the recap. Every check is a fixed,
explainable rule over facts already stored on the recap:

* ``engagements_rate`` — engagements / samples per shift hour. Flagged only
  past 300/hour (five people a minute, sustained for the whole shift),
  a rate no sampling shift can physically sustain. Anything at or below
  that line passes; this flags typos (an extra zero), not busy days.
* ``gps_distance`` — haversine distance between the clock-in GPS fix and
  the venue coordinates, flagged past 1.0 mile. Hidden (``na``) when
  either coordinate pair is missing — no fix is not a flag.
* ``photo_buckets`` — photo buckets configured for this tenant/program
  (``Tenant.checkin_photo_buckets`` via ``photo_bucket_specs``) with no
  (or too few) photos filed under that bucket's category, named in the
  label. A bucket with an explicit ``min: 0`` is optional and skipped;
  a bucket with no ``min`` is expected to hold at least one photo.
* ``duplicate_photos`` — the same stored image file (identical blob /
  URL) filed more than once in this recap.
* ``spend_without_receipt`` — a spend total over $0 with no photo filed
  under a Receipt / Product Spend / Expense bucket, when the tenant's
  buckets ask for one.

Each check returns ``{"code", "label", "status", "detail"}`` with
``status`` in ``"pass" | "flag" | "na"``. ``na`` means "cannot judge from
stored facts" and the UI hides it. This module never raises: a missing
recap (or any failure) returns the all-``na`` list.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# Five engagements or samples a minute, every minute of the shift, is
# beyond what a person handing out samples can do; past it the number is
# almost certainly a typo. Deliberately generous so real activations
# (festivals, stadium pours) never trip it.
MAX_PER_HOUR = 300

# A clock-in more than a mile from the venue is worth a look (wrong store,
# GPS drift in a mall aside). Distances at or under it pass quietly.
GPS_FLAG_MILES = 1.0

_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|heic|heif)(\?|$)", re.IGNORECASE)
_RECEIPT_BUCKET_RE = re.compile(r"receipt|product\s*spend|expense", re.IGNORECASE)
_MONEY_RE = re.compile(r"-?\d+(?:\.\d+)?")

CHECK_CODES = (
    "engagements_rate",
    "gps_distance",
    "photo_buckets",
    "duplicate_photos",
    "spend_without_receipt",
)


def _check(code: str, label: str, status: str, detail: str = "") -> dict:
    return {"code": code, "label": label, "status": status, "detail": detail}


def _na(code: str, label: str) -> dict:
    return _check(code, label, "na")


def _all_na() -> list[dict]:
    return [
        _na("engagements_rate", "Engagements vs shift hours"),
        _na("gps_distance", "Clock-in distance"),
        _na("photo_buckets", "Photo buckets"),
        _na("duplicate_photos", "Duplicate photos"),
        _na("spend_without_receipt", "Spend receipt"),
    ]


def _valid_coords(raw) -> tuple[float, float] | None:
    if not raw or len(raw) < 2:
        return None
    try:
        lat, lng = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lng == 0.0:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    return (lat, lng)


def _haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _fmt_hours(hours: float) -> str:
    return f"{hours:.1f}h" if hours != int(hours) else f"{int(hours)}h"


class _Facts:
    __slots__ = (
        "exists", "event", "tenant", "ambassador_id", "engagements",
        "samples", "files", "field_pairs", "account_spend",
    )

    def __init__(self):
        self.exists = False
        self.event = None
        self.tenant = None
        self.ambassador_id = None
        self.engagements: int | None = None
        self.samples: int | None = None
        # (blob_name, category_name) per filed file.
        self.files: list[tuple[str, str]] = []
        # (field_name, value, field_type_name) per custom field value.
        self.field_pairs: list[tuple[str, str, str]] = []
        self.account_spend: float | None = None


def _file_rows(qs, blob_attr: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for f in qs:
        field_file = getattr(f, blob_attr, None)
        if not field_file:
            continue
        try:
            blob = field_file.name or ""
        except Exception:
            blob = str(field_file)
        category = getattr(f, "file_recap_category", None)
        rows.append((blob, (getattr(category, "name", "") or "")))
    return rows


def _gather(recap_id: int, is_custom: bool) -> _Facts:
    from recaps.recap_quality import _gather_custom_facts, _gather_legacy_facts

    facts = _Facts()
    if is_custom:
        from recaps.models import CustomFieldValue, CustomRecap

        recap = (
            CustomRecap.objects.filter(id=recap_id)
            .select_related("event", "tenant", "event__event_type", "event__tenant")
            .prefetch_related("custom_recap_files")
            .first()
        )
        if recap is None:
            return facts
        facts.exists = True
        facts.event = recap.event
        facts.tenant = recap.tenant
        facts.ambassador_id = recap.ambassador_id
        base = _gather_custom_facts(recap)
        facts.engagements = base.total_engagements
        facts.samples = base.samples_distributed
        facts.files = _file_rows(recap.custom_recap_files.all(), "url")
        facts.field_pairs = [
            (name or "", value or "", type_name or "")
            for name, value, type_name in CustomFieldValue.objects.filter(
                custom_recap=recap
            ).values_list(
                "custom_field__name", "value", "custom_field__custom_field_type__name"
            )
        ]
        return facts

    from recaps.models import Recap

    recap = (
        Recap.objects.filter(id=recap_id)
        .select_related("event", "event__event_type", "event__tenant")
        .prefetch_related("recap_files")
        .first()
    )
    if recap is None:
        return facts
    facts.exists = True
    facts.event = recap.event
    facts.tenant = getattr(recap.event, "tenant", None)
    facts.ambassador_id = recap.ambassador_id
    base = _gather_legacy_facts(recap)
    facts.engagements = base.total_engagements
    facts.samples = base.samples_distributed
    facts.files = _file_rows(recap.recap_files.all(), "file")
    if recap.account_spend_amount is not None:
        try:
            facts.account_spend = float(recap.account_spend_amount)
        except (TypeError, ValueError):
            facts.account_spend = None
    return facts


def _attendance_rows(facts: _Facts) -> list:
    try:
        from ambassadors.models import Attendance

        qs = Attendance.objects.filter(event=facts.event).order_by("clock_time")
        if facts.ambassador_id:
            qs = qs.filter(ambassador_id=facts.ambassador_id)
        return list(qs)
    except Exception:
        return []


def _shift_hours(facts: _Facts, attendance: list) -> float | None:
    """Actual clock span first, scheduled event window as the fallback."""
    times = [r.clock_time for r in attendance if getattr(r, "clock_time", None)]
    if len(times) >= 2:
        span = (max(times) - min(times)).total_seconds() / 3600
        if span > 0:
            return span
    event = facts.event
    start = getattr(event, "start_time", None)
    end = getattr(event, "end_time", None) or getattr(event, "new_end_time", None)
    if start and end and end > start:
        return (end - start).total_seconds() / 3600
    return None


def _check_engagements_rate(facts: _Facts, attendance: list) -> dict:
    label = "Engagements vs shift hours"
    hours = _shift_hours(facts, attendance)
    values = [
        (name, v)
        for name, v in (
            ("engagements", facts.engagements),
            ("samples", facts.samples),
        )
        if v is not None and v > 0
    ]
    if hours is None or not values:
        return _na("engagements_rate", label)
    worst_name, worst_value, worst_rate = "", 0, 0.0
    for name, value in values:
        rate = value / hours
        if rate > worst_rate:
            worst_name, worst_value, worst_rate = name, value, rate
    detail = (
        f"{facts.engagements or 0} engagements · {facts.samples or 0} samples "
        f"in a {_fmt_hours(hours)} shift (check flags over {MAX_PER_HOUR}/hr)"
    )
    if worst_rate > MAX_PER_HOUR:
        return _check(
            "engagements_rate",
            f"{worst_value} {worst_name} in a {_fmt_hours(hours)} shift "
            f"(~{worst_rate:,.0f}/hr) — check for a typo",
            "flag",
            detail,
        )
    return _check("engagements_rate", "Engagements fit the shift length", "pass", detail)


def _check_gps(facts: _Facts, attendance: list) -> dict:
    label = "Clock-in distance"
    venue = _valid_coords(getattr(facts.event, "coordinates", None))
    clock = None
    for row in attendance:
        clock = _valid_coords(getattr(row, "coordinates", None))
        if clock is not None:
            break
    if venue is None or clock is None:
        return _na("gps_distance", label)
    miles = _haversine_miles(clock, venue)
    shown = f"{miles:.1f} mi" if miles >= 0.1 else "<0.1 mi"
    detail = f"Clock-in GPS is {shown} from the venue address (check flags over {GPS_FLAG_MILES:.0f} mi)"
    if miles > GPS_FLAG_MILES:
        return _check(
            "gps_distance",
            f"Clock-in {shown} from the venue — worth a look",
            "flag",
            detail,
        )
    return _check("gps_distance", f"Clock-in {shown} from venue", "pass", detail)


def _bucket_specs(facts: _Facts) -> list[dict]:
    try:
        from ambassadors.checkin_web import photo_bucket_specs

        return [
            e for e in photo_bucket_specs(facts.tenant, getattr(facts.event, "event_type", None))
            if isinstance(e, dict)
        ]
    except Exception:
        return []


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _check_photo_buckets(facts: _Facts, buckets: list[dict]) -> dict:
    label = "Photo buckets"
    required: list[tuple[str, int]] = []
    for entry in buckets:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        if "min" in entry:
            try:
                minimum = int(entry.get("min") or 0)
            except (TypeError, ValueError):
                minimum = 0
            if minimum <= 0:
                continue  # Explicitly optional bucket.
        else:
            minimum = 1
        required.append((name, minimum))
    if not required:
        return _na("photo_buckets", label)
    counts: Counter = Counter()
    for blob, category in facts.files:
        if blob and _IMAGE_EXT_RE.search(blob) and category:
            counts[_norm(category)] += 1
    missing = [
        name for name, minimum in required if counts.get(_norm(name), 0) < minimum
    ]
    if missing:
        return _check(
            "photo_buckets",
            f"Missing photo bucket{'s' if len(missing) > 1 else ''}: {', '.join(missing)}",
            "flag",
            f"{len(required) - len(missing)}/{len(required)} required buckets have photos",
        )
    return _check(
        "photo_buckets",
        "All required photo buckets have photos",
        "pass",
        f"{len(required)}/{len(required)} required buckets have photos",
    )


def _check_duplicates(facts: _Facts) -> dict:
    label = "Duplicate photos"
    blobs = [b for b, _ in facts.files if b and _IMAGE_EXT_RE.search(b)]
    if not blobs:
        return _na("duplicate_photos", label)
    dupes = [(blob, n) for blob, n in Counter(blobs).items() if n > 1]
    if dupes:
        names = ", ".join(
            f"{blob.rsplit('/', 1)[-1]} ×{n}" for blob, n in dupes[:3]
        )
        return _check(
            "duplicate_photos",
            f"Same photo file filed more than once: {names}",
            "flag",
            f"{sum(n - 1 for _, n in dupes)} duplicate filing(s) of the same stored image",
        )
    return _check("duplicate_photos", "No duplicate photos", "pass", f"{len(blobs)} photos, all distinct files")


def _spend_total(facts: _Facts) -> float | None:
    from recaps.spend_amount import is_spend_amount_field

    total = 0.0
    found = False
    for name, value, type_name in facts.field_pairs:
        if not is_spend_amount_field(name, type_name):
            continue
        match = _MONEY_RE.search(str(value or "").replace(",", ""))
        if not match:
            continue
        try:
            amount = float(match.group())
        except ValueError:
            continue
        if amount > 0:
            total += amount
            found = True
    if facts.account_spend is not None and facts.account_spend > 0:
        total += facts.account_spend
        found = True
    return total if found else None


def _check_spend(facts: _Facts, buckets: list[dict]) -> dict:
    label = "Spend receipt"
    spend = _spend_total(facts)
    if spend is None:
        return _na("spend_without_receipt", label)
    bucket_names = [str(e.get("name") or "") for e in buckets]
    asks = any(_RECEIPT_BUCKET_RE.search(n) for n in bucket_names)
    if not asks:
        # Fall back to the tenant's filed categories: a Receipt / Product
        # Spend category on the tenant means the template asks for one.
        try:
            from recaps.models import FileRecapCategory

            asks = FileRecapCategory.objects.filter(
                tenant=facts.tenant
            ).filter(name__iregex=r"receipt|product spend|expense").exists()
        except Exception:
            asks = False
    if not asks:
        return _na("spend_without_receipt", label)
    has_receipt_photo = any(
        category and _RECEIPT_BUCKET_RE.search(category) for _, category in facts.files
    )
    detail = f"${spend:,.2f} spend recorded"
    if not has_receipt_photo:
        return _check(
            "spend_without_receipt",
            f"${spend:,.2f} spend with no receipt / Product Spend photo",
            "flag",
            detail,
        )
    return _check(
        "spend_without_receipt", "Spend has a receipt photo", "pass", detail
    )


def recap_review_checks(recap_id, is_custom: bool = False) -> list[dict]:
    """The five rule-based review checks for one recap. Never raises."""
    try:
        rid = int(recap_id)
    except (TypeError, ValueError):
        return _all_na()
    try:
        facts = _gather(rid, is_custom)
        if not facts.exists:
            return _all_na()
        attendance = _attendance_rows(facts)
        buckets = _bucket_specs(facts)
        return [
            _check_engagements_rate(facts, attendance),
            _check_gps(facts, attendance),
            _check_photo_buckets(facts, buckets),
            _check_duplicates(facts),
            _check_spend(facts, buckets),
        ]
    except Exception:
        return _all_na()
