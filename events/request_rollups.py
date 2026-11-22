"""Server-side request rollups for dashboards and report charts.

Replaces multi-page client ``useAllConnectionPages`` walks over thousands of
``requests`` connection edges when screens only need daily/weekly counts,
request-type mix, or top upcoming retailers.
"""

from __future__ import annotations

from datetime import date, timezone as dt_timezone
import uuid as uuid_mod

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import Count, Value
from django.db.models.functions import Coalesce, NullIf, Trim, TruncDate

from events.models import Request
from events.queries import RequestQueriesService
from tenants.models import Tenant
from utils.graphql.mixins import SELECTED_TENANT_HEADER
from utils.graphql.permissions import IsClientOrSparkAdmin

DATE_FIELD_SCHEDULED = "date"
DATE_FIELD_CREATED = "created_at"
TOP_RETAILERS_LIMIT = 10


@strawberry.type
class RequestDayBucket:
    date: str
    count: int


@strawberry.type
class RequestNamedBucket:
    name: str
    count: int


@strawberry.type
class TenantRequestsRollup:
    total_count: int
    by_day: list[RequestDayBucket]
    by_request_type: list[RequestNamedBucket]
    top_retailers: list[RequestNamedBucket]


def _empty_rollup() -> TenantRequestsRollup:
    return TenantRequestsRollup(
        total_count=0,
        by_day=[],
        by_request_type=[],
        top_retailers=[],
    )


def _resolve_header_tenant_id(info: strawberry.Info) -> int | None:
    request = getattr(info.context, "request", None)
    headers = getattr(request, "headers", None) if request is not None else None
    raw_uuid = (headers.get(SELECTED_TENANT_HEADER) or "").strip() if headers else ""
    if not raw_uuid:
        return None
    try:
        parsed = uuid_mod.UUID(raw_uuid)
    except ValueError:
        return None
    return (
        Tenant.objects.filter(uuid=parsed)
        .values_list("id", flat=True)
        .first()
    )


def compute_tenant_requests_rollup(
    *,
    tenant_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
    date_field: str = DATE_FIELD_SCHEDULED,
) -> TenantRequestsRollup:
    """Aggregate live requests for one tenant in SQL without hydrating rows."""
    use_created = (date_field or "").strip().lower() in {
        "created_at",
        "createdat",
        "created",
    }
    col = "created_at" if use_created else "date"

    qs = Request.objects.filter(
        tenant_id=tenant_id,
        deleted_at__isnull=True,
        **{f"{col}__isnull": False},
    )
    if start_date is not None:
        qs = qs.filter(**{f"{col}__date__gte": start_date})
    if end_date is not None:
        qs = qs.filter(**{f"{col}__date__lte": end_date})

    day_rows = list(
        qs.annotate(_day=TruncDate(col, tzinfo=dt_timezone.utc))
        .values("_day")
        .annotate(c=Count("pk"))
        .order_by("_day")
    )
    by_day: list[RequestDayBucket] = []
    total_count = 0
    for row in day_rows:
        day_val = row["_day"]
        cnt = int(row["c"] or 0)
        if day_val is None or cnt <= 0:
            continue
        day_iso = day_val.isoformat() if isinstance(day_val, date) else str(day_val)[:10]
        by_day.append(RequestDayBucket(date=day_iso, count=cnt))
        total_count += cnt

    if total_count == 0:
        return _empty_rollup()

    type_counts: dict[str, int] = {}
    for row in qs.values("request_type__name").annotate(c=Count("pk")):
        cnt = int(row["c"] or 0)
        if cnt <= 0:
            continue
        label = (row["request_type__name"] or "").strip() or "Unspecified"
        type_counts[label] = type_counts.get(label, 0) + cnt

    by_request_type = [
        RequestNamedBucket(name=label, count=cnt)
        for label, cnt in sorted(
            type_counts.items(),
            key=lambda kv: (-kv[1], kv[0].casefold()),
        )
    ]

    retailer_rows = list(
        qs.annotate(
            _ret=Coalesce(
                NullIf(Trim("retailer__name"), Value("")),
                NullIf(Trim("retailer_name"), Value("")),
            )
        )
        .exclude(_ret__isnull=True)
        .values("_ret")
        .annotate(c=Count("pk"))
        .order_by("-c", "_ret")[:TOP_RETAILERS_LIMIT]
    )
    top_retailers = [
        RequestNamedBucket(name=str(row["_ret"]).strip(), count=int(row["c"] or 0))
        for row in retailer_rows
        if row.get("_ret") and int(row["c"] or 0) > 0
    ]

    return TenantRequestsRollup(
        total_count=total_count,
        by_day=by_day,
        by_request_type=by_request_type,
        top_retailers=top_retailers,
    )


@strawberry.type
class RequestRollupQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def tenant_requests_rollup(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        date_field: str = DATE_FIELD_SCHEDULED,
    ) -> TenantRequestsRollup:
        """Server-computed request counts by day, type, and top retailers."""
        service = RequestQueriesService()
        try:
            resolved_tenant_id = await service.resolve_tenant_id(
                info,
                tenant_id=tenant_id,
            )
        except Exception:
            resolved_tenant_id = await service.resolve_tenant_id(info)
        if resolved_tenant_id is None:
            resolved_tenant_id = await sync_to_async(_resolve_header_tenant_id)(info)
        if resolved_tenant_id is None:
            return _empty_rollup()

        return await sync_to_async(
            lambda: compute_tenant_requests_rollup(
                tenant_id=int(resolved_tenant_id),
                start_date=start_date,
                end_date=end_date,
                date_field=date_field,
            ),
            thread_sensitive=True,
        )()
