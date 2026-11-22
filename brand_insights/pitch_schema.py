"""GraphQL for Live In-Store Photo Wall + shareable Buyer Pitch Decks.

Authenticated surface on ``schema_clients``. Public buyer reads go through
``/api/public/pitch/<token>/`` (see ``pitch_views``) — never this module.
"""

from __future__ import annotations

from datetime import date

import strawberry
from asgiref.sync import sync_to_async

from pilot.schema import _access_for_info, _opt_date
from utils.graphql.permissions import StrictIsAuthenticated


@strawberry.type
class PhotoWallPhoto:
    id: str
    url: str
    category: str
    caption: str
    store: str
    retailer: str
    market: str
    state: str
    event_date: str
    skus: list[str]
    recap_uuid: str
    is_pinned: bool
    pinned_at: str | None
    pin_note: str


@strawberry.type
class PhotoWallResult:
    total: int
    pinned_count: int
    photos: list[PhotoWallPhoto]
    retailers: list[str]
    markets: list[str]
    skus: list[str]
    categories: list[str]


@strawberry.type
class PhotoWallPinResult:
    ok: bool
    error: str | None = None
    photo_id: str | None = None
    is_pinned: bool | None = None
    pinned_at: str | None = None
    pin_note: str | None = None
    pinned_count: int | None = None


@strawberry.type
class BuyerPitchDeckSummary:
    uuid: str
    token: str
    title: str
    subtitle: str
    buyer_name: str
    period_start: str
    period_end: str
    retailer_filter: str
    market_filter: str
    include_costs: bool
    has_passcode: bool
    expires_at: str | None
    revoked_at: str | None
    photo_count: int
    view_count: int
    created_at: str
    created_by_email: str | None


@strawberry.type
class BuyerPitchDeckCreateResult:
    ok: bool
    error: str | None = None
    deck: BuyerPitchDeckSummary | None = None


@strawberry.type
class BuyerPitchDeckRevokeResult:
    ok: bool
    error: str | None = None
    deck: BuyerPitchDeckSummary | None = None


def _deck_summary(deck) -> BuyerPitchDeckSummary:
    photos = deck.pinned_photo_ids or []
    return BuyerPitchDeckSummary(
        uuid=str(deck.uuid),
        token=deck.token,
        title=deck.title,
        subtitle=deck.subtitle or "",
        buyer_name=deck.buyer_name or "",
        period_start=deck.period_start.isoformat(),
        period_end=deck.period_end.isoformat(),
        retailer_filter=deck.retailer_filter or "",
        market_filter=deck.market_filter or "",
        include_costs=bool(deck.include_costs),
        has_passcode=bool(deck.passcode_hash),
        expires_at=deck.expires_at.isoformat() if deck.expires_at else None,
        revoked_at=deck.revoked_at.isoformat() if deck.revoked_at else None,
        photo_count=len(photos) if isinstance(photos, list) else 0,
        view_count=int(deck.view_count or 0),
        created_at=deck.created_at.isoformat(),
        created_by_email=(
            deck.created_by.email if getattr(deck, "created_by", None) else None
        ),
    )


def _photo_row(item: dict) -> PhotoWallPhoto:
    return PhotoWallPhoto(
        id=str(item["id"]),
        url=item["url"],
        category=item.get("category") or "",
        caption=item.get("caption") or "",
        store=item.get("store") or "",
        retailer=item.get("retailer") or "",
        market=item.get("market") or "",
        state=item.get("state") or "",
        event_date=item.get("event_date") or "",
        skus=list(item.get("skus") or []),
        recap_uuid=item.get("recap_uuid") or "",
        is_pinned=bool(item.get("is_pinned")),
        pinned_at=item.get("pinned_at"),
        pin_note=item.get("pin_note") or "",
    )


