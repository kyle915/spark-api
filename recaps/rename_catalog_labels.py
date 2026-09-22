"""Rename a Product or ProductType and relabel filed Products Sampled answers.

Stored answers keep the label string (``Type — Name`` or the bare SKU).
Changing the catalog row does not touch those strings unless we rewrite them.
Quantities and unrelated fields stay as they are. No SKUs are added.
"""

from __future__ import annotations

import json

from django.db import transaction
from graphql import GraphQLError

from recaps.products_sampled import is_products_sampled_field

_SEPARATORS = (" — ", " – ", " - ", "- ")
_NAME_MAX = 50


def label_pairs(old: str, new: str, *, type_name: str = "") -> list[tuple[str, str]]:
    """Exact stored labels that should move from ``old`` to ``new``."""
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or old == new:
        return []
    pairs: list[tuple[str, str]] = []
    prefix = (type_name or "").strip()
    if prefix:
        for sep in _SEPARATORS:
            pairs.append((f"{prefix}{sep}{old}", f"{prefix}{sep}{new}"))
    pairs.append((old, new))
    return pairs


def type_label_pairs(
    old_type: str, new_type: str, product_names: list[str]
) -> list[tuple[str, str]]:
    """``OldType — SKU`` → ``NewType — SKU`` for each product. Bare names stay."""
    old_type = (old_type or "").strip()
    new_type = (new_type or "").strip()
    if not old_type or old_type == new_type:
        return []
    pairs: list[tuple[str, str]] = []
    for name in product_names:
        sku = (name or "").strip()
        if not sku:
            continue
        for sep in _SEPARATORS:
            pairs.append((f"{old_type}{sep}{sku}", f"{new_type}{sep}{sku}"))
    return pairs


def _swap_text(text: str, pairs: list[tuple[str, str]]) -> str:
    raw = text.strip()
    for old, new in pairs:
        if raw == old:
            return new
    return text


def _rewrite_node(node, pairs: list[tuple[str, str]]):
    """Return (node, changed). Numbers and qty keys are left alone."""
    if isinstance(node, str):
        nxt = _swap_text(node, pairs)
        return nxt, nxt != node
    if isinstance(node, list):
        changed = False
        out = []
        for item in node:
            nxt, item_changed = _rewrite_node(item, pairs)
            changed = changed or item_changed
            out.append(nxt)
        return out, changed
    if isinstance(node, dict):
        changed = False
        out = dict(node)
        for key in ("label", "name", "product", "sku", "value"):
            current = out.get(key)
            if isinstance(current, str):
                nxt = _swap_text(current, pairs)
                if nxt != current:
                    out[key] = nxt
                    changed = True
        return out, changed
    return node, False


def rewrite_stored_value(raw: str | None, pairs: list[tuple[str, str]]) -> tuple[str, bool]:
    """Rewrite one CustomFieldValue.value. Returns (new_text, changed)."""
    if not pairs or raw is None:
        return raw or "", False
    text = str(raw)
    stripped = text.strip()
    if not stripped:
        return text, False
    try:
        parsed = json.loads(stripped)
    except Exception:
        nxt = _swap_text(stripped, pairs)
        return (nxt, nxt != stripped)
    rewritten, changed = _rewrite_node(parsed, pairs)
    if not changed:
        return text, False
    if isinstance(rewritten, str):
        return rewritten, True
    return json.dumps(rewritten, ensure_ascii=False), True


def _require_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise GraphQLError("Name is required.")
    if len(cleaned) > _NAME_MAX:
        raise GraphQLError(f"Name must be {_NAME_MAX} characters or fewer.")
    return cleaned


def _actor_may_edit(user, tenant_id: int) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    from tenants.models import TenantedUser
    from utils.graphql.permissions import email_grants_ignite_admin

    if email_grants_ignite_admin(getattr(user, "email", "") or ""):
        return True
    return TenantedUser.objects.filter(
        user_id=getattr(user, "id", None),
        tenant_id=tenant_id,
        is_active=True,
    ).exists()


