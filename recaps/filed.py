"""Shared definition of a "filed" recap.

A Recap / CustomRecap row is NOT filed just because it exists. Clock-out
inserts an empty stub so the BA can keep typing. Filed means submitted
content: a timestamp, photos, or metrics — the same bar quality /
completeness uses for a real recap, not a blank draft.
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q, QuerySet


def legacy_filed_q() -> Q:
    from recaps.models import RecapFile

    return (
        Q(submited_at__isnull=False)
        | Q(total_engagements__isnull=False)
        | Q(products_sold__isnull=False)
        | Q(total_cans_sold__isnull=False)
        | Q(total_packs_sold__isnull=False)
        | Exists(
            RecapFile.objects.filter(recap_id=OuterRef("pk"))
            .exclude(file__isnull=True)
            .exclude(file="")
        )
    )


def custom_filed_q() -> Q:
    from recaps.models import CustomFieldValue, CustomRecapFile

    return (
        Q(submitted_at__isnull=False)
        | Q(total_engagements__isnull=False)
        | Exists(
            CustomRecapFile.objects.filter(custom_recap_id=OuterRef("pk"))
            .exclude(url__isnull=True)
            .exclude(url="")
        )
        | Exists(
            CustomFieldValue.objects.filter(custom_recap_id=OuterRef("pk")).exclude(
                value=""
            )
        )
    )


def has_filed_recap(*, ambassador_id: int, event_id: int) -> bool:
    from recaps.models import CustomRecap, Recap

    return (
        CustomRecap.objects.filter(
            event_id=event_id, ambassador_id=ambassador_id
        )
        .filter(custom_filed_q())
        .exists()
        or Recap.objects.filter(event_id=event_id, ambassador_id=ambassador_id)
        .filter(legacy_filed_q())
        .exists()
    )


def events_missing_filed_recap(qs: QuerySet) -> QuerySet:
    """Keep events that have no filed legacy or custom recap.

    An empty clock-out stub does not count as filed, so those events
    stay on /recaps/missing.
    """
    from recaps.models import CustomRecap, Recap

    return qs.annotate(
        _has_filed_legacy=Exists(
            Recap.objects.filter(event_id=OuterRef("pk")).filter(legacy_filed_q())
        ),
        _has_filed_custom=Exists(
            CustomRecap.objects.filter(event_id=OuterRef("pk")).filter(
                custom_filed_q()
            )
        ),
    ).filter(_has_filed_legacy=False, _has_filed_custom=False)


def recap_instance_is_filed(obj, *, is_custom: bool = False) -> bool:
    """In-memory check for a loaded Recap / CustomRecap.

    Never hits the DB — list/detail scalar counts must stay off the
    prefetch cache. Photos are only consulted when ``recap_files`` /
    ``custom_recap_files`` were already prefetched.
    """
    if is_custom:
        if getattr(obj, "submitted_at", None):
            return True
    elif getattr(obj, "submited_at", None) or getattr(obj, "submitted_at", None):
        return True
    if getattr(obj, "total_engagements", None) is not None:
        return True
    if not is_custom:
        if getattr(obj, "products_sold", None) is not None:
            return True
        if getattr(obj, "total_cans_sold", None) is not None:
            return True
        if getattr(obj, "total_packs_sold", None) is not None:
            return True
    cache = getattr(obj, "_prefetched_objects_cache", {}) or {}
    if is_custom:
        files = cache.get("custom_recap_files")
        if files and any(getattr(f, "url", None) for f in files):
            return True
        values = cache.get("custom_field_value")
        if values and any((getattr(v, "value", "") or "").strip() for v in values):
            return True
        return False
    files = cache.get("recap_files")
    return bool(files and any(getattr(f, "file", None) for f in files))


def count_filed_from_cache(rows, *, is_custom: bool) -> int:
    """Count filed recaps off a prefetch cache without extra list queries.

    List prefetch is already filtered to filed rows and only loads
    id/event_id — ``len`` is the count. Detail prefetch loads full
    rows (including empty stubs), so those are filtered in memory.
    """
    if not rows:
        return 0
    sample = rows[0]
    deferred = sample.get_deferred_fields()
    if "submited_at" in deferred or "submitted_at" in deferred:
        return len(rows)
    return sum(1 for r in rows if recap_instance_is_filed(r, is_custom=is_custom))