@strawberry.type
class PitchDeckQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def photo_wall(
        self,
        info: strawberry.Info,
        start_date: str | None = None,
        end_date: str | None = None,
        retailer: str | None = None,
        market: str | None = None,
        sku: str | None = None,
        category: str | None = None,
        search: str | None = None,
        pinned_only: bool = False,
        first: int = 60,
        offset: int = 0,
    ) -> PhotoWallResult:
        def _run():
            from brand_insights.photo_wall import get_photo_wall

            access = _access_for_info(info)
            wall = get_photo_wall(
                access,
                start_date=_opt_date(start_date),
                end_date=_opt_date(end_date),
                retailer=retailer,
                market=market,
                sku=sku,
                category=category,
                search=search,
                pinned_only=bool(pinned_only),
                first=first,
                offset=offset,
            )
            return PhotoWallResult(
                total=int(wall["total"]),
                pinned_count=int(wall["pinned_count"]),
                photos=[_photo_row(p) for p in wall["photos"]],
                retailers=list(wall["retailers"]),
                markets=list(wall["markets"]),
                skus=list(wall["skus"]),
                categories=list(wall["categories"]),
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def buyer_pitch_decks(
        self, info: strawberry.Info
    ) -> list[BuyerPitchDeckSummary]:
        def _run():
            from brand_insights.pitch_decks import list_buyer_pitch_decks

            access = _access_for_info(info)
            return [_deck_summary(d) for d in list_buyer_pitch_decks(access)]

        return await sync_to_async(_run, thread_sensitive=True)()


@strawberry.type
class PitchDeckMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def toggle_photo_wall_pin(
        self,
        info: strawberry.Info,
        photo_id: str,
        pinned: bool | None = None,
        note: str | None = None,
    ) -> PhotoWallPinResult:
        def _run():
            from brand_insights.photo_wall import toggle_photo_wall_pin

            try:
                access = _access_for_info(info)
                out = toggle_photo_wall_pin(
                    access, photo_id=photo_id, pinned=pinned, note=note
                )
                return PhotoWallPinResult(
                    ok=True,
                    photo_id=out["photo_id"],
                    is_pinned=out["is_pinned"],
                    pinned_at=out["pinned_at"],
                    pin_note=out["pin_note"],
                    pinned_count=out["pinned_count"],
                )
            except PermissionError as exc:
                return PhotoWallPinResult(ok=False, error=str(exc))
            except ValueError as exc:
                return PhotoWallPinResult(ok=False, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                return PhotoWallPinResult(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_buyer_pitch_deck(
        self,
        info: strawberry.Info,
        title: str,
        subtitle: str | None = None,
        buyer_name: str | None = None,
        intro_note: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        retailer_filter: str | None = None,
        market_filter: str | None = None,
        provider_uuid: str | None = None,
        include_costs: bool = False,
        passcode: str | None = None,
        expires_in_days: int | None = None,
        pinned_photo_ids: list[str] | None = None,
    ) -> BuyerPitchDeckCreateResult:
        def _run():
            from brand_insights.pitch_decks import create_buyer_pitch_deck

            try:
                access = _access_for_info(info)
                deck = create_buyer_pitch_deck(
                    access,
                    title=title,
                    subtitle=subtitle or "",
                    buyer_name=buyer_name or "",
                    intro_note=intro_note or "",
                    period_start=_opt_date(period_start),
                    period_end=_opt_date(period_end),
                    retailer_filter=retailer_filter or "",
                    market_filter=market_filter or "",
                    provider_uuid=provider_uuid,
                    include_costs=bool(include_costs),
                    passcode=passcode,
                    expires_in_days=expires_in_days,
                    pinned_photo_ids=pinned_photo_ids,
                )
                return BuyerPitchDeckCreateResult(ok=True, deck=_deck_summary(deck))
            except PermissionError as exc:
                return BuyerPitchDeckCreateResult(ok=False, error=str(exc))
            except ValueError as exc:
                return BuyerPitchDeckCreateResult(ok=False, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                return BuyerPitchDeckCreateResult(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def revoke_buyer_pitch_deck(
        self, info: strawberry.Info, deck_uuid: str
    ) -> BuyerPitchDeckRevokeResult:
        def _run():
            from brand_insights.pitch_decks import revoke_buyer_pitch_deck

            try:
                access = _access_for_info(info)
                deck = revoke_buyer_pitch_deck(access, deck_uuid=deck_uuid)
                return BuyerPitchDeckRevokeResult(ok=True, deck=_deck_summary(deck))
            except PermissionError as exc:
                return BuyerPitchDeckRevokeResult(ok=False, error=str(exc))
            except ValueError as exc:
                return BuyerPitchDeckRevokeResult(ok=False, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                return BuyerPitchDeckRevokeResult(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()


# Keep date import referenced for type checkers / accidental removal.
_ = date
