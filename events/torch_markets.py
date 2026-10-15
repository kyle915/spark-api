"""Which Torch RMM market (Miami / Orlando / Houston / Austin-Dallas) a place is in.

Reads the address through the shared parser (``events.market``) and maps its
zip / city onto the four field-marketing markets. Torch's state routing
(``torch_retail_routing``) can't split them — Miami and Orlando are both FL,
Houston and Austin/Dallas both TX — so the split is by metro zip prefix,
then city. Anything outside those metros has no market.
"""

from __future__ import annotations

from events.market import market_for, parse_address_geo

# USPS 3-digit zip prefixes of each metro.
_ZIP3: dict[str, frozenset[str]] = {
    "miami": frozenset({"330", "331", "332", "333", "334"}),
    "orlando": frozenset({"327", "328", "347"}),
    "houston": frozenset({"770", "772", "773", "774", "775"}),
    "austin-dallas": frozenset({"750", "751", "752", "753", "760", "761", "762", "786", "787"}),
}

_CITIES: dict[str, frozenset[str]] = {
    "miami": frozenset(
        {
            "miami", "miami beach", "north miami", "north miami beach", "miami gardens",
            "miami lakes", "doral", "hialeah", "coral gables", "homestead", "kendall",
            "aventura", "sunny isles beach", "key biscayne", "hollywood", "dania beach",
            "fort lauderdale", "ft lauderdale", "ft. lauderdale", "pompano beach",
            "hallandale beach", "pembroke pines", "miramar", "davie", "plantation",
            "sunrise", "weston", "coral springs", "boca raton", "delray beach",
            "boynton beach", "west palm beach", "palm beach", "jupiter",
        }
    ),
    "orlando": frozenset(
        {
            "orlando", "kissimmee", "winter park", "sanford", "lake mary",
            "altamonte springs", "oviedo", "winter garden", "clermont", "apopka",
            "ocoee", "celebration", "lake buena vista", "maitland", "longwood",
            "windermere", "st. cloud", "st cloud", "saint cloud",
        }
    ),
    "houston": frozenset(
        {
            "houston", "pasadena", "katy", "sugar land", "the woodlands", "spring",
            "pearland", "humble", "cypress", "league city", "baytown", "conroe",
            "galveston", "missouri city", "tomball", "kingwood", "friendswood",
            "bellaire", "webster", "richmond", "rosenberg",
        }
    ),
    "austin-dallas": frozenset(
        {
            "austin", "round rock", "cedar park", "pflugerville", "georgetown",
            "san marcos", "leander", "kyle", "buda", "dallas", "fort worth", "plano",
            "arlington", "irving", "frisco", "garland", "mckinney", "denton",
            "grand prairie", "richardson", "carrollton", "addison", "lewisville",
            "mesquite", "allen", "flower mound", "grapevine", "southlake",
        }
    ),
}
_STATES = {"miami": "FL", "orlando": "FL", "houston": "TX", "austin-dallas": "TX"}
# Venue-only addresses the parser can't read ("Clutch City Cruisers, Houston").
_WORDS = {
    "miami": ("miami",),
    "orlando": ("orlando",),
    "houston": ("houston",),
    "austin-dallas": ("austin", "dallas"),
}


def _from_city(city: str | None, state: str | None) -> str | None:
    name = (city or "").strip().lower()
    for key, cities in _CITIES.items():
        if name in cities and (not state or _STATES[key] == state):
            return key
    return None


def torch_market_for_event(event) -> str | None:
    """Market key from the event's own address (then its Location), else None."""
    if event is None:
        return None
    address = getattr(event, "address", None) or ""
    geo = parse_address_geo(address)
    if geo.zip:
        for key, prefixes in _ZIP3.items():
            if geo.zip[:3] in prefixes:
                return key
    place = market_for(event)
    key = _from_city(place.city, place.state_code)
    if key:
        return key
    low = address.lower()
    for key, words in _WORDS.items():
        if any(word in low for word in words) and (
            not place.state_code or _STATES[key] == place.state_code
        ):
            return key
    return None
