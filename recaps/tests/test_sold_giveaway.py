"""Give-away wording never counts as sold.

Torch's guerilla template asks "TOTAL - how many cans did you give away?".
The word "cans" made it a SOLD line, so free samples reached the client as
sales. The field-name lists below come from every template seeder, data file
and fixture in this repo (enumerated for the fix): the give-away names stop
counting, every sold name still counts.
"""

import pytest

from recaps import metric_mapping
from recaps.types import _is_giveaway_name, _sold_unit_lines, _sold_units_from_fields

GIVEAWAY_NAMES = [
    "TOTAL - how many cans did you give away?",
    "Cans Sampled",
    "Total cans sampled",
    "Full can samples",
    "Can handout",
    "Packs given out",
    "Free cans",
    "Comped packs",
    "Cans donated",
    "Cans distributed",
]

# Real sold field names across tenants' templates; none may stop counting.
SOLD_NAMES = [
    "# of Single Cans Sold",
    "# of Multi Packs Sold",
    "# of Total Cans Sold",
    "# of PURPLE Variety Packs sold",
    "6-Packs Sold",
    "Blueberry Lavender 6-packs Sold",
    "Cans Sold",
    "Packs sold",
    "MULTIPACKS SOLD",
    "How many single cans did consumers purchase?",
    "How many multipacks did consumers purchase?",
    "How many packs did consumers purchase?",
    "How many single cans / bottles / shots did consumers purchase?",
    "How many products did consumers purchase during the event?",
    "Loaves Sold",
    "Products Sold",
    "Units sold: Almond Oil",
    # A sale word wins over a give-away word in the same name.
    "Cans sold to consumers who sampled",
]


@pytest.mark.parametrize("name", GIVEAWAY_NAMES)
def test_giveaway_is_not_sold(name):
    assert _is_giveaway_name(name)
    assert _sold_units_from_fields([(name, "40")]) is None


@pytest.mark.parametrize("name", SOLD_NAMES)
def test_sold_names_still_count(name):
    assert not _is_giveaway_name(name)
    assert _sold_units_from_fields([(name, "7")]) == 7


def test_torch_guerilla_recap_reports_no_sales():
    pairs = [
        ("TOTAL - how many cans did you give away?", "40"),
        ("How many QR Code Scans", "6"),
        ("Koozies Distributed (total out of 25)", "25"),
        ("Number of people asking where they could buy the product?", "9"),
    ]
    assert _sold_units_from_fields(pairs) is None


def test_sampled_cans_next_to_sold_cans_count_once():
    pairs = [("Cans sampled", "96"), ("Single Cans Sold", "12"), ("Packs Sold", "3")]
    assert _sold_units_from_fields(pairs) == 15


def test_old_rule_still_reachable_for_the_audit():
    pairs = [("TOTAL - how many cans did you give away?", "40")]
    assert _sold_unit_lines(pairs, exclude_giveaway=False) == [
        ("TOTAL - how many cans did you give away?", 40)
    ]


def test_reasons_to_decline_is_never_sold():
    """Torch's prose question matched the "purchase" fallback."""
    pairs = [("Reasons to decline to purchase", "1. Came for something else")]
    assert _sold_units_from_fields(pairs) is None


def test_staff_confirmation_beats_the_wording():
    """A field staff confirmed as units sold counts, whatever it says."""
    name = metric_mapping.MappedName(
        "Cans handed out at checkout", metric_mapping.UNITS_SOLD_SINGLE
    )
    assert _sold_units_from_fields([(name, "11")]) == 11
