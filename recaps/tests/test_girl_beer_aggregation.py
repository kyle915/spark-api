"""
Coverage for Girl Beer-vocabulary KPI aggregation.

Girl Beer's custom template names its consumer/sample fields differently
from the Borjomi-era vocabulary the aggregators were built against:

  * sampled consumers live in demographics rows — "Men who sampled
    (Total)" / "Women who sampled (Total)" — not a "Consumers Sampled"
    headline;
  * the samples headline is "Total Samples Given Out";
  * packs are mostly "…6-packs Sold" (6 cans, not the legacy ×12).

The dashboard consequently showed 0 consumers sampled next to 720 cans,
0.0% awareness for a tenant that never collects awareness, and a ×12
cans conversion for 6-pack SKUs. These tests pin the widened matchers —
and that the original Borjomi-style labels still resolve identically.
Pure helper style (no DB), mirroring test_custom_recap_card_aggregates.
"""

from recaps.report_service import _custom_engagement_totals
from recaps.types import (
    _consumers_sampled_from_fields,
    _samples_given_from_fields,
)
from tenants.dashboard.queries import _pack_size_from_label


GB_PAIRS = [
    ("Store Associate Spoken To", "Jess"),
    ("What flavors were available to taste?", "Peach, Yuzu"),
    ("# of PURPLE Variety Packs sold", "3"),
    ("Peach 6-packs Sold", "4"),
    ("Total Samples Given Out", "95"),
    ("Foot Traffic (number of people walking by demo table per hour)", "200"),
    ("Number of Customers Engaged (talked to or sampled product)", "120"),
    ("Men who sampled (21-29)", "10"),
    ("Men who sampled (30-39)", "16"),
    ("Men who sampled (Total)", "38"),
    ("Women who sampled (21-29)", "20"),
    ("Women who sampled (Total)", "52"),
    ("Men who bought (Total)", "9"),
    ("Women who bought (Total)", "14"),
]

BORJOMI_PAIRS = [
    ("Consumers Sampled", "120"),
    ("How many consumers knew about the brand?", "40"),
    ("How many would be willing to purchase?", "33"),
    ("Single cans sold", "18"),
    ("Multi packs sold", "5"),
]


# ---------------------------------------------------------------------------
# consumers sampled — demographics fallback
# ---------------------------------------------------------------------------


def test_gb_consumers_from_demographic_totals_only():
    # 38 + 52 from the two "(Total)" rows; the age brackets and the
    # "who bought" rows must NOT count (double counting / wrong metric).
    assert _consumers_sampled_from_fields(GB_PAIRS) == 90


def test_explicit_consumers_sampled_headline_still_wins():
    pairs = [("Consumers Sampled", "77")] + GB_PAIRS
    assert _consumers_sampled_from_fields(pairs) == 77


def test_borjomi_consumers_unchanged():
    assert _consumers_sampled_from_fields(BORJOMI_PAIRS) == 120


def test_no_consumer_fields_is_none():
    assert _consumers_sampled_from_fields([("Foot Traffic", "200")]) is None


# ---------------------------------------------------------------------------
# samples given out
# ---------------------------------------------------------------------------


def test_gb_samples_given_out():
    assert _samples_given_from_fields(GB_PAIRS) == 95


def test_samples_given_none_when_absent():
    assert _samples_given_from_fields(BORJOMI_PAIRS) is None


# ---------------------------------------------------------------------------
# campaign-report engagement totals — demographics fallback
# ---------------------------------------------------------------------------


def test_report_totals_gb_demographics_fallback():
    out = _custom_engagement_totals(GB_PAIRS)
    assert out["total_consumer"] == 90
    # GB never collects these — absolute zeros are correct here.
    assert out["first_time_consumers"] == 0
    assert out["brand_aware_consumers"] == 0
    assert out["willing_to_purchase_consumers"] == 0


def test_report_totals_borjomi_unchanged():
    out = _custom_engagement_totals(BORJOMI_PAIRS)
    assert out["total_consumer"] == 120
    assert out["brand_aware_consumers"] == 40
    assert out["willing_to_purchase_consumers"] == 33


def test_report_totals_headline_beats_demographics():
    out = _custom_engagement_totals(
        [("Consumers Sampled", "77"), ("Men who sampled (Total)", "38")]
    )
    assert out["total_consumer"] == 77


# ---------------------------------------------------------------------------
# pack size from label
# ---------------------------------------------------------------------------


def test_pack_size_six_pack():
    assert _pack_size_from_label("peach 6-packs sold") == 6


def test_pack_size_spaced_and_unhyphenated():
    assert _pack_size_from_label("blueberry 4 pack sold") == 4


def test_pack_size_defaults_to_twelve():
    assert _pack_size_from_label("# of purple variety packs sold") == 12
    assert _pack_size_from_label("multi packs sold") == 12
