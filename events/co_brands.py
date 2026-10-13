"""Multi-brand activations on requests.

A request lives on its host tenant. When the client picks "multi-brand
activation", each sibling brand from the host's :class:`BrandGroup` is stored
as a :class:`RequestCoBrand` row with the SKUs (from that brand's own Product
catalog) to sample alongside the host's products.
"""

from __future__ import annotations

from typing import Any, Iterable

from django.db import transaction
from django.db.models import Prefetch
from graphql import GraphQLError

from events.models import Product, Request, RequestCoBrand
from tenants.models import ARCHIVED_NAME_PREFIX, Tenant
from utils.graphql.mixins import resolve_id_to_int


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        camel = "".join(
            part.capitalize() if i else part for i, part in enumerate(name.split("_"))
        )
        return item.get(name, item.get(camel))
    return getattr(item, name, None)


def _int_id(raw: Any, label: str) -> int:
    try:
        return resolve_id_to_int(raw)
    except (TypeError, ValueError, GraphQLError):
        raise GraphQLError(f"Invalid {label}.")


def resolve_co_brands(host_tenant_id: int, raw: Iterable[Any]) -> list[tuple[Tenant, list[int]]]:
    """Validate ``[{tenantId, productIds}]`` against the host's brand group.

    Each co-brand must be a different, non-archived tenant in the same group
    and needs at least one SKU from its own catalog.
    """
    items = list(raw or [])
    if not items:
        return []
    host = Tenant.objects.filter(id=host_tenant_id).only("id", "brand_group_id").first()
    if host is None or not host.brand_group_id:
        raise GraphQLError("This brand isn't set up for multi-brand activations.")

    resolved: list[tuple[Tenant, list[int]]] = []
    seen: set[int] = set()
    for item in items:
        tenant_id = _int_id(_field(item, "tenant_id"), "co-brand")
        if tenant_id == host.id:
            raise GraphQLError("Pick the other brands — this brand is already on the request.")
        if tenant_id in seen:
            continue
        tenant = (
            Tenant.objects.filter(id=tenant_id, brand_group_id=host.brand_group_id)
            .exclude(name__istartswith=ARCHIVED_NAME_PREFIX)
            .first()
        )
        if tenant is None:
            raise GraphQLError("That brand isn't part of this brand group.")
        product_ids = [
            _int_id(pid, "product") for pid in (_field(item, "product_ids") or [])
        ]
        product_ids = list(dict.fromkeys(product_ids))
        if not product_ids:
            raise GraphQLError(f"Pick at least one {tenant.name} SKU to sample.")
        valid = set(
            Product.objects.filter(id__in=product_ids, tenant_id=tenant.id).values_list(
                "id", flat=True
            )
        )
        if len(valid) != len(product_ids):
            raise GraphQLError(f"Some SKUs aren't in the {tenant.name} catalog.")
        seen.add(tenant_id)
        resolved.append((tenant, product_ids))
    return resolved


def replace_request_co_brands(request: Request, raw: Iterable[Any] | None) -> None:
    """Replace the request's co-brands. ``None`` leaves them untouched."""
    if raw is None:
        return
    resolved = resolve_co_brands(request.tenant_id, raw)
    with transaction.atomic():
        RequestCoBrand.objects.filter(request_id=request.id).exclude(
            tenant_id__in=[tenant.id for tenant, _ in resolved]
        ).delete()
        for tenant, product_ids in resolved:
            row, _ = RequestCoBrand.objects.get_or_create(
                request_id=request.id, tenant_id=tenant.id
            )
            row.products.set(product_ids)


def co_brand_rows(request: Request) -> list[RequestCoBrand]:
    if not request or not getattr(request, "id", None):
        return []
    cached = getattr(request, "_prefetched_objects_cache", {}).get("co_brands")
    if cached is not None:
        return list(cached)
    return list(
        RequestCoBrand.objects.filter(request_id=request.id)
        .select_related("tenant")
        .prefetch_related(
            Prefetch("products", queryset=Product.objects.select_related("product_type"))
        )
        .order_by("tenant__name", "id")
    )


def co_brand_lines(request: Request) -> list[str]:
    """``"FAMUS Energy: Original, Havana Nights"`` per co-brand."""
    lines: list[str] = []
    try:
        rows = co_brand_rows(request)
    except Exception:
        return lines
    for row in rows:
        names = sorted({(p.name or "").strip() for p in row.products.all()} - {""})
        label = (row.tenant.name or "").strip()
        lines.append(f"{label}: {', '.join(names)}" if names else label)
    return lines
