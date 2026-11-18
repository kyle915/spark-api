"""The fixed words a recap photo is tagged with, and how each one reads.

Every list here is closed: the model must answer with one of these keys
(strict JSON schema), the search filters accept only these keys, and the
natural-language search box is mapped onto them. Keys are never renamed, so
a photo tagged last spring still matches a search made today.

Nothing here describes a person. ``people_present`` is yes or no and
``crowd_size`` is a rough count bucket; no one is identified or described.
"""

from __future__ import annotations

# What the photo shows. A photo may show several.
SHOWS = (
    "product_on_table",
    "shelf_display",
    "consumers_engaging",
    "ba_at_table",
    "signage",
    "store_exterior",
    "receipt",
    "other",
)
SHOW_LABELS = {
    "product_on_table": "Product on table",
    "shelf_display": "Shelf display",
    "consumers_engaging": "Consumers engaging",
    "ba_at_table": "Ambassador at table",
    "signage": "Signage",
    "store_exterior": "Store front",
    "receipt": "Receipt",
    "other": "Other",
}
# Searchable tag types. A receipt is never in the library.
SEARCHABLE_SHOWS = tuple(s for s in SHOWS if s != "receipt")

CROWD_SIZES = ("none", "few", "group", "crowd")
CROWD_LABELS = {
    "none": "No one in frame",
    "few": "1 to 5 people",
    "group": "6 to 15 people",
    "crowd": "More than 15 people",
}

SETTINGS = ("indoor_store", "outdoor_event", "bar_restaurant", "other")
SETTING_LABELS = {
    "indoor_store": "In store",
    "outdoor_event": "Outdoor event",
    "bar_restaurant": "Bar or restaurant",
    "other": "Other setting",
}

ISSUES = ("blurry", "dark", "overexposed", "low_resolution")
ISSUE_LABELS = {
    "blurry": "Blurry",
    "dark": "Too dark",
    "overexposed": "Washed out",
    "low_resolution": "Low resolution",
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

SORTS = ("relevance", "quality", "newest")


def labels_for(keys, table: dict[str, str]) -> list[str]:
    return [table[k] for k in keys or [] if k in table]
