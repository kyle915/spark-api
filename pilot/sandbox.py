"""Admin-only Sample Brand Sandbox read-only payload.

Serves a completely fictional, self-contained CPG beverage brand workspace
("Meadowlark Botanicals") with made-up retailer banners, store names,
addresses, and competitor brands over ``GET /api/public/sandbox-brand``
(restricted to authenticated Ignite admins for internal preview until a
future public launch).
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
                f'font-family="system-ui,sans-serif" font-size="8.5" font-weight="700" fill="#ffffff">MEADOWLARK</text>'
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
        f'<text x="48" y="314" font-family="ui-monospace,monospace" font-size="10" fill="#7c8270">SPARK VERIFIED FIELD PROOF · ADMIN SANDBOX PREVIEW</text>'
        f"</svg>"
    )
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


def build_sandbox_sample_brand(*, today: date | None = None) -> dict[str, Any]:
    """Return the complete, deterministic read-only Sample Brand Sandbox payload.

    Every brand, competitor, retailer banner, store name, and street address
    is 100% fictional.
    """
    ref = today or date.today()
    start_90 = ref - timedelta(days=89)

    def _d(days_ago: int) -> str:
        return (ref - timedelta(days=days_ago)).isoformat()

    stores: list[dict[str, Any]] = [
        {
            "storeKey": "sandbox:greenway-austin-barton",
            "storeName": "Greenway Market — Barton Creek #104",
            "retailerBanner": "Greenway Market",
            "market": "Austin, TX",
            "stateCode": "TX",
            "address": "410 Meadowbrook Pkwy, Austin, TX 78704",
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
            "competitorPromo": "Bramble Pop endcap BOGO on aisle 4",
            "latestDate": _d(3),
            "topSku": "Yuzu Ginger 12oz",
            "quotes": [
                "Shoppers kept comparing the yuzu finish to CitrusCloud — 4-packs moved fastest after tasting.",
                "Category buyer walked by at 2pm and asked to add Blood Orange Hibiscus to the cold grab-and-go set.",
            ],
        },
        {
            "storeKey": "sandbox:sunbelt-houston-oakridge",
            "storeName": "Sunbelt Grocers #218 — Oakridge",
            "retailerBanner": "Sunbelt Grocers",
            "market": "Houston, TX",
            "stateCode": "TX",
            "address": "1840 Cypress Ridge Rd, Houston, TX 77007",
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
            "competitorPromo": "Zestify Soda 2/$4 TPR running all month",
            "latestDate": _d(5),
            "topSku": "Yuzu Ginger 12oz",
            "quotes": [
                "Customers loved the zero-stevia ingredient panel; several picked up two 4-packs.",
            ],
        },
        {
            "storeKey": "sandbox:greenway-dallas-northgate",
            "storeName": "Greenway Market — Northgate #119",
            "retailerBanner": "Greenway Market",
            "market": "Dallas, TX",
            "stateCode": "TX",
            "address": "7220 Briarwood Ln, Dallas, TX 75214",
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
            "competitorPromo": "PeakWave Tea sparkling display at front entrance",
            "latestDate": _d(6),
            "topSku": "Blood Orange Hibiscus 12oz",
            "quotes": [
                "Shoppers tried samples at the table then asked where to grab chilled singles — cooler was empty.",
            ],
        },
        {
            "storeKey": "sandbox:alder-la-westside",
            "storeName": "Alder & Pine Provisions — Westside",
            "retailerBanner": "Alder & Pine Provisions",
            "market": "Los Angeles, CA",
            "stateCode": "CA",
            "address": "2450 Oceanview Terrace, Los Angeles, CA 90401",
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
                "Multiple repeat buyers noted they spotted Meadowlark online and grabbed 6 singles.",
            ],
        },
        {
            "storeKey": "sandbox:harvest-denver-parkview",
            "storeName": "Harvest Table Co-op — Parkview",
            "retailerBanner": "Harvest Table Co-op",
            "market": "Denver, CO",
            "stateCode": "CO",
            "address": "3110 Highline Ave, Denver, CO 80209",
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
            "competitorPromo": "CitrusCloud 8-pack featured in weekly circular",
            "latestDate": _d(8),
            "topSku": "Yuzu Ginger 12oz",
            "quotes": [
                "Great taste feedback, though shelf tag was missing on bottom row so several asked the price.",
            ],
        },
        {
            "storeKey": "sandbox:greenway-nyc-riverfront",
            "storeName": "Greenway Market — Riverfront #302",
            "retailerBanner": "Greenway Market",
            "market": "New York, NY",
            "stateCode": "NY",
            "address": "290 Harborview Way, New York, NY 10013",
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
            "competitorPromo": "Bramble Pop sampling team scheduled next weekend",
            "latestDate": _d(2),
            "topSku": "Blood Orange Hibiscus 12oz",
            "quotes": [
                "Weekend brunch crowd loved Blood Orange Hibiscus as a mocktail mixer.",
            ],
        },
        {
            "storeKey": "sandbox:copperleaf-dallas-lakewood",
            "storeName": "Copperleaf Grocer — Lakewood",
            "retailerBanner": "Copperleaf Grocer",
            "market": "Dallas, TX",
            "stateCode": "TX",
            "address": "5400 Cedar Crest Blvd, Dallas, TX 75206",
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
            "storeKey": "sandbox:greenway-chicago-lakeshore",
            "storeName": "Greenway Market — Lakeshore #208",
            "retailerBanner": "Greenway Market",
            "market": "Chicago, IL",
            "stateCode": "IL",
            "address": "1620 Northwind Ave, Chicago, IL 60614",
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
            "competitorPromo": "Zestify Soda 4-pack $7.99 feature at front checkout",
            "latestDate": _d(9),
            "topSku": "Yuzu Ginger 12oz",
            "quotes": [
                "Shoppers liked the crisp bite and lower sugar vs kombucha.",
            ],
        },
        {
            "storeKey": "sandbox:harvest-la-palms",
            "storeName": "Harvest Table Co-op — Palms",
            "retailerBanner": "Harvest Table Co-op",
            "market": "Los Angeles, CA",
            "stateCode": "CA",
            "address": "5180 Sunnyside Blvd, Los Angeles, CA 90034",
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
            "competitorPromo": "CitrusCloud lime demo same day",
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
            "storeKey": "sandbox:greenway-austin-barton",
            "storeName": "Greenway Market — Barton Creek #104",
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
                title="Greenway Barton Creek · Cold Box",
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
            "storeKey": "sandbox:greenway-dallas-northgate",
            "storeName": "Greenway Market — Northgate #119",
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
                title="Greenway Northgate · Shelf Gap",
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
            "storeKey": "sandbox:sunbelt-houston-oakridge",
            "storeName": "Sunbelt Grocers #218 — Oakridge",
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
                title="Sunbelt #218 Oakridge · Earlier Visit",
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
            "storeKey": "sandbox:sunbelt-houston-oakridge",
            "storeName": "Sunbelt Grocers #218 — Oakridge",
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
                title="Sunbelt #218 Oakridge · Latest Visit",
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
            "storeKey": "sandbox:alder-la-westside",
            "storeName": "Alder & Pine Provisions — Westside",
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
                title="Alder & Pine Westside · Chilled Set",
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
            "storeKey": "sandbox:harvest-denver-parkview",
            "storeName": "Harvest Table Co-op — Parkview",
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
                title="Harvest Table Parkview · Missing Tag Flag",
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
            "storeKey": "sandbox:greenway-austin-barton",
            "storeName": "Greenway Market — Barton Creek #104",
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
                title="Greenway Barton Creek · Initial Audit",
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
            "storeKey": "sandbox:greenway-nyc-riverfront",
            "storeName": "Greenway Market — Riverfront #302",
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
                title="Greenway Riverfront · Endcap",
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
            "storeKey": "sandbox:greenway-austin-barton",
            "storeName": "Greenway Market — Barton Creek #104",
            "market": "Austin, TX",
            "stateCode": "TX",
            "photoCount": 2,
            "beforePhoto": shelf_photos[6],
            "afterPhoto": shelf_photos[0],
        },
        {
            "storeKey": "sandbox:sunbelt-houston-oakridge",
            "storeName": "Sunbelt Grocers #218 — Oakridge",
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
            "competitorBrand": "Zestify Soda",
            "mentionCount": 9,
            "priorMentionCount": 4,
            "delta": 5,
            "trend": "rising",
            "sampleQuote": "Several Sunbelt shoppers noted Zestify Soda was 2/$4 nearby, but preferred Meadowlark's zero-stevia citrus finish.",
            "sampleStoreKey": "sandbox:sunbelt-houston-oakridge",
            "sampleStoreName": "Sunbelt Grocers #218 — Oakridge",
        },
        {
            "market": "Austin, TX",
            "stateCode": "TX",
            "competitorBrand": "Bramble Pop",
            "mentionCount": 7,
            "priorMentionCount": 5,
            "delta": 2,
            "trend": "rising",
            "sampleQuote": "Shoppers compared fiber claims to the Bramble Pop endcap; Yuzu Ginger won taste tests 3-to-1.",
            "sampleStoreKey": "sandbox:greenway-austin-barton",
            "sampleStoreName": "Greenway Market — Barton Creek #104",
        },
        {
            "market": "Dallas, TX",
            "stateCode": "TX",
            "competitorBrand": "PeakWave Tea",
            "mentionCount": 5,
            "priorMentionCount": 0,
            "delta": 5,
            "trend": "new",
            "sampleQuote": "New PeakWave Tea display at entrance drew curiosity; shoppers asked how Meadowlark compares on calories.",
            "sampleStoreKey": "sandbox:greenway-dallas-northgate",
            "sampleStoreName": "Greenway Market — Northgate #119",
        },
        {
            "market": "Denver, CO",
            "stateCode": "CO",
            "competitorBrand": "CitrusCloud",
            "mentionCount": 6,
            "priorMentionCount": 6,
            "delta": 0,
            "trend": "steady",
            "sampleQuote": "Loyal CitrusCloud drinkers loved the botanical complexity of Meyer Lemon Rosemary.",
            "sampleStoreKey": "sandbox:harvest-denver-parkview",
            "sampleStoreName": "Harvest Table Co-op — Parkview",
        },
        {
            "market": "Los Angeles, CA",
            "stateCode": "CA",
            "competitorBrand": "Verbena Zero",
            "mentionCount": 3,
            "priorMentionCount": 5,
            "delta": -2,
            "trend": "cooling",
            "sampleQuote": "Alder & Pine guests called Blood Orange Hibiscus an everyday $2.99 alternative to Verbena Zero.",
            "sampleStoreKey": "sandbox:alder-la-westside",
            "sampleStoreName": "Alder & Pine Provisions — Westside",
        },
    ]

    competitor_summary = [
        {"brand": "Zestify Soda", "prefersOurs": 7, "onlyMentioned": 5, "prefersTheirs": 2},
        {"brand": "Bramble Pop", "prefersOurs": 6, "onlyMentioned": 4, "prefersTheirs": 1},
        {"brand": "CitrusCloud", "prefersOurs": 5, "onlyMentioned": 4, "prefersTheirs": 1},
        {"brand": "PeakWave Tea", "prefersOurs": 3, "onlyMentioned": 2, "prefersTheirs": 0},
        {"brand": "Verbena Zero", "prefersOurs": 2, "onlyMentioned": 1, "prefersTheirs": 0},
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
            "id": "sv-tx-greenway",
            "name": "Texas Greenway · Last 90d",
            "retailerFilter": "Greenway Market",
            "marketFilter": "TX",
            "schedule": "Fridays · CSV + Summary → regional-tx@meadowlarkbev.example",
        },
        {
            "id": "sv-oos-alerts",
            "name": "Price Drift & Missing Tags",
            "retailerFilter": "",
            "marketFilter": "",
            "storeFilter": "attention",
            "schedule": "Mondays · Ops digest → field-ops@meadowlarkbev.example",
        },
        {
            "id": "sv-ca-premium",
            "name": "West Coast Natural Doors",
            "retailerFilter": "",
            "marketFilter": "CA",
            "schedule": "Bi-weekly Fridays → west-broker@meadowlarkbev.example",
        },
    ]

    return {
        "brand": {
            "name": "Meadowlark Botanicals",
            "tagline": "Botanical Sparkling Tonic · Zero Stevia · 12oz Cans",
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
