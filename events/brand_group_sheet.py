"""Brand routing for retail-sheet confirmations.

The Famus / Alcohol Armor / Wook Water workbook is one spreadsheet and three
tenants. Column H decides which standing check-in links, BA materials, and
catalog SKUs a row may use. A brand that is not named in that cell is not
attached.

The Pivot Energy workbook is a separate spreadsheet with no Brand column.
Every row on that sheet is Pivot Energy Drink. It is not a fourth choice
in column H.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from events.models import Product
from tenants.models import Tenant

BRAND_GROUP_SHEET_ID = "1xnnzFb3s0mAV-6abyzmPzEX3wjtObD8Bv4qQsr8VB6A"

# External // Pivot Energy Retail Demos. gid 0, tab "Retail Schedule".
# No Brand column — registering the id is what selects Pivot.
PIVOT_SHEET_ID = "1kMuUWZtaEJ0rbvsDzE-uI3FkBTIJdX_LbnEqYaAHG4I"
PIVOT_TENANT_SLUG = "pivot-energy-drink"

# Standing walk-up codes. Do not remint. The email still builds the URL from
# Tenant.checkin_code; a mismatch with these codes refuses the send.
FAMUS_CHECKIN_CODE = "FM-PAG4TX"
ALCOHOL_ARMOR_CHECKIN_CODE = "AA-RPP85E"
WOOK_WATER_CHECKIN_CODE = "WW-36DJAJ"
PIVOT_CHECKIN_CODE = "PE-Y7VPYC"

# Extra confirmation-email buttons for Pivot only. BA Guide stays whatever
# the walk-up already stored. These two match the live training PDFs and are
# appended only when that tenant's checkin_resources does not already list them.
PIVOT_EMAIL_BUTTONS: tuple[tuple[str, str, str], ...] = (
    (
        "Retail Sampling FAQ",
        "/training/pivot-energy-drink/retail-sampling-faq.pdf",
        "Total Wine retail sampling guide",
    ),
    (
        "Product Comparison",
        "/training/pivot-energy-drink/product-comparison.pdf",
        "How Pivot compares with other energy drinks",
    ),
)


@dataclass(frozen=True)
class BrandRoute:
    slug: str
    labels: frozenset[str]
    standing_checkin_code: str


BRAND_ROUTES: tuple[BrandRoute, ...] = (
    BrandRoute(
        slug="famus-energy",
        labels=frozenset({"famus energy", "famus"}),
        standing_checkin_code=FAMUS_CHECKIN_CODE,
    ),
    BrandRoute(
        slug="alcohol-armor",
        labels=frozenset({"alcohol armor", "alcohol armour"}),
        standing_checkin_code=ALCOHOL_ARMOR_CHECKIN_CODE,
    ),
    BrandRoute(
        slug="wook-water",
        labels=frozenset({"wook water", "wookwater", "wook"}),
        standing_checkin_code=WOOK_WATER_CHECKIN_CODE,
    ),
)

_ROUTE_BY_LABEL: dict[str, BrandRoute] = {
    label: route for route in BRAND_ROUTES for label in route.labels
}
_STANDING_BY_SLUG: dict[str, str] = {
    route.slug: route.standing_checkin_code for route in BRAND_ROUTES
}
_STANDING_BY_SLUG[PIVOT_TENANT_SLUG] = PIVOT_CHECKIN_CODE

_BRAND_SPLIT = re.compile(r"\s*(?:,|;|/|&|\+|\n|\band\b)\s*", re.IGNORECASE)
_SKU_SPACE = re.compile(r"\s+")


def normalize_brand_label(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (raw or "").lower()).strip()


def parse_brand_slugs(raw: str | None) -> list[str]:
    """Ordered tenant slugs named in column H. Unknown text is an error.

    An empty cell is an error. This never falls back to all three brands.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Brand is required (column H)")

    slugs: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for part in _BRAND_SPLIT.split(text):
        label = normalize_brand_label(part)
        if not label:
            continue
        route = _ROUTE_BY_LABEL.get(label)
        if route is None:
            unknown.append(part.strip())
            continue
        if route.slug in seen:
            continue
        seen.add(route.slug)
        slugs.append(route.slug)

    if unknown:
        listed = ", ".join(repr(item) for item in unknown)
        raise ValueError(
            f"Unknown brand {listed}. Column H must name Famus Energy, "
            "Alcohol Armor, and/or Wook Water."
        )
    if not slugs:
        raise ValueError("Brand is required (column H)")
    return slugs


