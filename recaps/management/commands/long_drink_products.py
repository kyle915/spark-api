"""The Finnish Long Drink SKU catalog + can artwork URLs.

Shared by onboard + Long Drink Total Wine retail setup. Labels stay
``The Finnish Long Drink — {flavor}`` so they match existing MAB catalog
rows from ``mab_products`` / ``onboard_mab_products``.
"""

from __future__ import annotations

PRODUCT_TYPE = "The Finnish Long Drink"

# (flavor name, image_url). Artwork from Total Wine dynamic CDN when the
# SKU has a product page there; otherwise readytodrinkcocktails.com CDN.
# Empty URL = create the Product row without artwork (Torch pattern).
LONG_DRINK_PRODUCTS: list[tuple[str, str]] = [
    (
        "Traditional Citrus",
        "https://www.totalwine.com/dynamic/x1000,6pk/images/219024118/219024118-2-fr.png",
    ),
    (
        "Cranberry",
        "https://www.totalwine.com/dynamic/x1000,6pk/images/228324118/228324118-2-fr.png",
    ),
    (
        "Peach",
        "https://www.totalwine.com/dynamic/x1000,6pk/images/2126238446/2126238446-2-fr.png",
    ),
    (
        "Raspberry",
        "https://readytodrinkcocktails.com/cdn/shop/files/the-finnish-long-drink-raspberry.jpg",
    ),
    (
        "Pineapple",
        "https://readytodrinkcocktails.com/cdn/shop/files/the-finnish-long-drink-pineapple.jpg",
    ),
    (
        "Strong Citrus",
        "https://www.totalwine.com/dynamic/x1000,6pk/images/228325118/228325118-2-fr.png",
    ),
    (
        "Zero Sugar Citrus",
        "https://www.totalwine.com/dynamic/x1000,6pk/images/184354118/184354118-2-fr.png",
    ),
    (
        "Zero Sugar Peach",
        "https://readytodrinkcocktails.com/cdn/shop/files/the-finnish-long-drink-zero-sugar-peach.jpg",
    ),
    (
        "Zero Sugar Pineapple",
        "https://readytodrinkcocktails.com/cdn/shop/files/the-finnish-long-drink-zero-sugar-pineapple.jpg",
    ),
]


def long_drink_product_options() -> list[str]:
    """SKU labels as ``Type — Name`` for Products Sampled."""
    return [f"{PRODUCT_TYPE} — {name}" for name, _url in LONG_DRINK_PRODUCTS]


def long_drink_flavor_names() -> list[str]:
    return [name for name, _url in LONG_DRINK_PRODUCTS]
