"""Scenario data for ``seed_brand_group_demo`` — the same illustrative
activations as the FAMUS / Alcohol Armor / Wook Water sample recap PDFs.

Every number is fixed: re-seeding writes the same recaps, and per-SKU samples
always add up to the brand's consumers sampled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

FM, AA, WW = "famus-energy", "alcohol-armor", "wook-water"
BRAND_SLUGS = (FM, AA, WW)
RETAIL, EVENT = "Retail Sampling", "Event Activation"

KNEW_ABOUT = "knew about"


@dataclass(frozen=True)
class BrandResult:
    brand: str
    results: dict[str, str]
    feedback: dict[str, str]
    skus: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Scenario:
    key: str
    program: str
    host: str
    title: str
    address: str
    coordinates: tuple[float, float]
    iana: str
    day: date
    hours: tuple[time, time]
    ba_first_name: str
    shared: dict[str, str]
    brands: tuple[BrandResult, ...]
    photos: tuple[tuple[str, int], ...]
    store: tuple[str, str] | None = None

    @property
    def brand_slugs(self) -> list[str]:
        return [b.brand for b in self.brands]

    @property
    def is_co_activation(self) -> bool:
        return len(self.brands) > 1


def retail_brand(
    brand,
    sampled,
    first,
    aware,
    willing,
    singles,
    packs,
    skus,
    feedback,
    quotes,
    decline,
) -> BrandResult:
    assert willing <= sampled and first <= sampled and aware <= sampled
    assert sum(q for _, q in skus) == sampled, brand
    return BrandResult(
        brand=brand,
        results={
            "Total number of consumers sampled": str(sampled),
            "First Time consumers?": str(first),
            KNEW_ABOUT: str(aware),
            "How many consumers would be willing to purchase the product after tasting it?": str(
                willing
            ),
            "How many consumers would NOT be willing to purchase the product after tasting it?": str(
                sampled - willing
            ),
            "How many single cans / bottles / shots did consumers purchase?": str(
                singles
            ),
            "How many packs did consumers purchase?": str(packs),
        },
        feedback={
            "Consumer Feedback": feedback,
            "Quotes from Consumers": quotes,
            "Reasons to decline to purchase?": decline,
        },
        skus=tuple(skus),
    )


def event_brand(brand, sampled, aware, skus, feedback, quotes) -> BrandResult:
    assert aware <= sampled
    assert sum(q for _, q in skus) == sampled, brand
    return BrandResult(
        brand=brand,
        results={
            "Total number of consumers sampled": str(sampled),
            KNEW_ABOUT: str(aware),
        },
        feedback={"Consumer Feedback": feedback, "Quotes from Consumers": quotes},
        skus=tuple(skus),
    )


RETAIL_PHOTOS = (
    ("Table Set Up", 2),
    ("Product Display", 2),
    ("Consumer Sampling Pictures", 3),
    ("Product Receipt", 1),
)
EVENT_PHOTOS = (
    ("Activation Setup", 2),
    ("Consumer Sampling Pictures", 4),
    ("Expense Receipts", 1),
)

FAMUS_SKUS = ("OG Luxe (Tropical Ice) 16oz", "Havana Nights (Mint & Lime) 16oz")
AA_SKUS = ("Slayberry 2oz Shot", "Boujee Berry 2oz Shot")
WW_SKUS = ("Pineapple Peak 12oz Can", "Laser Lime 12oz Can", "Berry Groovy 12oz Can")

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="famus-retail",
        program=RETAIL,
        host=FM,
        title="Kroger #421",
        store=("Kroger", "421"),
        address="1700 Monroe Dr NE, Atlanta, GA 30324",
        coordinates=(33.7987, -84.3685),
        iana="America/New_York",
        day=date(2026, 10, 10),
        hours=(time(13, 0), time(17, 0)),
        ba_first_name="Jordan",
        shared={
            "Estimated store foot traffic": "650",
            "Sample format": "Sample cup / pour",
            "Account Feedback": (
                "Store manager (Dana) liked the movement and asked for a second facing "
                "of OG Luxe next to the Celsius set; the cooler was down to three cans by 4pm."
            ),
            "Anything you'd change or do differently?": (
                "Set up by the front registers instead of the energy aisle end cap; "
                "most shoppers were in and out on a quick lunch run."
            ),
            "Demographics (general age, sex, ethnicities of consumers)": (
                "Mostly 21-40, slightly more men than women. Saturday afternoon mix of "
                "gym-goers, college students, and parents grabbing a pick-me-up."
            ),
        },
        brands=(
            retail_brand(
                FM,
                sampled=72,
                first=51,
                aware=14,
                willing=44,
                singles=11,
                packs=0,
                skus=[(FAMUS_SKUS[0], 40), (FAMUS_SKUS[1], 32)],
                feedback=(
                    'OG Luxe won on taste: people called it smooth and "not syrupy" for an '
                    "energy drink. Havana Nights surprised people who expected mint to be "
                    "too strong; the lime keeps it light."
                ),
                quotes=(
                    '"Tastes like a vacation, not a lab." (male, ~28)  '
                    '"Finally an energy drink that doesn\'t taste like candy." (female, ~34)'
                ),
                decline=(
                    "Several were loyal to their usual brand on sale; a few only drink "
                    "sugar-free and wanted to check the label first."
                ),
            ),
        ),
        photos=RETAIL_PHOTOS,
    ),
    Scenario(
        key="famus-event",
        program=EVENT,
        host=FM,
        title="Ponce City Market Fall Night Market",
        address="675 Ponce De Leon Ave NE, Atlanta, GA 30308",
        coordinates=(33.7726, -84.3655),
        iana="America/New_York",
        day=date(2026, 10, 3),
        hours=(time(17, 0), time(22, 0)),
        ba_first_name="Avery",
        shared={
            "Event name / venue": "Fall Night Market · Ponce City Market (Central Food Hall plaza)",
            "Estimated attendance / foot traffic": "3500",
            "Sample format": "Sample cup / pour",
            "Key highlight (1–2 sentences) / What worked well & What didn't?": (
                "Chilled 2oz pours by the DJ booth kept a line going all night; the gold "
                "cans photographed well and drove a lot of story posts. Next time bring a "
                "second cooler — OG Luxe ran warm by 9pm."
            ),
            "Demographics (general age, sex, ethnicities of consumers)": (
                "Mostly 21-35, slight male skew after 8pm. Diverse young professional "
                "crowd, lots of friend groups and couples."
            ),
        },
        brands=(
            event_brand(
                FM,
                sampled=420,
                aware=63,
                skus=[(FAMUS_SKUS[0], 230), (FAMUS_SKUS[1], 190)],
                feedback=(
                    "OG Luxe was the easy first taste; Havana Nights got the \"wait, that's "
                    "good\" reaction from people who don't usually like mint. Lots of "
                    "questions about caffeine (200mg) and where to buy."
                ),
                quotes=(
                    '"This could be a mocktail." (female, ~27)  '
                    '"Where do I get the mint one?" (male, ~31)'
                ),
            ),
        ),
        photos=EVENT_PHOTOS,
    ),
    Scenario(
        key="alcohol-armor-retail",
        program=RETAIL,
        host=AA,
        title="Spec's Wines, Spirits & Finer Foods #47",
        store=("Spec's Wines, Spirits & Finer Foods", "47"),
        address="2410 Smith St, Houston, TX 77006",
        coordinates=(29.7499, -95.3797),
        iana="America/Chicago",
        day=date(2026, 10, 9),
        hours=(time(16, 0), time(20, 0)),
        ba_first_name="Maya",
        shared={
            "Estimated store foot traffic": "420",
            "Sample format": "Sample cup / pour",
            "Account Feedback": (
                "Front-end lead asked to keep the 2oz shots at the register as an impulse "
                "add-on; she wants a counter display for the Slayberry shots."
            ),
            "Anything you'd change or do differently?": (
                "Bring a small sign that explains what Alcohol Armor is; most shoppers "
                "needed a 20-second explanation before trying it."
            ),
            "Demographics (general age, sex, ethnicities of consumers)": (
                "Mostly 25-45, even male/female split. Friday after-work crowd stocking up "
                "for the weekend; lots of couples and groups heading to parties."
            ),
        },
        brands=(
            retail_brand(
                AA,
                sampled=58,
                first=49,
                aware=9,
                willing=31,
                singles=12,
                packs=3,
                skus=[(AA_SKUS[0], 31), (AA_SKUS[1], 27)],
                feedback=(
                    "Slayberry was the favorite: bright berry flavor, easy to take as a shot. "
                    "Boujee Berry landed with people who wanted something less sweet. Most "
                    "interest came once people heard it's meant for before a night out."
                ),
                quotes=(
                    '"I\'m buying this for the bachelorette trip." (female, ~30)  '
                    '"Way better than I expected from a shot." (male, ~38)'
                ),
                decline=(
                    "Some wanted to read reviews first; a few said they rarely drink enough "
                    "to need it."
                ),
            ),
        ),
        photos=RETAIL_PHOTOS,
    ),
    Scenario(
        key="alcohol-armor-event",
        program=EVENT,
        host=AA,
        title="Rainey Street Fall Fest",
        address="70 Rainey St, Austin, TX 78701",
        coordinates=(30.2585, -97.7387),
        iana="America/Chicago",
        day=date(2026, 10, 4),
        hours=(time(15, 0), time(21, 0)),
        ba_first_name="Taylor",
        shared={
            "Event name / venue": "Rainey Street Fall Fest · Rainey St district",
            "Estimated attendance / foot traffic": "2800",
            "Sample format": "Full can / bottle",
            "Key highlight (1–2 sentences) / What worked well & What didn't?": (
                "Handing out full 2oz shots as people arrived for the evening worked "
                'best — the "take one before your first drink" pitch clicked. Afternoon '
                "heat slowed traffic before 5pm."
            ),
            "Demographics (general age, sex, ethnicities of consumers)": (
                "Mostly 21-35, even male/female split; bachelor/bachelorette groups and "
                "UT alumni. ID checked at the table; every shot went to 21+."
            ),
        },
        brands=(
            event_brand(
                AA,
                sampled=310,
                aware=41,
                skus=[(AA_SKUS[0], 168), (AA_SKUS[1], 142)],
                feedback=(
                    "Group buy-in was strong: one person tried it and the whole group took "
                    'a shot. Slayberry tasted "like a berry candy," Boujee Berry read as '
                    "more grown-up. Most common question: how it works with alcohol."
                ),
                quotes=(
                    '"Taking these on our Vegas trip." (female, ~29)  '
                    '"Tastes way better than the hangover stuff at the gas station." (male, ~33)'
                ),
            ),
        ),
        photos=EVENT_PHOTOS,
    ),
    Scenario(
        key="wook-water-retail",
        program=RETAIL,
        host=WW,
        title="Sprouts Farmers Market #212",
        store=("Sprouts Farmers Market", "212"),
        address="1111 S Washington St, Denver, CO 80210",
        coordinates=(39.6966, -104.9792),
        iana="America/Denver",
        day=date(2026, 10, 11),
        hours=(time(11, 0), time(15, 0)),
        ba_first_name="Chris",
        shared={
            "Estimated store foot traffic": "780",
            "Sample format": "Sample cup / pour",
            "Account Feedback": (
                "Grocery manager moved the 4-packs to an end cap after lunch when the shelf "
                "facing ran low; asked about a repeat demo before the holidays."
            ),
            "Anything you'd change or do differently?": (
                "Bring more ice; the cans warmed up fast by the produce entrance."
            ),
            "Demographics (general age, sex, ethnicities of consumers)": (
                "Mostly 25-45, slightly more women than men. Sunday health-food shoppers, "
                "runners after the morning trail loop, and families."
            ),
        },
        brands=(
            retail_brand(
                WW,
                sampled=94,
                first=70,
                aware=18,
                willing=61,
                singles=14,
                packs=9,
                skus=[(WW_SKUS[0], 36), (WW_SKUS[1], 31), (WW_SKUS[2], 27)],
                feedback=(
                    'Pineapple Peak was the crowd favorite: "like a tropical sports drink '
                    'without the sugar crash." Laser Lime won the runners; Berry Groovy '
                    "was the pick for kids' lunchboxes (parents tasted first)."
                ),
                quotes=(
                    '"This is what I want after a hike." (female, ~32)  '
                    '"Berry Groovy tastes like a smoothie." (male, ~40)'
                ),
                decline=(
                    "A few already had a hydration mix at home; some wanted single cans "
                    "only to try before committing to a 4-pack."
                ),
            ),
        ),
        photos=RETAIL_PHOTOS,
    ),
    Scenario(
        key="wook-water-event",
        program=EVENT,
        host=WW,
        title="Yoga on the Rocks",
        address="18300 W Alameda Pkwy, Morrison, CO 80465",
        coordinates=(39.6654, -105.2057),
        iana="America/Denver",
        day=date(2026, 10, 5),
        hours=(time(7, 0), time(10, 0)),
        ba_first_name="Sam",
        shared={
            "Event name / venue": "Yoga on the Rocks · Red Rocks Amphitheatre (lower lot)",
            "Estimated attendance / foot traffic": "2000",
            "Sample format": "Full can / bottle",
            "Key highlight (1–2 sentences) / What worked well & What didn't?": (
                "Cold full cans right as class let out was perfect timing — the table "
                "cleared 540 cans in 90 minutes. Parking-lot placement meant we missed "
                "people who left by the upper exit."
            ),
            "Demographics (general age, sex, ethnicities of consumers)": (
                "Mostly 22-45, about 65% women. Active, health-minded crowd; lots of "
                "friend groups and couples."
            ),
        },
        brands=(
            event_brand(
                WW,
                sampled=540,
                aware=96,
                skus=[(WW_SKUS[0], 200), (WW_SKUS[1], 180), (WW_SKUS[2], 160)],
                feedback=(
                    "Exactly the right product for the moment: people wanted something "
                    "hydrating after class. Laser Lime was the favorite with runners; "
                    "Pineapple Peak won overall. Many asked if Sprouts carries it."
                ),
                quotes=(
                    '"I need this after every class." (female, ~30)  '
                    '"Berry Groovy is dangerously good." (male, ~36)'
                ),
            ),
        ),
        photos=EVENT_PHOTOS,
    ),
    Scenario(
        key="coact-retail",
        program=RETAIL,
        host=WW,
        title="Publix #1543",
        store=("Publix", "1543"),
        address="2345 Peachtree Rd NE, Atlanta, GA 30305",
        coordinates=(33.8197, -84.3871),
        iana="America/New_York",
        day=date(2026, 10, 12),
        hours=(time(12, 0), time(16, 0)),
        ba_first_name="Jamie",
        shared={
            "Estimated store foot traffic": "800",
            "Sample format": "Sample cup / pour",
            "Account Feedback": (
                'Assistant manager liked the two-brand table — one stop for "hydrate and '
                'energize." Asked us to come back on a Saturday morning.'
            ),
            "Anything you'd change or do differently?": (
                "Split the table into two clear halves with each brand's signage; a few "
                "shoppers thought FAMUS was a Wook Water flavor."
            ),
            "Demographics (general age, sex, ethnicities of consumers)": (
                "Mostly 25-45, slightly more women than men. Weekday lunch shoppers, "
                "nurses from the nearby hospital, and remote workers."
            ),
        },
        brands=(
            retail_brand(
                WW,
                sampled=66,
                first=48,
                aware=12,
                willing=41,
                singles=8,
                packs=6,
                skus=[(WW_SKUS[0], 24), (WW_SKUS[1], 22), (WW_SKUS[2], 20)],
                feedback=(
                    "Pineapple Peak led again; shoppers liked having a no-sugar hydration "
                    "option next to an energy drink. Laser Lime sold best in 4-packs."
                ),
                quotes='"I\'ll take the lime for my shift." (female, ~35, nurse)',
                decline="Some only wanted singles; a few were already holding a full basket.",
            ),
            retail_brand(
                FM,
                sampled=49,
                first=37,
                aware=10,
                willing=27,
                singles=9,
                packs=0,
                skus=[(FAMUS_SKUS[0], 27), (FAMUS_SKUS[1], 22)],
                feedback=(
                    "OG Luxe converted the afternoon-slump crowd; Havana Nights won over "
                    "shoppers who wanted something lighter and more citrusy."
                ),
                quotes='"Can I get the gold one to go?" (male, ~29)',
                decline="A few caffeine-sensitive shoppers passed; some only wanted to try, not buy.",
            ),
        ),
        photos=(
            ("Table Set Up", 2),
            ("Product Display", 1),
            ("Consumer Sampling Pictures", 2),
            ("Product Receipt", 1),
        ),
    ),
    Scenario(
        key="coact-event",
        program=EVENT,
        host=FM,
        title="Piedmont Park Fall Music Fest",
        address="400 Park Dr NE, Atlanta, GA 30306",
        coordinates=(33.7851, -84.3738),
        iana="America/New_York",
        day=date(2026, 10, 11),
        hours=(time(14, 0), time(20, 0)),
        ba_first_name="Riley",
        shared={
            "Event name / venue": "Fall Music Fest · Piedmont Park (Active Oval)",
            "Estimated attendance / foot traffic": "6000",
            "Sample format": "Sample cup / pour",
            "Key highlight (1–2 sentences) / What worked well & What didn't?": (
                'A three-brand "day-to-night" table worked: Wook Water in the afternoon '
                "heat, FAMUS before the headliner, Alcohol Armor shots as the sun went "
                "down. Lines bunched at 6pm — a second pourer would help."
            ),
            "Demographics (general age, sex, ethnicities of consumers)": (
                "Mostly 21-40, even male/female split. Festival crowd with lots of friend "
                "groups; ID checked before any Alcohol Armor shot."
            ),
        },
        brands=(
            event_brand(
                FM,
                sampled=380,
                aware=52,
                skus=[(FAMUS_SKUS[0], 210), (FAMUS_SKUS[1], 170)],
                feedback="Peak demand right before the headliner; OG Luxe was the go-to.",
                quotes='"Exactly what I needed for the last set." (male, ~26)',
            ),
            event_brand(
                AA,
                sampled=240,
                aware=31,
                skus=[(AA_SKUS[0], 130), (AA_SKUS[1], 110)],
                feedback="Groups took shots together before heading to the bars after the show.",
                quotes='"Smart move putting this at a festival." (female, ~31)',
            ),
            event_brand(
                WW,
                sampled=450,
                aware=77,
                skus=[(WW_SKUS[0], 160), (WW_SKUS[1], 150), (WW_SKUS[2], 140)],
                feedback="Afternoon heat made Wook Water the first stop; Berry Groovy was a hit with families.",
                quotes='"Free hydration at a festival? Legends." (male, ~24)',
            ),
        ),
        photos=(
            ("Activation Setup", 2),
            ("Consumer Sampling Pictures", 3),
            ("Expense Receipts", 1),
        ),
    ),
)
