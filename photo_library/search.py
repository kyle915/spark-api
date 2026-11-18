"""Search a brand's recap photos by structured filters and free text.

Who sees what is decided here, at read time, from the recap:

* Every caller: one tenant only, never a receipt or other paperwork (by
  bucket, by the receipt reader, by the scorer or by the tags), never an
  archived recap, never a recap under a deleted request, never a photo the
  tagger could not open.
* A brand user: only approved recaps of their own brand (the same bar as
  the Photos page and the client dashboards). The tenant is pinned by
  :meth:`_CampaignReportService.resolve_target_tenant_id` before this runs.
* Staff: approved recaps by default; ``approved_only=False`` widens to
  every live recap of the brand.

Tag filters (what is shown, crowd, setting, product, quality) only match
photos that have been tagged. Free text is Postgres full-text search over
each photo's caption, tag words, products, store, city and state; it never
reads a file name or an ambassador's name. The result is a list of photo
dicts; nothing here builds SQL from user text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, Q

from photo_library.vocabulary import (
    CROWD_LABELS,
    CROWD_SIZES,
    ISSUE_LABELS,
    SEARCHABLE_SHOWS,
    SETTING_LABELS,
    SETTINGS,
    SHOW_LABELS,
    SORTS,
    US_STATES,
    labels_for,
)

MAX_PAGE = 120
MAX_OFFSET = 10_000
MAX_TEXT = 120
MAX_STORE = 80
# Issues a "hide poor photos" filter leaves out.
POOR_ISSUES = ["blurry", "dark", "overexposed"]


@dataclass
class Filters:
    text: str = ""
    shows: list[str] = field(default_factory=list)
    crowd_sizes: list[str] = field(default_factory=list)
    settings: list[str] = field(default_factory=list)
    product_ids: list[int] = field(default_factory=list)
    store: str = ""
    states: list[str] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    min_quality: int | None = None
    people_present: bool | None = None
    hide_issues: bool = False
    approved_only: bool = True
    picks_only: bool = False
    sort: str = ""

    def has_tag_filters(self) -> bool:
        return bool(
            self.text
            or self.shows
            or self.crowd_sizes
            or self.settings
            or self.product_ids
            or self.min_quality is not None
            or self.people_present is not None
            or self.hide_issues
        )


def _iso(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip()[:10]) if raw else None
    except ValueError:
        return None


def _keys(raw, allowed) -> list[str]:
    out: list[str] = []
    for value in raw or []:
        key = str(value or "").strip()
        if key in allowed and key not in out:
            out.append(key)
    return out


def clean_filters(raw: dict, *, is_staff: bool) -> Filters:
    """Whatever the caller sent → :class:`Filters` holding only known keys.

    A brand user is always held to approved recaps, whatever they send.
    """
    product_ids: list[int] = []
    for value in raw.get("product_ids") or []:
        try:
            pid = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if pid not in product_ids:
            product_ids.append(pid)
    states = [
        s for s in (str(v or "").strip().upper() for v in raw.get("states") or []) if s in US_STATES
    ]
    min_quality = raw.get("min_quality")
    try:
        min_quality = None if min_quality is None else max(0, min(100, int(min_quality)))
    except (TypeError, ValueError):
        min_quality = None
    start, end = _iso(raw.get("start_date")), _iso(raw.get("end_date"))
    if start and end and start > end:
        start, end = end, start
    sort = str(raw.get("sort") or "").strip()
    return Filters(
        text=" ".join(str(raw.get("text") or "").split())[:MAX_TEXT],
        shows=_keys(raw.get("shows"), SEARCHABLE_SHOWS),
        crowd_sizes=_keys(raw.get("crowd_sizes"), CROWD_SIZES),
        settings=_keys(raw.get("settings"), SETTINGS),
        product_ids=product_ids[:20],
        store=" ".join(str(raw.get("store") or "").split())[:MAX_STORE],
        states=list(dict.fromkeys(states)),
        start=start,
        end=end,
        min_quality=min_quality,
        people_present=raw.get("people_present") if isinstance(raw.get("people_present"), bool) else None,
        hide_issues=bool(raw.get("hide_issues")),
        approved_only=True if not is_staff else raw.get("approved_only") is not False,
        picks_only=bool(raw.get("picks_only")),
        sort=sort if sort in SORTS else "",
    )


def visible_files(tenant_id: int, filters: Filters):
    """Every image in the library this caller may see, before narrowing."""
    from recaps.models import CustomRecapFile
    from recaps.photo_picks import _image_q
    from recaps.tenant_overview import (
        _approved_only,
        _filter_event_window,
        _inclusive_dates_to_window,
    )
    from recaps.types import _PAPERWORK_CATEGORY_RE

    qs = CustomRecapFile.objects.filter(
        custom_recap__tenant_id=tenant_id,
        custom_recap__archived_at__isnull=True,
    )
    if filters.approved_only:
        qs = _approved_only(qs, "custom_recap__")
    qs = _filter_event_window(
        qs, "custom_recap__event__", _inclusive_dates_to_window(filters.start, filters.end)
    )
    qs = qs.filter(_image_q())
    # Never a receipt, a screenshot or a photo that could not be opened.
    qs = (
        qs.exclude(file_recap_category__name__iregex=_PAPERWORK_CATEGORY_RE.pattern)
        .exclude(receipt_reading__status="read")
        .exclude(photo_score__kind__in=["receipt", "screenshot"])
        .exclude(library_tag__status__in=["excluded", "unreadable"])
        .exclude(library_tag__shows__contains=["receipt"])
    )
    # The tag row must belong to this tenant too (it is copied on tagging).
    qs = qs.filter(Q(library_tag__isnull=True) | Q(library_tag__tenant_id=tenant_id))
    return qs


def _store_q(needle: str) -> Q:
    return (
        Q(custom_recap__retailer__name__icontains=needle)
        | Q(custom_recap__typed_store_name__icontains=needle)
        | Q(custom_recap__event__retailer__name__icontains=needle)
        | Q(custom_recap__event__request__retailer__name__icontains=needle)
        | Q(custom_recap__event__name__icontains=needle)
        | Q(custom_recap__event__address__icontains=needle)
        | Q(custom_recap__location__name__icontains=needle)
    )


def narrow(qs, filters: Filters):
    """Apply the filters. Returns ``(queryset, search query or None)``."""
    from recaps.photo_picks import effective_pick_q

    if filters.picks_only:
        qs = qs.filter(effective_pick_q())
    if filters.store:
        qs = qs.filter(_store_q(filters.store))
    if filters.states:
        qs = qs.filter(
            Q(custom_recap__state__code__in=filters.states)
            | Q(custom_recap__state__isnull=True, custom_recap__event__state__code__in=filters.states)
        )
    if filters.shows:
        qs = qs.filter(library_tag__status="tagged", library_tag__shows__overlap=filters.shows)
    if filters.crowd_sizes:
        qs = qs.filter(library_tag__status="tagged", library_tag__crowd_size__in=filters.crowd_sizes)
    if filters.settings:
        qs = qs.filter(library_tag__status="tagged", library_tag__setting__in=filters.settings)
    if filters.product_ids:
        qs = qs.filter(
            library_tag__status="tagged", library_tag__product_ids__overlap=filters.product_ids
        )
    if filters.people_present is not None:
        qs = qs.filter(library_tag__status="tagged", library_tag__people_present=filters.people_present)
    if filters.min_quality is not None:
        qs = qs.filter(library_tag__quality_score__gte=filters.min_quality)
    if filters.hide_issues:
        qs = qs.filter(library_tag__status="tagged").exclude(
            library_tag__issues__overlap=POOR_ISSUES
        )
    query = None
    if filters.text:
        query = SearchQuery(filters.text, search_type="websearch", config="english")
        qs = qs.filter(library_tag__search_vector=query)
    return qs, query


def _ordered(qs, filters: Filters, query):
    sort = filters.sort or ("relevance" if query is not None else "quality")
    quality = F("library_tag__quality_score").desc(nulls_last=True)
    scorer = F("photo_score__score").desc(nulls_last=True)
    newest = F("custom_recap__event__date").desc(nulls_last=True)
    if sort == "relevance" and query is not None:
        qs = qs.annotate(_rank=SearchRank(F("library_tag__search_vector"), query))
        return qs.order_by(F("_rank").desc(), quality, newest, "-id")
    if sort == "newest":
        return qs.order_by(newest, "-custom_recap_id", "id")
    return qs.order_by(quality, scorer, newest, "-id")


def _photo(recap_file, *, is_staff: bool) -> dict | None:
    from recaps.client_recap_draft import _store_name
    from recaps.photo_gallery import _image_url
    from recaps.photo_picks import _related, is_effective_pick

    url = _image_url(recap_file)
    if not url:
        return None
    recap = recap_file.custom_recap
    event = getattr(recap, "event", None)
    tag = _related(recap_file, "library_tag")
    tagged = tag is not None and tag.status == "tagged"
    score = _related(recap_file, "photo_score")
    when = getattr(event, "date", None) or getattr(event, "start_time", None)
    state = getattr(recap, "state", None) or getattr(event, "state", None)
    store = (
        getattr(getattr(recap, "retailer", None), "name", None)
        or (_store_name(event) if event is not None else "")
        or recap.typed_store_name
        or ""
    )
    if store and recap.store_number and recap.store_number not in store:
        store = f"{store} #{recap.store_number}"
    caption = (tag.caption if tagged else "") or getattr(score, "caption", "") or ""
    return {
        "id": str(recap_file.id),
        "url": url,
        "caption": caption,
        "category": getattr(recap_file.file_recap_category, "name", None) or "",
        "shows": list(tag.shows) if tagged else [],
        "show_labels": labels_for(tag.shows, SHOW_LABELS) if tagged else [],
        "crowd_size": tag.crowd_size if tagged else "",
        "crowd_label": CROWD_LABELS.get(tag.crowd_size, "") if tagged else "",
        "setting": tag.setting if tagged else "",
        "setting_label": SETTING_LABELS.get(tag.setting, "") if tagged else "",
        "people_present": tag.people_present if tagged else None,
        "quality_score": tag.quality_score if tagged else getattr(score, "score", None),
        "issues": labels_for(tag.issues, ISSUE_LABELS) if tagged else [],
        "products": list(tag.product_names) if tagged else [],
        "store": store,
        "city": getattr(getattr(recap, "location", None), "name", None) or "",
        "state": (getattr(state, "code", "") or "").upper(),
        "event_date": when.date().isoformat() if when else "",
        "recap_uuid": str(recap.uuid),
        "is_pick": is_effective_pick(recap_file),
        "approved": bool(recap.approved) if is_staff else True,
        "tagged": tagged,
    }


def search_photos(
    tenant_id: int,
    filters: Filters,
    *,
    is_staff: bool,
    first: int = 60,
    offset: int = 0,
) -> dict:
    """One page of photos plus the choices that exist for this brand."""
    from photo_library.models import RecapPhotoTag
    from photo_library.vision import catalog_labels

    base = visible_files(tenant_id, filters)
    states = sorted(
        {
            (c or "").strip().upper()
            for c in base.values_list("custom_recap__event__state__code", flat=True).distinct()
            if (c or "").strip()
        }
        | {
            (c or "").strip().upper()
            for c in base.values_list("custom_recap__state__code", flat=True).distinct()
            if (c or "").strip()
        }
    )
    in_scope = base.count()
    tagged = base.filter(library_tag__status=RecapPhotoTag.STATUS_TAGGED).count()

    qs, query = narrow(base, filters)
    total = qs.count()
    first = max(1, min(int(first or 60), MAX_PAGE))
    offset = max(0, min(int(offset or 0), MAX_OFFSET))
    page = _ordered(
        qs.select_related(
            "file_recap_category",
            "custom_recap",
            "custom_recap__retailer",
            "custom_recap__location",
            "custom_recap__state",
            "custom_recap__event",
            "custom_recap__event__retailer",
            "custom_recap__event__request__retailer",
            "custom_recap__event__state",
            "library_tag",
            "photo_score",
            "staff_pick",
            "receipt_reading",
        ),
        filters,
        query,
    )[offset : offset + first]
    photos = [p for p in (_photo(f, is_staff=is_staff) for f in page) if p]
    return {
        "total": total,
        "in_scope": in_scope,
        "tagged": tagged,
        "photos": photos,
        "states": states,
        "products": [{"key": str(pid), "label": label} for pid, label in catalog_labels(tenant_id)],
        "approved_only": filters.approved_only,
    }


def option_lists() -> dict:
    return {
        "shows": [{"key": k, "label": SHOW_LABELS[k]} for k in SEARCHABLE_SHOWS],
        "crowd_sizes": [{"key": k, "label": CROWD_LABELS[k]} for k in CROWD_SIZES],
        "settings": [{"key": k, "label": SETTING_LABELS[k]} for k in SETTINGS],
    }