def standing_checkin_code(slug: str) -> str:
    return _STANDING_BY_SLUG.get((slug or "").strip(), "")


def assert_standing_checkin(tenant) -> None:
    """Refuse a send whose tenant code is not the standing walk-up code."""
    slug = (getattr(tenant, "slug", "") or "").strip()
    expected = standing_checkin_code(slug)
    if not expected:
        return
    actual = (getattr(tenant, "checkin_code", "") or "").strip()
    if actual != expected:
        raise ValueError(
            f"{slug} check-in code is {actual!r}; the standing code is {expected}"
        )


def _sku_key(name: str) -> str:
    return _SKU_SPACE.sub(" ", (name or "").strip().lower())


def match_sku_tokens(
    tokens: list[str], catalogs: dict[str, list[str]]
) -> list[str]:
    """Map sheet SKU text onto catalog names for the brands in ``catalogs``.

    ``catalogs`` is slug → product names and must already be limited to the
    brands named on the row. A token that is not in that catalog is refused.
    Exact name wins. Otherwise the token may be the leading words of exactly
    one catalog name (``OG Luxe`` → ``OG Luxe (Tropical Ice) 16oz``).
    """
    options: list[tuple[str, str]] = []
    for names in catalogs.values():
        for name in names:
            cleaned = (name or "").strip()
            if cleaned:
                options.append((cleaned, _sku_key(cleaned)))

    resolved: list[str] = []
    seen: set[str] = set()
    brand_list = ", ".join(catalogs) or "the brands on this row"
    for token in tokens:
        key = _sku_key(token)
        if not key:
            continue
        exact = [name for name, norm in options if norm == key]
        if len(exact) == 1:
            chosen = exact[0]
        elif len(exact) > 1:
            raise ValueError(f"SKU {token!r} matches more than one catalog product")
        else:
            prefix: list[str] = []
            if len(key) >= 4:
                for name, norm in options:
                    if norm.startswith(key) and (
                        len(norm) == len(key) or norm[len(key)] in " ("
                    ):
                        prefix.append(name)
            if len(prefix) == 1:
                chosen = prefix[0]
            elif not prefix:
                raise ValueError(
                    f"SKU {token!r} is not in the catalog for {brand_list}"
                )
            else:
                raise ValueError(
                    f"SKU {token!r} matches more than one catalog product"
                )
        chosen_key = _sku_key(chosen)
        if chosen_key in seen:
            continue
        seen.add(chosen_key)
        resolved.append(chosen)
    return resolved


def catalogs_for_tenants(tenants) -> dict[str, list[str]]:
    """Live Product names for these tenants only. The catalog is the SKU list."""
    slugs = [(getattr(tenant, "slug", "") or "").strip() for tenant in tenants]
    ids = [tenant.id for tenant in tenants if getattr(tenant, "id", None)]
    out: dict[str, list[str]] = {slug: [] for slug in slugs if slug}
    if not ids:
        return out
    rows = Product.objects.filter(tenant_id__in=ids).values_list(
        "tenant__slug", "name"
    )
    for slug, name in rows:
        cleaned = (name or "").strip()
        if slug and cleaned:
            out.setdefault(slug, []).append(cleaned)
    return out


def tenants_for_slugs(slugs: list[str]) -> list:
    """Load tenants in column-H order. Missing slugs raise."""
    found: dict[str, Tenant] = {}
    for tenant in Tenant.objects.filter(slug__in=slugs).order_by("id"):
        found.setdefault(tenant.slug, tenant)
    ordered = []
    missing = []
    for slug in slugs:
        tenant = found.get(slug)
        if tenant is None:
            missing.append(slug)
        else:
            ordered.append(tenant)
    if missing:
        raise ValueError(
            "Tenant not found for column H: " + ", ".join(missing)
        )
    return ordered
