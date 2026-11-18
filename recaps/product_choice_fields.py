"""Which recap choice fields are "product pickers", and what a catalog offers.

A product-choice field is a select/multiselect whose options are a tenant's
Product catalog: the "Products Sampled" pills (and their aliases in
``recaps.products_sampled``), or any other choice field whose options are
catalog product names. Cloning a template onto a new brand must rewrite
these from the NEW tenant's catalog; copying them verbatim hands the new
client a dropdown of someone else's products (the CoHo Imports bug, where a
cloned Mark Anthony template kept "The Finnish Long Drink — …" options).

Options are stored either as bare product names or as ``Type — Name``
labels (``events.event_confirmations.catalog_product_options``); both forms
are recognised here.
"""

from __future__ import annotations

from events.event_confirmations import PRODUCT_CATEGORY_SEPARATOR
from recaps.products_sampled import is_products_sampled_field

CHOICE_FIELD_TYPES = frozenset({"select", "multiselect"})


def is_choice_field_type(type_name: str | None) -> bool:
    name = (type_name or "").strip().lower()
    return name in CHOICE_FIELD_TYPES or "select" in name


def option_product_name(option: str | None) -> str:
    """``"Type — Name"`` → ``"Name"``; a bare name is returned as-is."""
    text = str(option or "").strip()
    if PRODUCT_CATEGORY_SEPARATOR in text:
        return text.split(PRODUCT_CATEGORY_SEPARATOR, 1)[1].strip()
    return text


def catalog_keys_for_products(products) -> set[str]:
    """Lower-cased bare names AND ``type — name`` labels for Product rows."""
    keys: set[str] = set()
    for product in products:
        name = (getattr(product, "name", "") or "").strip()
        if not name:
            continue
        keys.add(name.lower())
        ptype = getattr(product, "product_type", None)
        type_name = (getattr(ptype, "name", "") or "").strip()
        if type_name:
            keys.add(f"{type_name}{PRODUCT_CATEGORY_SEPARATOR}{name}".lower())
    return keys


def catalog_keys_for_tenant(tenant_id: int | None) -> set[str]:
    if not tenant_id:
        return set()
    from events.models import Product

    return catalog_keys_for_products(
        Product.objects.filter(tenant_id=tenant_id).select_related("product_type")
    )


def option_in_catalog(option: str | None, keys: set[str]) -> bool:
    text = str(option or "").strip().lower()
    if not text or not keys:
        return False
    return text in keys or option_product_name(text).lower() in keys


def is_product_choice_field(
    *,
    name: str | None,
    type_name: str | None,
    options: list | None,
    catalog_keys: set[str],
) -> bool:
    """A choice field named like Products Sampled, or whose options include
    at least one of the given catalog's products."""
    if not is_choice_field_type(type_name):
        return False
    if is_products_sampled_field(name):
        return True
    return any(option_in_catalog(o, catalog_keys) for o in (options or []))
