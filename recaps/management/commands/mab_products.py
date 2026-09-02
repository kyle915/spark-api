"""Mark Anthony Brands product catalog (shared by seed + onboard).

Category — Name labels keep a long SKU list scannable on a phone in-aisle,
matching Liquid Death's Products Sampled convention. One source of truth for:
* CustomField.options on both recap templates
* ProductType + Product rows (event confirmation + qty mapping)
"""

from __future__ import annotations

PRODUCTS: list[tuple[str, list[str]]] = [
    (
        'White Claw',
        [
            'Black Cherry',
            'Cranberry',
            'Pineapple',
            'Mango',
            'Peach',
            'Watermelon',
            'Lemon',
            'Blackberry',
            'Strawberry',
            'Green Apple',
            'Grape',
            'Blood Orange',
            'Ruby Grapefruit',
            'Raspberry',
            'Natural Lime',
            'Surge Blood Orange',
            'Surge Grape',
            'Surge Cranberry',
            'Surge Blackberry',
            'Surge Blueberry',
            'Surge Green Apple',
            'Surge Pineapple',
            'Surge Lime',
            'Surge Strawberry',
            'Surge Peach',
            'Surge Black Cherry',
            'Clawtails Strawberry Cosmo',
            'Clawtails Mango Margarita',
            'Clawtails Wild Berry Mojito',
            'Clawtails Peach Daiquiri',
            'Zero Proof Black Cherry Cranberry',
            'Zero Proof Peach Orange Blossom',
            'Zero Proof Mango Passion Fruit',
            'Zero Proof Lime Yuzu',
            'Vodka + Soda Pineapple',
            'Vodka + Soda Peach',
            'Vodka + Soda Wild Cherry',
            'Vodka + Soda Watermelon',
            'Vodka + Soda Cranberry',
            'Vodka + Soda Mango',
            'Vodka + Soda Lemon',
            'Vodka + Soda Guava',
            'Tequila Smash Pineapple Passion Fruit',
            'Tequila Smash Mango Tamarind',
            'Tequila Smash Lime Prickly Pear',
            'Tequila Smash Strawberry Guava',
            'White Claw Premium Vodka',
            'White Claw Black Cherry Vodka',
            'White Claw Mango Vodka',
            'White Claw Pineapple Vodka',
        ],
    ),
    (
        "Mike's Hard Lemonade",
        [
            'Original Lemonade',
            'Pink Lemonade',
            'Strawberry Pineapple',
            'Black Cherry',
            'Strawberry',
            'Mango',
            'Peach',
            'Strawberry Kiwi',
            'Limeade',
            'Blue Freeze',
            'White Freeze',
            'Pink Freeze',
            'Red Freeze',
            'Zero Sugar Black Cherry',
            'Zero Sugar Tropical',
            'Zero Sugar Watermelon',
            'Zero Sugar Strawberry',
            'Zero Sugar Mango',
            'Zero Sugar Lemonade',
        ],
    ),
    (
        "Mike's HARDER",
        [
            'Pink Lemonade',
            'Blue Raspberry',
            'Green Apple',
            'Pineapple Mandarin',
            'Strawberry Pineapple',
            'Mango',
            'Black Cherry',
            'Piña Colada',
            'Hurricane Punch',
            'Strawberry',
            'Cranberry',
            'Lemonade',
        ],
    ),
    (
        "Mike's Dirty",
        [
            'Dirty Lemon Secret',
            'Pineapple Haze',
            'Dark Cherry Brew',
            'Very Berry Grape',
        ],
    ),
    (
        'Cayman Jack',
        [
            'Margarita',
            'Strawberry Margarita',
            'Mango Margarita',
            'Blood Orange Margarita',
            'Smoky Orange Margarita',
            'Spicy Lime Margarita',
            'Sweet Heat Peach Margarita',
            'Grilled Pineapple Margarita',
            'Paloma',
            'Mexican Sunrise',
            'Agave Mule',
            'Zero Sugar Margarita',
            'Zero Sugar Strawberry Margarita',
            'Zero Sugar Mango Margarita',
            'Zero Sugar Passionfruit Margarita',
            'Cayman Jacked Tropical',
            'Cayman Jacked Margarita',
            'Cayman Jacked Strawberry',
        ],
    ),
    (
        'MXD',
        [
            'Long Island Iced Tea',
            'Margarita',
            'Mai Tai',
            'Strawberry Daiquiri',
            'Blue Hawaiian',
        ],
    ),
    (
        'The Finnish Long Drink',
        [
            'Traditional Citrus',
            'Cranberry',
            'Peach',
            'Raspberry',
            'Pineapple',
            'Strong Citrus',
            'Zero Sugar Citrus',
            'Zero Sugar Peach',
            'Zero Sugar Pineapple',
        ],
    ),
    (
        'Olé Cocktails',
        [
            'Paloma',
            'Margarita',
            'Chili Mango',
            'Tequila Sunrise',
            'Spicy Margarita',
            'Passion Fruit Margarita',
            'Pineapple',
            'Double Shot Paloma',
            'Double Shot Margarita',
            'NA Paloma',
            'NA Margarita',
            'NA Chili Mango',
            'NA Pineapple',
        ],
    ),
    (
        "Dillon's",
        [
            'Blackberry, Lemon & Elderflower',
            'Tangerine, Lemon & Mint',
            'Gin Cocktail Variety Pack',
            'Vodka Cocktail Variety Pack',
        ],
    ),
    (
        '2 Hoots Hard Iced Tea',
        [
            'Original Hard Iced Tea',
            'Half & Half',
        ],
    ),
    (
        'Rey Azul Tequila & Soda',
        [
            'Grapefruit',
            'Lime',
            'Mango',
            'Pineapple',
            'Variety Pack',
        ],
    ),
]


def product_options() -> list[str]:
    """SKU labels as ``"Category — Name"`` choice values."""
    return [f"{cat} — {name}" for cat, names in PRODUCTS for name in names]


def flat_product_rows() -> list[tuple[str, str]]:
    """(product_type, product_name) rows for catalog seeding."""
    return [(cat, name) for cat, names in PRODUCTS for name in names]


# Alias used by event-confirmation catalog-empty fallbacks / onboard.
mab_product_options = product_options
