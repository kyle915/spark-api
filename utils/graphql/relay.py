import base64
from typing import Generic, TypeVar, Any, Sequence

import strawberry
from strawberry import relay
from asgiref.sync import sync_to_async
from django.db.models import QuerySet

NodeType = TypeVar("NodeType")

CURSOR_PREFIX = "cursor"


def ensure_relay_mutation() -> None:
    """Add relay.mutation helper when missing in older Strawberry versions."""

    if hasattr(relay, "mutation"):
        return

    def _relay_mutation(*args: Any, **kwargs: Any):
        return strawberry.mutation(*args, **kwargs)

    setattr(relay, "mutation", _relay_mutation)


ensure_relay_mutation()


@strawberry.type
class CountableEdge(relay.Edge[NodeType], Generic[NodeType]):
    """Edge type for countable connection."""


@strawberry.type
class CountableConnection(relay.Connection[NodeType], Generic[NodeType]):
    """Relay connection variant that includes a total_count field."""

    edges: list[CountableEdge[NodeType]]
    page_info: relay.PageInfo
    total_count: int


def encode_cursor(offset: int) -> str:
    raw = f"{CURSOR_PREFIX}:{offset}"
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")


def decode_cursor(cursor: str) -> int:
    try:
        raw = base64.b64decode(cursor.encode("utf-8")).decode("utf-8")
        prefix, value = raw.split(":", 1)
        if prefix != CURSOR_PREFIX:
            raise ValueError("Invalid cursor prefix.")
        return int(value)
    except Exception as exc:
        raise ValueError("Invalid pagination cursor.") from exc


def _validate_limits(
    first: int | None,
    last: int | None,
    *,
    max_limit: int,
    default_limit: int,
) -> tuple[int | None, int | None]:
    if first is not None and last is not None:
        raise ValueError("Use either `first` or `last`, not both.")
    if first is not None and first < 0:
        raise ValueError("`first` must be positive.")
    if last is not None and last < 0:
        raise ValueError("`last` must be positive.")

    effective_max = max(max_limit, default_limit)
    if first is not None:
        first = min(first, effective_max)
    if last is not None:
        last = min(last, effective_max)
    return first, last


def _calculate_slice_bounds(
    total_count: int,
    *,
    first: int | None,
    after: str | None,
    last: int | None,
    before: str | None,
    default_limit: int,
) -> tuple[int, int]:
    start_offset = 0
    end_offset = total_count

    if after:
        start_offset = min(decode_cursor(after) + 1, total_count)
    if before:
        end_offset = min(decode_cursor(before), total_count)

    if first is not None:
        end_offset = min(end_offset, start_offset + first)
    elif last is not None:
        start_offset = max(start_offset, end_offset - last)
    else:
        end_offset = min(end_offset, start_offset + default_limit)

    if end_offset < start_offset:
        start_offset = end_offset

    return start_offset, end_offset


def _build_connection(
    records: Sequence[NodeType],
    *,
    start_offset: int,
    end_offset: int,
    total_count: int,
) -> CountableConnection[NodeType]:
    record_list = list(records)
    edges: list[CountableEdge[NodeType]] = [
        CountableEdge(
            cursor=encode_cursor(start_offset + idx),
            node=record,
        )
        for idx, record in enumerate(record_list)
    ]
    page_info = relay.PageInfo(
        has_previous_page=start_offset > 0,
        has_next_page=end_offset < total_count,
        start_cursor=edges[0].cursor if edges else None,
        end_cursor=edges[-1].cursor if edges else None,
    )
    return CountableConnection(
        edges=edges,
        page_info=page_info,
        total_count=total_count,
    )


def connection_from_queryset_sync(
    queryset: QuerySet,
    *,
    first: int | None = None,
    after: str | None = None,
    last: int | None = None,
    before: str | None = None,
    default_limit: int = 10,
    max_limit: int = 50,
) -> CountableConnection[NodeType]:
    """Build a relay connection from a queryset synchronously."""
    first, last = _validate_limits(
        first,
        last,
        max_limit=max_limit,
        default_limit=default_limit,
    )
    total_count = queryset.count()
    start_offset, end_offset = _calculate_slice_bounds(
        total_count,
        first=first,
        after=after,
        last=last,
        before=before,
        default_limit=default_limit,
    )
    records = list(queryset[start_offset:end_offset])
    return _build_connection(
        records,
        start_offset=start_offset,
        end_offset=end_offset,
        total_count=total_count,
    )


async def connection_from_queryset_async(
    queryset: QuerySet,
    *,
    first: int | None = None,
    after: str | None = None,
    last: int | None = None,
    before: str | None = None,
    default_limit: int = 10,
    max_limit: int = 50,
) -> CountableConnection[NodeType]:
    """Build a relay connection from a queryset asynchronously."""
    first, last = _validate_limits(
        first,
        last,
        max_limit=max_limit,
        default_limit=default_limit,
    )
    total_count = await sync_to_async(queryset.count)()
    start_offset, end_offset = _calculate_slice_bounds(
        total_count,
        first=first,
        after=after,
        last=last,
        before=before,
        default_limit=default_limit,
    )
    records = await sync_to_async(list)(queryset[start_offset:end_offset])
    return _build_connection(
        records,
        start_offset=start_offset,
        end_offset=end_offset,
        total_count=total_count,
    )
