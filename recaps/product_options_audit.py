"""Read-only audit: do recap product pickers offer the tenant's OWN products?

For every tenant, each recap template's product-choice fields (Products
Sampled and aliases, or any select/multiselect whose options are catalog
products — see ``recaps.product_choice_fields``) are compared against that
tenant's Product rows. The stored ``CustomField.options`` are what the web
check-in serves BAs, so they are what gets compared — even where the
GraphQL read-time resolver would paper over them with the live catalog.

Findings, most serious first:

- ``foreign``: an option is ANOTHER tenant's product and not this tenant's
  (the CoHo Imports clone that kept Mark Anthony's Long Drink SKUs).
- ``stale_type_filter``: the template's ``productsSampledProductTypes``
  names product lines this tenant doesn't have, so the read-time resolver
  filters the live catalog to nothing and falls back to stored options.
- ``unknown``: an option matches no tenant's catalog (renamed or deleted
  product, typo, or a free-text extra like "Other" on a detected field).
- ``missing``: a Products Sampled field doesn't offer one of the tenant's
  own products.

Nothing here writes. Fixes go through ``set_recap_choice_options`` (or a
re-run of the launch wizard for wizard-launched tenants) after review.
"""

from __future__ import annotations

from collections import defaultdict

from recaps.product_choice_fields import (
    catalog_keys_for_products,
    is_choice_field_type,
    option_product_name,
)
from recaps.products_sampled import PRODUCTS_SAMPLED_TYPES_KEY, is_products_sampled_field

ARCHIVED_PREFIX = "[ARCHIVED]"


def _norm(text: str | None) -> str:
    return str(text or "").strip().lower()


def audit_product_choice_options(
    *, tenant_slug: str | None = None, include_archived: bool = False
) -> dict:
    """Return ``{"summary": {...}, "fields": [...]}``. Read-only."""
    from events.models import Product
    from recaps.models import CustomField
    from tenants.models import Tenant

    tenants_qs = Tenant.objects.all().order_by("id")
    if tenant_slug:
        tenants_qs = tenants_qs.filter(slug__iexact=tenant_slug.strip())
    if not include_archived:
        tenants_qs = tenants_qs.exclude(name__istartswith=ARCHIVED_PREFIX)
    tenants = {t.id: t for t in tenants_qs}

    # Every tenant's catalog (not only the audited ones) — "foreign" means
    # any other tenant's product, archived or not.
    products_by_tenant: dict[int, list] = defaultdict(list)
    for product in Product.objects.select_related("product_type").order_by("id"):
        products_by_tenant[product.tenant_id].append(product)
    keys_by_tenant = {
        tid: catalog_keys_for_products(rows) for tid, rows in products_by_tenant.items()
    }
    owners: dict[str, set[int]] = defaultdict(set)
    for tid, keys in keys_by_tenant.items():
        for key in keys:
            owners[key].add(tid)
    tenant_names = dict(Tenant.objects.values_list("id", "name"))

    fields = (
        CustomField.objects.filter(custom_recap_template__tenant_id__in=list(tenants))
        .select_related("custom_field_type", "custom_recap_template")
        .order_by("custom_recap_template__tenant_id", "custom_recap_template_id", "order", "id")
    )

    rows: list[dict] = []
    for field in fields:
        type_name = getattr(field.custom_field_type, "name", "")
        if not is_choice_field_type(type_name):
            continue
        template = field.custom_recap_template
        tid = template.tenant_id
        options = [str(o) for o in (field.options or [])]
        named = is_products_sampled_field(field.name)

        def _owners_of(option: str) -> set[int]:
            key = _norm(option)
            found = set(owners.get(key, set()))
            found |= owners.get(_norm(option_product_name(option)), set())
            return found

        option_owners = {opt: _owners_of(opt) for opt in options}
        if not named and not any(option_owners.values()):
            continue  # an ordinary choice field (Sample format, Yes/No, …)

        own, foreign, unknown = [], [], []
        for opt, opt_owners in option_owners.items():
            if tid in opt_owners:
                own.append(opt)
            elif opt_owners:
                foreign.append(
                    {
                        "option": opt,
                        "owners": sorted(
                            f"[{o}] {tenant_names.get(o, '?')}" for o in opt_owners
                        ),
                    }
                )
            else:
                unknown.append(opt)

        missing: list[str] = []
        if named:
            offered = {_norm(option_product_name(o)) for o in options}
            for product in products_by_tenant.get(tid, []):
                name = (product.name or "").strip()
                if name and _norm(name) not in offered:
                    missing.append(name)

        stale_types: list[str] = []
        layout = template.layout if isinstance(template.layout, dict) else {}
        raw_types = layout.get(PRODUCTS_SAMPLED_TYPES_KEY)
        if named and isinstance(raw_types, list) and raw_types:
            own_types = {
                _norm(getattr(p.product_type, "name", ""))
                for p in products_by_tenant.get(tid, [])
            }
            stale_types = [str(t) for t in raw_types if _norm(t) not in own_types]

        issues = []
        if foreign:
            issues.append("foreign")
        if stale_types:
            issues.append("stale_type_filter")
        if unknown:
            issues.append("unknown")
        if missing:
            issues.append("missing")
        tenant = tenants[tid]
        rows.append(
            {
                "tenant_id": tid,
                "tenant_name": tenant.name,
                "tenant_slug": tenant.slug,
                "template_id": template.id,
                "template_name": template.name,
                "field_id": field.id,
                "field_name": field.name,
                "field_type": type_name,
                "named_products_sampled": named,
                "catalog_size": len(products_by_tenant.get(tid, [])),
                "option_count": len(options),
                "own": own,
                "foreign": foreign,
                "unknown": unknown,
                "missing": missing,
                "stale_type_filter": stale_types,
                "issues": issues,
            }
        )

    mismatched = [r for r in rows if r["issues"]]
    summary = {
        "tenants_audited": len(tenants),
        "product_fields": len(rows),
        "fields_with_issues": len(mismatched),
        "fields_with_foreign_options": sum(1 for r in rows if r["foreign"]),
        "fields_with_stale_type_filter": sum(1 for r in rows if r["stale_type_filter"]),
        "fields_with_unknown_options": sum(1 for r in rows if r["unknown"]),
        "fields_missing_products": sum(1 for r in rows if r["missing"]),
        "tenants_with_issues": sorted({f"[{r['tenant_id']}] {r['tenant_name']}" for r in mismatched}),
    }
    return {"summary": summary, "fields": rows}