def _rewrite_tenant_sampled(tenant_id: int, pairs: list[tuple[str, str]]) -> int:
    """Rewrite Products Sampled answers. Returns how many recaps changed."""
    if not pairs:
        return 0
    from recaps.models import CustomField, CustomFieldValue

    fields = list(
        CustomField.objects.filter(
            custom_recap_template__tenant_id=tenant_id,
        ).only("id", "name", "options")
    )
    sampled = [field for field in fields if is_products_sampled_field(field.name)]
    if not sampled:
        return 0

    for field in sampled:
        options = field.options
        if not isinstance(options, list):
            continue
        nxt, changed = _rewrite_node(options, pairs)
        if changed:
            field.options = nxt
            field.save(update_fields=["options"])

    recap_ids: set[int] = set()
    values = CustomFieldValue.objects.filter(
        custom_field_id__in=[field.id for field in sampled]
    ).only("id", "value", "custom_recap_id")
    for row in values.iterator():
        new_value, changed = rewrite_stored_value(row.value, pairs)
        if not changed:
            continue
        row.value = new_value
        row.save(update_fields=["value"])
        recap_ids.add(row.custom_recap_id)
    return len(recap_ids)


def rename_catalog_label(*, user, product_id: int | None, product_type_id: int | None, name: str) -> int:
    """Rename one catalog row and relabel that tenant's filed answers.

    Pass exactly one of ``product_id`` or ``product_type_id``.
    Returns the number of recaps whose Products Sampled answers changed.
    """
    from events.models import Product, ProductType

    new_name = _require_name(name)
    if bool(product_id) == bool(product_type_id):
        raise GraphQLError("Choose a product or a product type to rename.")

    with transaction.atomic():
        if product_id:
            product = (
                Product.objects.select_related("product_type")
                .select_for_update()
                .filter(pk=product_id)
                .first()
            )
            if product is None:
                raise GraphQLError("Product not found.")
            if not _actor_may_edit(user, product.tenant_id):
                raise GraphQLError("You do not have permission to rename this product.")
            old_name = (product.name or "").strip()
            if old_name == new_name:
                return 0
            clash = (
                Product.objects.filter(
                    tenant_id=product.tenant_id,
                    product_type_id=product.product_type_id,
                    name__iexact=new_name,
                )
                .exclude(pk=product.pk)
                .exists()
            )
            if clash:
                raise GraphQLError("Another product in this type already uses that name.")
            type_name = (getattr(product.product_type, "name", "") or "").strip()
            pairs = label_pairs(old_name, new_name, type_name=type_name)
            product.name = new_name
            if user is not None and getattr(user, "id", None):
                product.updated_by = user
                product.save(update_fields=["name", "updated_by", "updated_at"])
            else:
                product.save(update_fields=["name", "updated_at"])
            return _rewrite_tenant_sampled(product.tenant_id, pairs)

        product_type = (
            ProductType.objects.select_for_update().filter(pk=product_type_id).first()
        )
        if product_type is None:
            raise GraphQLError("Product type not found.")
        if not _actor_may_edit(user, product_type.tenant_id):
            raise GraphQLError("You do not have permission to rename this product type.")
        old_name = (product_type.name or "").strip()
        if old_name == new_name:
            return 0
        clash = (
            ProductType.objects.filter(
                tenant_id=product_type.tenant_id,
                name__iexact=new_name,
            )
            .exclude(pk=product_type.pk)
            .exists()
        )
        if clash:
            raise GraphQLError("Another product type already uses that name.")
        product_names = list(
            Product.objects.filter(product_type_id=product_type.pk).values_list(
                "name", flat=True
            )
        )
        pairs = type_label_pairs(old_name, new_name, product_names)
        product_type.name = new_name
        if user is not None and getattr(user, "id", None):
            product_type.updated_by = user
            product_type.save(update_fields=["name", "updated_by", "updated_at"])
        else:
            product_type.save(update_fields=["name", "updated_at"])
        return _rewrite_tenant_sampled(product_type.tenant_id, pairs)
