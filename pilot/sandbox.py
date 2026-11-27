"""Interactive Self-Serve Sandbox ("Explore Sample Brand") read-only payload.

Serves a rich, completely self-contained fictional CPG beverage brand workspace
("Solstice Botanical Sparkling") over ``GET /api/public/sandbox-brand`` so
prospective founders can explore Program Reports, StoreSheet, Shelf & Voice,
Photo Wall, Buyer QBR Decks (with live .pptx export), Saved Views, and Ask Spark
with zero login friction and zero risk to live tenant data.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from urllib.parse import quote


def _svg_data_uri(
    *,
    title: str,
    subtitle: str,
    badge: str,
    accent: str,
    bg: str,
    cans: int = 5,
    gap_index: int | None = None,
    price_tag: str | None = "$2.49",
) -> str:
    """Generate a crisp, reliable inline SVG shelf / activation visual card."""
    can_elems: list[str] = []
    start_x = 76
    step = 76
    for idx in range(min(cans, 6)):
        x = start_x + idx * step
        if gap_index is not None and idx == gap_index:
            can_elems.append(
                f'<rect x="{x}" y="118" width="54" height="128" rx="10" '
                f'fill="none" stroke="#c2410c" stroke-width="2.5" stroke-dasharray="6 5"/>'
                f'<text x="{x + 27}" y="186" text-anchor="middle" '
                f'font-family="system-ui,sans-serif" font-size="11" font-weight="700" fill="#a8340f">OOS</text>'
            )
        else:
            can_elems.append(
                f'<rect x="{x}" y="112" width="54" height="134" rx="10" fill="{accent}" opacity="0.92"/>'
                f'<rect x="{x + 6}" y="122" width="42" height="10" rx="3" fill="#ffffff" opacity="0.35"/>'
                f'<text x="{x + 27}" y="176" text-anchor="middle" '
                f'font-family="system-ui,sans-serif" font-size="10" font-weight="700" fill="#ffffff">SOLSTICE</text>'
                f'<text x="{x + 27}" y="192" text-anchor="middle" '
                f'font-family="system-ui,sans-serif" font-size="8.5" fill="#f6f6f2" opacity="0.9">12oz</text>'
            )

    tag_elem = (
        f'<rect x="448" y="254" width="84" height="32" rx="6" fill="#ffffff" stroke="#14160f" stroke-width="1.5"/>'
        f'<text x="490" y="275" text-anchor="middle" font-family="ui-monospace,monospace" '
        f'font-size="15" font-weight="700" fill="#14160f">{price_tag}</text>'
        if price_tag
        else '<rect x="424" y="254" width="108" height="32" rx="6" fill="#fff1ec" stroke="#c2410c" stroke-width="1.5"/>'
        '<text x="478" y="274" text-anchor="middle" font-family="system-ui,sans-serif" '
        'font-size="11" font-weight="700" fill="#a8340f">MISSING TAG</text>'
    )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360" width="600" height="360">'
        f'<rect width="600" height="360" fill="{bg}"/>'
        f'<rect x="28" y="24" width="544" height="312" rx="18" fill="#ffffff" stroke="#dcddd5" stroke-width="1.5"/>'
        f'<rect x="48" y="44" width="160" height="24" rx="12" fill="#f2f3ee" stroke="#dcddd5"/>'
        f'<text x="128" y="60" text-anchor="middle" font-family="ui-monospace,monospace" font-size="10.5" font-weight="700" fill="#14160f">{badge}</text>'
        f'<text x="48" y="92" font-family="system-ui,sans-serif" font-size="16" font-weight="700" fill="#14160f">{title}</text>'
        f'<rect x="48" y="246" width="504" height="10" rx="4" fill="#d6d7ce"/>'
        f'{"".join(can_elems)}'
        f'{tag_elem}'
        f'<text x="48" y="276" font-family="system-ui,sans-serif" font-size="12" fill="#5b6052">{subtitle}</text>'
        f'<text x="48" y="314" font-family="ui-monospace,monospace" font-size="10" fill="#7c8270">SPARK VERIFIED FIELD PROOF · SAMPLE BRAND SANDBOX</text>'
        f"</svg>"
    )
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


def build_sandbox_sample_brand(*, today: date | None = None) -> dict[str, Any]:
    """Return the complete, deterministic read-only Sample Brand Sandbox payload."""
    ref = today or date.today()
    start_90 = ref - timedelta(days=89)

    def _d(days_ago: int) -> str:
        return (ref - timedelta(days=days_ago)).isoformat()

    stores: list[dict[str, Any]] = [
        {
            "storeKey": "sandbox:wfm-austin-lamar",
            "storeName": "Whole Foods Market — Lamar Flagship",
            "retailerBanner": "Whole Foods Market",
            "market": "Austin, TX",
            "stateCode": "TX",
            "address": "525 N Lamar Blvd, Austin, TX 78703",
            "visits": 6,
            "consumersSampled": 612,
            "unitsSold": 224,
            "conversionPct": 36.6,
            "osaRatePct": 100.0,
            "latestStatus": "on_shelf",
            "latestFacings": 6,
            "avgFacings": 5.5,
            "latestPrice": "2.49",
            "missingTagVisits": 0,
            "oosGapVisits": 0,
            "posLiftPct": 38.4,
            "demoCost": 1350.0,
            "costPerSample": 2.21,
            "stockoutSignal": "",
            "competitorPromo": "Olipop endcap BOGO on aisle 4",
            "latestDate": _d(3),
            "topSku": "Yuzu Ginger 12oz",
            "quotes": [
                "Shoppers kept comparing the yuzu finish to Spindrift — 4-packs moved fastest after tasting.",
                "Buyer walked by at 2pm and asked to add Blood Orange Hibiscus to the cold grab-and-go set.",
            ],
        },
        {
            "storeKey": "sandbox:heb-houston-montrose",
            "storeName": "H-E-B #725 — Montrose",
            "retailerBanner": "H-E-B",
            "market": "Houston, TX",
            "stateCode": "TX",
            "address": "1701 W Alabama St, Houston, TX 77098",
            "visits": 5,
            "consumersSampled": 528,
            "unitsSold": 176,
            "conversionPct": 33.3,
            "osaRatePct": 80.0,
            "latestStatus": "on_shelf",
            "latestFacings": 4,
            "avgFacings": 3.4,
            "latestPrice": "2.79",
            "missingTagVisits": 0,
            "oosGapVisits": 1,
            "posLiftPct": 31.2,
            "demoCost": 1125.0,
            "costPerSample": 2.13,
            "stockoutSignal": "Yuzu Ginger depleted by 4pm Saturday — backstock restocked Sunday morning.",
            "competitorPromo": "Poppi 2/$4 TPR running all month",
            "latestDate": _d(5),
            "topSku": "Yuzu Ginger 12oz",
            "quotes": [
                "Customers loved the zero-stevia ingredient panel; several picked up two 4-packs.",
            ],
        },
        {
            "storeKey": "sandbox:wfm-dallas-parklane",
            "storeName": "Whole Foods Market — Park Lane",
            "retailerBanner": "Whole Foods Market",
            "market": "Dallas, TX",
            "stateCode": "TX",
            "address": "8190 Park Ln, Dallas, TX 75231",
            "visits": 4,
            "consumersSampled": 390,
            "unitsSold": 118,
            "conversionPct": 30.3,
            "osaRatePct": 75.0,
            "latestStatus": "not_found",
            "latestFacings": 0,
            "avgFacings": 2.0,
            "latestPrice": "2.89",
            "missingTagVisits": 2,
            "oosGapVisits": 2,
            "posLiftPct": 19.5,
            "demoCost": 900.0,
            "costPerSample": 2.31,
            "stockoutSignal": "Blood Orange Hibiscus missing from cold box; shelf tag also missing on ambient aisle.",
            "competitorPromo": "Liquid Death sparkling tea display at front entrance",
            "latestDate": _d(6),
            "topSku": "Blood Orange Hibiscus 12oz",
            "quotes": [
                "Shoppers tried samples at the table then asked where to grab chilled singles — cooler was empty.",
            ],
        },
        {
            "storeKey": "sandbox:erewhon-santa-monica",
            "storeName": "Erewhon — Santa Monica",
            "retailerBanner": "Erewhon",
            "market": "Los Angeles, CA",
            "stateCode": "CA",
            "address": "2800 Wilshire Blvd, Santa Monica, CA 90403",
            "visits": 5,
            "consumersSampled": 445,
            "unitsSold": 168,
            "conversionPct": 37.8,
            "osaRatePct": 100.0,
            "latestStatus": "on_shelf",
            "latestFacings": 5,
            "avgFacings": 5.0,
            "latestPrice": "2.99",
            "missingTagVisits": 0,
            "oosGapVisits": 0,
            "posLiftPct": 41.0,
            "demoCost": 1250.0,
            "costPerSample": 2.81,
            "stockoutSignal": "",
            "competitorPromo": "",
            "latestDate": _d(4),
            "topSku": "Meyer Lemon Rosemary 12oz",
            "quotes": [
                "Meyer Lemon Rosemary resonated strongly with afternoon fitness shoppers.",
                "Multiple repeat buyers noted they saw Solstice on Instagram and bought 6 singles.",
            ],
        },
        {
            "storeKey": "sandbox:sprouts-denver-colfax",
            "storeName": "Sprouts Farmers Market — Colfax",
            "retailerBanner": "Sprouts",
            "market": "Denver, CO",
            "stateCode": "CO",
            "address": "3625 E Colfax Ave, Denver, CO 80206",
            "visits": 4,
            "consumersSampled": 356,
            "unitsSold": 98,
            "conversionPct": 27.5,
            "osaRatePct": 100.0,
            "latestStatus": "on_shelf",
            "latestFacings": 2,
            "avgFacings": 2.0,
            "latestPrice": "2.49",
            "missingTagVisits": 1,
            "oosGapVisits": 0,
            "posLiftPct": 22.8,
            "demoCost": 880.0,
            "costPerSample": 2.47,
            "stockoutSignal": "",
            "competitorPromo": "Spindrift 8-pack featured in weekly circular",
            "latestDate": _d(8),
            "topSku": "Yuzu Ginger 12oz",
            "quotes": [
                "Great taste feedback, though shelf tag was missing on bottom row so several asked the price.",
            ],
        },
        {
            "storeKey": "sandbox:wfm-brooklyn-williamsburg",
            "storeName": "Whole Foods Market — Williamsburg",
            "retailerBanner": "Whole Foods Market",
            "market": "New York, NY",
            "stateCode": "NY",
            "address": "238 Bedford Ave, Brooklyn, NY 11249",
            "visits": 5,
            "consumersSampled": 498,
            "unitsSold": 154,
            "conversionPct": 30.9,
            "osaRatePct": 100.0,
            "latestStatus": "on_shelf",
            "latestFacings": 5,
            "avgFacings": 4.6,
            "latestPrice": "2.59",
            "missingTagVisits": 0,
            "oosGapVisits": 0,
            "posLiftPct": 29.6,
            "demoCost": 1320.0,
            "costPerSample": 2.65,
            "stockoutSignal": "",
            "competitorPromo": "Olipop sampling team scheduled next weekend",
            "latestDate": _d(2),
            "topSku": "Blood Orange Hibiscus 12oz",
            "quotes": [
                "Weekend brunch crowd loved Blood Orange Hibiscus as a mocktail mixer.",
            ],
        },
        {
            "storeKey": "sandbox:central-market-dallas",
            "storeName": "Central Market — Lovers Lane",
            "retailerBanner": "Central Market",
            "market": "Dallas, TX",
            "stateCode": "TX",
            "address": "5750 E Lovers Ln, Dallas, TX 75206",
            "visits": 4,
            "consumersSampled": 364,
            "unitsSold": 112,
            "conversionPct": 30.8,
            "osaRatePct": 100.0,
            "latestStatus": "on_shelf",
            "latestFacings": 4,
            "avgFacings": 4.0,
            "latestPrice": "2.49",
            "missingTagVisits": 0,
            "oosGapVisits": 0,
            "posLiftPct": 26.5,
            "demoCost": 900.0,
            "costPerSample": 2.47,
            "stockoutSignal": "",
            "competitorPromo": "",
            "latestDate": _d(11),
            "topSku": "Meyer Lemon Rosemary 12oz",
            "quotes": [
                "Culinary shoppers paired Meyer Lemon Rosemary with cheese & charcuterie.",
            ],
        },
        {
            "storeKey": "sandbox:wfm-chicago-lincolnpark",
            "storeName": "Whole Foods Market — Lincoln Park",
            "retailerBanner": "Whole Foods Market",
            "market": "Chicago, IL",
            "stateCode": "IL",
            "address": "1550 N Kingsbury St, Chicago, IL 60642",
            "visits": 5,
            "consumersSampled": 382,
            "unitsSold": 88,
            "conversionPct": 23.0,
            "osaRatePct": 80.0,
            "latestStatus": "on_shelf",
            "latestFacings": 2,
            "avgFacings": 2.2,
            "latestPrice": "2.19",
            "missingTagVisits": 1,
            "oosGapVisits": 1,
            "posLiftPct": 18.2,
            "demoCost": 1100.0,
            "costPerSample": 2.88,
            "stockoutSignal": "Endcap slot down to 2 facings; missing promo tag.",
            "competitorPromo": "Poppi 4-pack $7.99 feature at front checkout",
            "latestDate": _d(9),
            "topSku": "Yuzu Ginger 12oz",
            "quotes": [
                "Shoppers liked the crisp bite and lower sugar vs kombucha.",
            ],
        },
        {
            "storeKey": "sandbox:sprouts-la-culver",
            "storeName": "Sprouts Farmers Market — Culver City",
            "retailerBanner": "Sprouts",
            "market": "Los Angeles, CA",
            "stateCode": "CA",
            "address": "5660 Sepulveda Blvd, Culver City, CA 90230",
            "visits": 4,
            "consumersSampled": 265,
            "unitsSold": 58,
            "conversionPct": 21.9,
            "osaRatePct": 75.0,
            "latestStatus": "not_found",
            "latestFacings": 1,
            "avgFacings": 1.8,
            "latestPrice": "2.49",
            "missingTagVisits": 1,
            "oosGapVisits": 1,
            "posLiftPct": 16.4,
            "demoCost": 880.0,
            "costPerSample": 3.32,
            "stockoutSignal": "Only 1 warm can left on shelf during Saturday demo.",
            "competitorPromo": "Spindrift lime demo same day",
            "latestDate": _d(14),
            "topSku": "Blood Orange Hibiscus 12oz",
            "quotes": [
                "High trial interest, but low shelf inventory capped basket conversion.",
            ],
        },
    ]

    shelf_photos = [
        {
            "fileId": "sb-p1",
            "storeKey": "sandbox:wfm-austin-lamar",
            "storeName": "Whole Foods Market — Lamar Flagship",
            "market": "Austin, TX",
            "stateCode": "TX",
            "eventDate": _d(3),
            "bucketName": "Cold Box Shelf",
            "productOnShelf": "yes",
            "facings": 6,
            "priceTagVisible": "yes",
            "price": "2.49",
            "outOfStockGap": "no",
            "photoUrl": _svg_data_uri(
                title="Whole Foods Lamar · Cold Box",
                subtitle="6 front facings · clean eye-level block · $2.49 verified",
                badge="ON SHELF · 6 FACINGS",
                accent="#3b6e4c",
                bg="#f6f6f2",
                cans=6,
                price_tag="$2.49",
            ),
        },
        {
            "fileId": "sb-p2",
            "storeKey": "sandbox:wfm-dallas-parklane",
            "storeName": "Whole Foods Market — Park Lane",
            "market": "Dallas, TX",
            "stateCode": "TX",
            "eventDate": _d(6),
            "bucketName": "Ambient Shelf Audit",
            "productOnShelf": "no",
            "facings": 0,
            "priceTagVisible": "no",
            "price": "2.89",
            "outOfStockGap": "yes",
            "photoUrl": _svg_data_uri(
                title="Whole Foods Park Lane · Shelf Gap",
                subtitle="OOS gap detected · missing shelf tag · +16% price drift",
                badge="OOS GAP · MISSING TAG",
                accent="#c2410c",
                bg="#fff8f5",
                cans=4,
                gap_index=2,
                price_tag=None,
            ),
        },
        {
            "fileId": "sb-p3",
            "storeKey": "sandbox:heb-houston-montrose",
            "storeName": "H-E-B #725 — Montrose",
            "market": "Houston, TX",
            "stateCode": "TX",
            "eventDate": _d(42),
            "bucketName": "Shelf Before Restock",
            "productOnShelf": "yes",
            "facings": 2,
            "priceTagVisible": "yes",
            "price": "2.79",
            "outOfStockGap": "yes",
            "photoUrl": _svg_data_uri(
                title="H-E-B #725 Montrose · Earlier Visit",
                subtitle="2 facings with 1 OOS slot before distributor restock",
                badge="BEFORE · 2 FACINGS",
                accent="#8b5e34",
                bg="#faf8f3",
                cans=3,
                gap_index=1,
                price_tag="$2.79",
            ),
        },
        {
            "fileId": "sb-p4",
            "storeKey": "sandbox:heb-houston-montrose",
            "storeName": "H-E-B #725 — Montrose",
            "market": "Houston, TX",
            "stateCode": "TX",
            "eventDate": _d(5),
            "bucketName": "Shelf After Restock",
            "productOnShelf": "yes",
            "facings": 4,
            "priceTagVisible": "yes",
            "price": "2.79",
            "outOfStockGap": "no",
            "photoUrl": _svg_data_uri(
                title="H-E-B #725 Montrose · Latest Visit",
                subtitle="Expanded to 4 facings post-restock · +31.2% POS lift",
                badge="AFTER · 4 FACINGS",
                accent="#2d6a4f",
                bg="#f4f8f5",
                cans=4,
                price_tag="$2.79",
            ),
        },
        {
            "fileId": "sb-p5",
            "storeKey": "sandbox:erewhon-santa-monica",
            "storeName": "Erewhon — Santa Monica",
            "market": "Los Angeles, CA",
            "stateCode": "CA",
            "eventDate": _d(4),
            "bucketName": "Sampling Table & Cooler",
            "productOnShelf": "yes",
            "facings": 5,
            "priceTagVisible": "yes",
            "price": "2.99",
            "outOfStockGap": "no",
            "photoUrl": _svg_data_uri(
                title="Erewhon Santa Monica · Chilled Set",
                subtitle="5 facings · 37.8% demo conversion · $2.99 premium retail",
                badge="TOP DOOR · 37.8% CONV",
                accent="#1d4e89",
                bg="#f4f7fa",
                cans=5,
                price_tag="$2.99",
            ),
        },
        {
            "fileId": "sb-p6",
            "storeKey": "sandbox:sprouts-denver-colfax",
            "storeName": "Sprouts Farmers Market — Colfax",
            "market": "Denver, CO",
            "stateCode": "CO",
            "eventDate": _d(8),
            "bucketName": "Shelf Tag Audit",
            "productOnShelf": "yes",
            "facings": 2,
            "priceTagVisible": "no",
            "price": "2.49",
            "outOfStockGap": "no",
            "photoUrl": _svg_data_uri(
                title="Sprouts Colfax · Missing Tag Flag",
                subtitle="2 facings on shelf · price tag missing on lower tier",
                badge="MISSING TAG · 2 FACINGS",
                accent="#b45309",
                bg="#fffbf2",
                cans=2,
                price_tag=None,
            ),
        },
        {
            "fileId": "sb-p7",
            "storeKey": "sandbox:wfm-austin-lamar",
            "storeName": "Whole Foods Market — Lamar Flagship",
            "market": "Austin, TX",
            "stateCode": "TX",
            "eventDate": _d(54),
            "bucketName": "Initial Shelf Audit",
            "productOnShelf": "yes",
            "facings": 3,
            "priceTagVisible": "yes",
            "price": "2.49",
            "outOfStockGap": "no",
            "photoUrl": _svg_data_uri(
                title="Whole Foods Lamar · Initial Audit",
                subtitle="Started with 3 facings before Q4 velocity pitch",
                badge="BEFORE · 3 FACINGS",
                accent="#5c7a67",
                bg="#f6f6f2",
                cans=3,
                price_tag="$2.49",
            ),
        },
        {
            "fileId": "sb-p8",
            "storeKey": "sandbox:wfm-brooklyn-williamsburg",
            "storeName": "Whole Foods Market — Williamsburg",
            "market": "New York, NY",
            "stateCode": "NY",
            "eventDate": _d(2),
            "bucketName": "Endcap Display",
            "productOnShelf": "yes",
            "facings": 5,
            "priceTagVisible": "yes",
            "price": "2.59",
            "outOfStockGap": "no",
            "photoUrl": _svg_data_uri(
                title="Whole Foods Williamsburg · Endcap",
                subtitle="5 facings · Blood Orange Hibiscus lead SKU",
                badge="ON SHELF · 5 FACINGS",
                accent="#9d174d",
                bg="#fdf5f8",
                cans=5,
                price_tag="$2.59",
            ),
        },
    ]

    before_after_pairs = [
        {
            "storeKey": "sandbox:wfm-austin-lamar",
            "storeName": "Whole Foods Market — Lamar Flagship",
            "market": "Austin, TX",
            "stateCode": "TX",
            "photoCount": 2,
            "beforePhoto": shelf_photos[6],
            "afterPhoto": shelf_photos[0],
        },
        {
            "storeKey": "sandbox:heb-houston-montrose",
            "storeName": "H-E-B #725 — Montrose",
            "market": "Houston, TX",
            "stateCode": "TX",
            "photoCount": 2,
            "beforePhoto": shelf_photos[2],
            "afterPhoto": shelf_photos[3],
        },
    ]

    competitor_markets = [
        {
            "market": "Houston, TX",
            "stateCode": "TX",
            "competitorBrand": "Poppi",
            "mentionCount": 9,
            "priorMentionCount": 4,
            "delta": 5,
            "trend": "rising",
            "sampleQuote": "Several H-E-B shoppers noted Poppi was 2/$4 nearby, but preferred Solstice's zero-stevia citrus finish.",
            "sampleStoreKey": "sandbox:heb-houston-montrose",
            "sampleStoreName": "H-E-B #725 — Montrose",
        },
        {
            "market": "Austin, TX",
            "stateCode": "TX",
            "competitorBrand": "Olipop",
            "mentionCount": 7,
            "priorMentionCount": 5,
            "delta": 2,
            "trend": "rising",
            "sampleQuote": "Shoppers compared fiber claims to Olipop endcap; Yuzu Ginger won taste tests 3-to-1.",
            "sampleStoreKey": "sandbox:wfm-austin-lamar",
            "sampleStoreName": "Whole Foods Market — Lamar Flagship",
        },
        {
            "market": "Dallas, TX",
            "stateCode": "TX",
            "competitorBrand": "Liquid Death",
            "mentionCount": 5,
            "priorMentionCount": 0,
            "delta": 5,
            "trend": "new",
            "sampleQuote": "New sparkling tea display at entrance drew curiosity; shoppers asked how Solstice compares on calories.",
            "sampleStoreKey": "sandbox:wfm-dallas-parklane",
            "sampleStoreName": "Whole Foods Market — Park Lane",
        },
        {
            "market": "Denver, CO",
            "stateCode": "CO",
            "competitorBrand": "Spindrift",
            "mentionCount": 6,
            "priorMentionCount": 6,
            "delta": 0,
            "trend": "steady",
            "sampleQuote": "Loyal Spindrift drinkers loved the botanical complexity of Meyer Lemon Rosemary.",
            "sampleStoreKey": "sandbox:sprouts-denver-colfax",
            "sampleStoreName": "Sprouts Farmers Market — Colfax",
        },
        {
            "market": "Los Angeles, CA",
            "stateCode": "CA",
            "competitorBrand": "Ghia",
            "mentionCount": 3,
            "priorMentionCount": 5,
            "delta": -2,
            "trend": "cooling",
            "sampleQuote": "Erewhon guests called Blood Orange Hibiscus an everyday $2.99 alternative to Ghia.",
            "sampleStoreKey": "sandbox:erewhon-santa-monica",
            "sampleStoreName": "Erewhon — Santa Monica",
        },
    ]

    competitor_summary = [
        {"brand": "Poppi", "prefersOurs": 7, "onlyMentioned": 5, "prefersTheirs": 2},
        {"brand": "Olipop", "prefersOurs": 6, "onlyMentioned": 4, "prefersTheirs": 1},
        {"brand": "Spindrift", "prefersOurs": 5, "onlyMentioned": 4, "prefersTheirs": 1},
        {"brand": "Liquid Death", "prefersOurs": 3, "onlyMentioned": 2, "prefersTheirs": 0},
        {"brand": "Ghia", "prefersOurs": 2, "onlyMentioned": 1, "prefersTheirs": 0},
    ]

    footprint = [
        {"stateCode": "TX", "activations": 19, "consumers": 1894, "unitsSold": 630, "stores": 4},
        {"stateCode": "CA", "activations": 9, "consumers": 710, "unitsSold": 226, "stores": 2},
        {"stateCode": "NY", "activations": 5, "consumers": 498, "unitsSold": 154, "stores": 1},
        {"stateCode": "IL", "activations": 5, "consumers": 382, "unitsSold": 88, "stores": 1},
        {"stateCode": "CO", "activations": 4, "consumers": 356, "unitsSold": 98, "stores": 1},
    ]

    saved_views = [
        {
            "id": "sv-tx-wfm",
            "name": "Texas Whole Foods · Last 90d",
            "retailerFilter": "Whole Foods Market",
            "marketFilter": "TX",
            "schedule": "Fridays · CSV + Summary → regional-tx@solsticebev.example",
        },
        {
            "id": "sv-oos-alerts",
            "name": "Price Drift & Missing Tags",
            "retailerFilter": "",
            "marketFilter": "",
            "storeFilter": "attention",
            "schedule": "Mondays · Ops digest → field-ops@solsticebev.example",
        },
        {
            "id": "sv-ca-premium",
            "name": "SoCal Natural Doors",
            "retailerFilter": "",
            "marketFilter": "CA",
            "schedule": "Bi-weekly Fridays → west-broker@solsticebev.example",
        },
    ]

    return {
        "brand": {
            "name": "Solstice Botanical Sparkling",
            "tagline": "Botanical Sparkling Water · Zero Stevia · 12oz Cans",
            "category": "Better-for-You Beverage",
            "targetMsrp": "2.49",
            "skus": [
                "Yuzu Ginger 12oz",
                "Blood Orange Hibiscus 12oz",
                "Meyer Lemon Rosemary 12oz",
            ],
            "periodStart": start_90.isoformat(),
            "periodEnd": ref.isoformat(),
        },
        "kpis": {
            "activations": 42,
            "activeDoors": len(stores),
            "markets": 6,
            "consumersSampled": 3840,
            "unitsSold": 1196,
            "conversionPct": 31.1,
            "avgCostPerSample": 2.42,
            "totalDemoSpend": 9305.0,
            "posLiftPct": 28.4,
            "osaRatePct": 89.2,
            "priceTagRatePct": 83.3,
            "avgFacings": 3.5,
            "medianPrice": "2.49",
            "oosGapsCount": 5,
            "missingTagCount": 5,
        },
        "stores": stores,
        "shelfPhotos": shelf_photos,
        "beforeAfterPairs": before_after_pairs,
        "competitorMarkets": competitor_markets,
        "competitorSummary": competitor_summary,
        "footprint": footprint,
        "savedViews": saved_views,
        "shopperThemes": [
            {
                "theme": "Crisp citrus without stevia aftertaste",
                "mentions": 64,
                "quote": "Finally a botanical sparkling that isn't cloyingly sweet — grabbing a 4-pack of Yuzu Ginger.",
            },
            {
                "theme": "Evening mocktail & aperitif occasion",
                "mentions": 41,
                "quote": "Blood Orange Hibiscus tastes like a craft spritz over ice for weeknights.",
            },
            {
                "theme": "Cold-box availability drives impulse trial",
                "mentions": 29,
                "quote": "Doors with chilled singles converted 11.4 points higher than ambient-only shelves.",
            },
        ],
    }
