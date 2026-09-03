"""Products Sampled multiselect ↔ tenant Product catalog.

The upper Product Samples grid always reads live ``Product`` rows. The bottom
"Products Sampled" pills historically read a frozen ``CustomField.options``
JSON list seeded from a hardcoded SKU table (Liquid Death, Torch, …). Adding a
SKU to the catalog then showed on the grid but not the pills until someone
re-ran a seed command.

These helpers make the pills resolve from the same catalog at read time.
Stored ``CustomField.options`` remain a fallback for tenants without products
(e.g. Brew Dr. cans) and for non–Products-Sampled choice fields.
"""

from __future__ import annotations

PRODUCTS_SAMPLED_FIELD = "Products Sampled"

# Product Seeding (and similar) use a BA-facing label but still resolve from
# the tenant Product catalog — same pills → productSamples qty path.
PRODUCTS_SAMPLED_ALIASES = frozenset(
    {
        PRODUCTS_SAMPLED_FIELD.lower(),
        "cases dropped by sku",
        "cases by sku",
    }
)


def is_products_sampled_field(name: str | None) -> bool:
    """True when a custom field is the brand Products Sampled multiselect."""
    return (name or "").strip().lower() in PRODUCTS_SAMPLED_ALIASES


def products_sampled_options_for_tenant(tenant) -> list[str]:
    """Live catalog labels as ``Type — Name`` (same shape as LD / confirmation).

    Empty when the tenant has no Product rows — callers should fall back to
    whatever was stored on the CustomField.
    """
    from events.event_confirmations import catalog_product_options

    return catalog_product_options(tenant)


def resolve_products_sampled_options(
    *,
    field_name: str | None,
    tenant,
    stored: list[str] | None,
) -> list[str]:
    """Options the BA / admin should see for this field.

    Products Sampled + a non-empty catalog → catalog. Otherwise the stored
    JSON list (or []).
    """
    fallback = [str(o) for o in stored] if isinstance(stored, list) else []
    if not is_products_sampled_field(field_name):
        return fallback
    if tenant is None:
        return fallback
    live = products_sampled_options_for_tenant(tenant)
    return live if live else fallback


def tenant_id_for_custom_field(field) -> int | None:
    """Resolve the template's tenant without assuming relations are loaded."""
    tpl = getattr(field, "custom_recap_template", None)
    if tpl is not None:
        tid = getattr(tpl, "tenant_id", None)
        if tid:
            return int(tid)
        tenant = getattr(tpl, "tenant", None)
        if tenant is not None and getattr(tenant, "pk", None):
            return int(tenant.pk)

    pk = getattr(field, "pk", None)
    if not pk:
        return None

    from recaps.models import CustomField

    return (
        CustomField.objects.filter(pk=pk)
        .values_list("custom_recap_template__tenant_id", flat=True)
        .first()
    )


def resolve_field_options(field, stored: list[str] | None = None) -> list[str]:
    """Sync helper for the GraphQL CustomField.options resolver."""
    if stored is None:
        raw = field.__dict__.get("options")
        if raw is None:
            raw = getattr(field, "options", None)
        stored = raw if isinstance(raw, list) else []

    name = field.__dict__.get("name")
    if name is None:
        name = getattr(field, "name", None)

    tid = tenant_id_for_custom_field(field)
    tenant = None
    if tid:
        from tenants.models import Tenant

        tenant = Tenant.objects.filter(pk=tid).only("id", "name", "slug").first()

    return resolve_products_sampled_options(
        field_name=name,
        tenant=tenant,
        stored=list(stored) if stored else [],
    )