def format_report(result: dict, *, include_ok: bool = False) -> str:
    lines: list[str] = []
    summary = result["summary"]
    lines.append("=" * 72)
    lines.append("Recap product-choice options vs each tenant's own catalog (READ-ONLY)")
    lines.append("=" * 72)
    order = {"foreign": 0, "stale_type_filter": 1, "unknown": 2, "missing": 3}
    rows = sorted(
        (r for r in result["fields"] if include_ok or r["issues"]),
        key=lambda r: (
            min((order[i] for i in r["issues"]), default=9),
            r["tenant_id"],
            r["template_id"],
            r["field_id"],
        ),
    )
    for r in rows:
        status = ", ".join(i.upper() for i in r["issues"]) or "OK"
        lines.append("")
        lines.append(
            f"[{status}] tenant [{r['tenant_id']}] {r['tenant_name']} ({r['tenant_slug']})"
        )
        lines.append(
            f"    template [{r['template_id']}] {r['template_name']!r} · field "
            f"[{r['field_id']}] {r['field_name']!r} <{r['field_type']}> · "
            f"{r['option_count']} option(s), catalog {r['catalog_size']} product(s)"
        )
        for item in r["foreign"]:
            lines.append(
                f"    FOREIGN  {item['option']!r} belongs to {', '.join(item['owners'])}"
            )
        if r["stale_type_filter"]:
            lines.append(
                f"    FILTER   layout.{PRODUCTS_SAMPLED_TYPES_KEY} names "
                f"{r['stale_type_filter']} — not product lines this tenant has"
            )
        for opt in r["unknown"]:
            lines.append(f"    UNKNOWN  {opt!r} matches no tenant's catalog")
        if r["missing"]:
            shown = ", ".join(repr(m) for m in r["missing"][:10])
            more = f" … +{len(r['missing']) - 10} more" if len(r["missing"]) > 10 else ""
            lines.append(f"    MISSING  own products not offered: {shown}{more}")
    lines.append("")
    lines.append("-" * 72)
    lines.append(
        f"Tenants audited: {summary['tenants_audited']} · product-choice fields: "
        f"{summary['product_fields']} · with issues: {summary['fields_with_issues']}"
    )
    lines.append(
        f"  foreign options: {summary['fields_with_foreign_options']} · stale type "
        f"filter: {summary['fields_with_stale_type_filter']} · unknown options: "
        f"{summary['fields_with_unknown_options']} · missing products: "
        f"{summary['fields_missing_products']}"
    )
    if summary["tenants_with_issues"]:
        lines.append("  tenants: " + "; ".join(summary["tenants_with_issues"]))
    lines.append("Nothing was changed. Review before fixing (set_recap_choice_options).")
    return "\n".join(lines)
