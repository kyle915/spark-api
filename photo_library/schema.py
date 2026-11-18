"""GraphQL for the photo library: ``photoLibrary`` (search) and
``interpretPhotoSearch`` (typed words → filters).

Tenant scoping matches ``tenantPhotoGallery``: a brand user is pinned to
their own brand and to approved recaps; staff may choose the brand and may
widen to recaps not yet approved. Neither query raises; both degrade to an
empty result.
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

logger = logging.getLogger(__name__)


@strawberry.input
class PhotoLibraryFilterInput:
    text: str | None = None
    shows: list[str] | None = None
    crowd_sizes: list[str] | None = None
    settings: list[str] | None = None
    product_ids: list[str] | None = None
    store: str | None = None
    states: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None
    min_quality: int | None = None
    people_present: bool | None = None
    hide_issues: bool = False
    # Staff only; a brand user is always held to approved recaps.
    approved_only: bool = True
    picks_only: bool = False
    # relevance | quality | newest
    sort: str | None = None


@strawberry.type
class PhotoLibraryOption:
    key: str
    label: str


@strawberry.type
class PhotoLibraryFilters:
    text: str
    shows: list[str]
    crowd_sizes: list[str]
    settings: list[str]
    product_ids: list[str]
    store: str
    states: list[str]
    start_date: str | None
    end_date: str | None
    min_quality: int | None
    people_present: bool | None
    hide_issues: bool
    sort: str


@strawberry.type
class LibraryPhoto:
    id: str
    url: str
    caption: str
    category: str
    shows: list[str]
    show_labels: list[str]
    crowd_size: str
    crowd_label: str
    setting: str
    setting_label: str
    people_present: bool | None
    quality_score: int | None
    issues: list[str]
    products: list[str]
    store: str
    city: str
    state: str
    event_date: str
    recap_uuid: str
    is_pick: bool
    approved: bool
    tagged: bool


@strawberry.type
class PhotoLibraryPage:
    total: int
    in_scope: int
    tagged: int
    photos: list[LibraryPhoto]
    states: list[str]
    products: list[PhotoLibraryOption]
    show_options: list[PhotoLibraryOption]
    crowd_options: list[PhotoLibraryOption]
    setting_options: list[PhotoLibraryOption]
    approved_only: bool


@strawberry.type
class PhotoSearchInterpretation:
    ok: bool
    reason: str
    filters: PhotoLibraryFilters
    chips: list[str]


def _empty_page() -> PhotoLibraryPage:
    return PhotoLibraryPage(
        total=0,
        in_scope=0,
        tagged=0,
        photos=[],
        states=[],
        products=[],
        show_options=[],
        crowd_options=[],
        setting_options=[],
        approved_only=True,
    )


def _filters_type(filters) -> PhotoLibraryFilters:
    return PhotoLibraryFilters(
        text=filters.text,
        shows=filters.shows,
        crowd_sizes=filters.crowd_sizes,
        settings=filters.settings,
        product_ids=[str(p) for p in filters.product_ids],
        store=filters.store,
        states=filters.states,
        start_date=filters.start.isoformat() if filters.start else None,
        end_date=filters.end.isoformat() if filters.end else None,
        min_quality=filters.min_quality,
        people_present=filters.people_present,
        hide_issues=filters.hide_issues,
        sort=filters.sort,
    )


async def _scope(info: strawberry.Info, tenant_id) -> tuple[int | None, bool]:
    """``(tenant id, is staff)``; tenant None when out of scope."""
    from recaps.report_types import _CampaignReportService

    service = _CampaignReportService()
    try:
        target = await service.resolve_target_tenant_id(info, tenant_id)
        user = await service.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    except Exception:  # noqa: BLE001
        return None, False
    return target, _is_admin_access(role_slug, is_staff, is_super, email)


def _raw(filters: PhotoLibraryFilterInput | None) -> dict:
    if filters is None:
        return {}
    return {
        "text": filters.text,
        "shows": filters.shows,
        "crowd_sizes": filters.crowd_sizes,
        "settings": filters.settings,
        "product_ids": filters.product_ids,
        "store": filters.store,
        "states": filters.states,
        "start_date": filters.start_date,
        "end_date": filters.end_date,
        "min_quality": filters.min_quality,
        "people_present": filters.people_present,
        "hide_issues": filters.hide_issues,
        "approved_only": filters.approved_only,
        "picks_only": filters.picks_only,
        "sort": filters.sort,
    }


@strawberry.type
class PhotoLibraryQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def photo_library(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        filters: PhotoLibraryFilterInput | None = None,
        first: int = 60,
        offset: int = 0,
    ) -> PhotoLibraryPage:
        """Search a brand's recap photos. Structured filters plus free text
        over each photo's caption, tags, products, store, city and state.
        Never a receipt; brand users see their own approved recaps only.
        Sorted by relevance (with text) or quality. At most 120 a page."""
        target, is_staff = await _scope(info, tenant_id)
        if target is None:
            return _empty_page()

        def _build():
            from photo_library.search import clean_filters, option_lists, search_photos

            cleaned = clean_filters(_raw(filters), is_staff=is_staff)
            data = search_photos(target, cleaned, is_staff=is_staff, first=first, offset=offset)
            return data, option_lists()

        try:
            data, options = await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("photoLibrary failed tenant=%s: %s", target, exc)
            return _empty_page()
        return PhotoLibraryPage(
            total=data["total"],
            in_scope=data["in_scope"],
            tagged=data["tagged"],
            photos=[LibraryPhoto(**p) for p in data["photos"]],
            states=data["states"],
            products=[PhotoLibraryOption(**o) for o in data["products"]],
            show_options=[PhotoLibraryOption(**o) for o in options["shows"]],
            crowd_options=[PhotoLibraryOption(**o) for o in options["crowd_sizes"]],
            setting_options=[PhotoLibraryOption(**o) for o in options["settings"]],
            approved_only=data["approved_only"],
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def interpret_photo_search(
        self, info: strawberry.Info, tenant_id: strawberry.ID, text: str
    ) -> PhotoSearchInterpretation:
        """Typed words → photo library filters, for the search box. The
        model fills a fixed form that is checked against the same lists the
        search accepts; it never writes a query. Without the model the words
        are searched as typed (``ok`` false, with a reason)."""
        from photo_library.search import Filters

        empty = PhotoSearchInterpretation(
            ok=False, reason="Search is not available.", filters=_filters_type(Filters()), chips=[]
        )
        target, is_staff = await _scope(info, tenant_id)
        if target is None:
            return empty

        def _build():
            from photo_library.interpret import filter_chips, interpret
            from photo_library.search import clean_filters
            from photo_library.vision import catalog_labels

            result = interpret(text, target)
            cleaned = clean_filters(result["raw"], is_staff=is_staff)
            names = dict(catalog_labels(target))
            cleaned.product_ids = [p for p in cleaned.product_ids if p in names]
            return PhotoSearchInterpretation(
                ok=result["ok"],
                reason=result["reason"],
                filters=_filters_type(cleaned),
                chips=filter_chips(cleaned, names),
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("interpretPhotoSearch failed tenant=%s: %s", target, exc)
            return empty
