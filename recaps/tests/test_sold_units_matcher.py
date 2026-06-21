"""SOLD on the recap list (CustomRecap.soldUnits) sums a template's "sold"
fields by NAME. Drink tenants (Girl Beer) break it into cans/packs; bread
tenants (Stone House Bread) use a single "Products Sold" / "Loaves Sold"
field that the cans/packs-only matcher missed — so SOLD showed "—". This
covers the two-tier matcher: cans/packs primary, generic "sold" fallback.
"""

from recaps.types import _sold_units_from_fields


def test_cans_packs_summed_girl_beer():
    pairs = [("Cans Sold", "30"), ("Packs Sold", "12"), ("Notes", "n/a")]
    assert _sold_units_from_fields(pairs) == 42


def test_bread_products_sold_fallback_stone_house():
    # No cans/packs field — must fall back to the "sold" field.
    assert _sold_units_from_fields([("Products Sold", "60")]) == 60
    assert _sold_units_from_fields([("Loaves Sold", "36")]) == 36
    assert _sold_units_from_fields([("Total Units Sold", "18")]) == 18


def test_cans_packs_take_precedence_no_double_count():
    # A template with BOTH granular cans/packs AND a redundant total must
    # count only the granular tier — never both.
    pairs = [("Cans Sold", "30"), ("Packs Sold", "12"), ("Total Sold", "42")]
    assert _sold_units_from_fields(pairs) == 42  # not 84


def test_non_numeric_sold_field_skipped():
    # A "Sold out?" yes/no field matches the name but isn't a count — the
    # int parse drops it, so it doesn't fabricate a value.
    assert _sold_units_from_fields([("Sold out?", "Yes")]) is None
    # ...but a real number alongside it still counts.
    assert _sold_units_from_fields(
        [("Sold out?", "No"), ("Products Sold", "5")]
    ) == 5


def test_no_sold_field_returns_none():
    # Non-sales template → "—", not a misleading 0.
    assert _sold_units_from_fields(
        [("Consumers Sampled", "100"), ("Corporate Card Used", "Yes")]
    ) is None
