"""Feel Free sampling recap: the seeder's shape + the KPI matcher it needs.

The matcher half is the important part. Feel Free's client PDF asks
"How many TOTAL consumers did you sample?" — the same metric every other brand
reports, worded as a question. Before this it matched nothing, so seeding their
template verbatim would have shipped a dashboard reading "—" with the number
sitting right there in every recap. Same class of silent failure as the Girl
Beer vocabulary miss.
"""

from __future__ import annotations

import pytest

from recaps.management.commands.setup_feel_free_checkin import (
    CODE_PREFIX,
    SPEC,
    TEMPLATE_NAME,
)
from recaps.types import (
    _consumers_sampled_from_fields,
    _engagement_totals_from_field_pairs,
)


class TestConsumersSampledMatcher:
    def test_feel_free_question_wording_is_counted(self):
        """The regression this change exists for."""
        assert (
            _consumers_sampled_from_fields(
                [("How many TOTAL consumers did you sample?", "192")]
            )
            == 192
        )

    @pytest.mark.parametrize(
        "label",
        [
            "Consumers Sampled",
            "Total consumers sampled",
            "How many consumers did you sample?",
            "How many TOTAL consumers did you sample?",
        ],
    )
    def test_all_known_phrasings_count(self, label):
        assert _consumers_sampled_from_fields([(label, "40")]) == 40

    @pytest.mark.parametrize(
        "label",
        [
            # Descriptive, not a count — prose would digit-mash into nonsense.
            "General demographics of consumers sampled (age range, gender)",
            "Demographics",
            # Different metric entirely; must not be mistaken for the count.
            "How many consumers would be willing to purchase the product "
            "after tasting it?",
            "How many consumers had tried a Feel Free flavor before?",
        ],
    )
    def test_neighbouring_feel_free_fields_do_not_match(self, label):
        assert _consumers_sampled_from_fields([(label, "60")]) is None

    def test_prose_value_is_never_mashed_into_a_count(self):
        assert (
            _consumers_sampled_from_fields(
                [("How many TOTAL consumers did you sample?", "roughly 40 to 60")]
            )
            is None
        )

    def test_first_real_count_wins_over_a_descriptive_field(self):
        assert (
            _consumers_sampled_from_fields(
                [
                    ("Demographics of consumers sampled", "25-55 years old"),
                    ("How many TOTAL consumers did you sample?", "192"),
                ]
            )
            == 192
        )


    def test_first_time_derived_from_sampled_minus_tried_before(self):
        pairs = [
            ("How many TOTAL consumers did you sample?", "192"),
            ("How many consumers had tried a Feel Free flavor before?", "60"),
        ]
        totals = _engagement_totals_from_field_pairs(pairs)
        assert totals["total_consumer"] == 192
        assert totals["first_time_consumers"] == 132

    def test_explicit_first_time_label_wins_over_derivation(self):
        pairs = [
            ("Consumers Sampled", "100"),
            ("First time consumers", "25"),
            ("How many consumers had tried a Feel Free flavor before?", "40"),
        ]
        totals = _engagement_totals_from_field_pairs(pairs)
        assert totals["first_time_consumers"] == 25


class TestFeelFreeSpec:
    """Pins the template to the client's PDF so a future edit is deliberate."""

    def test_covers_every_field_on_the_client_pdf(self):
        names = [f[0] for _, fields in SPEC for f in fields]
        assert names == [
            "Quantity Distributed of Kava Matte",
            "Quantity Distributed of Classic Tonic",
            "How many TOTAL consumers did you sample?",
            "How many consumers would be willing to purchase the product "
            "after tasting it?",
            "How many consumers that were engaged with knew about Feel Free "
            "product/brand?",
            "How many consumers had tried a Feel Free flavor before?",
            "Demographics",
            "What were the top 5 frequently asked questions you received "
            "from consumers?",
            "Sampling Pictures",
            "Where did you sample? (name a few locations)",
            "Sampling Timeframe?",
            "Helpful feedback",
        ]

    def test_date_and_location_are_not_template_fields(self):
        """They belong to the event the check-in resolves; duplicating them
        makes the BA type what Spark already knows."""
        lowered = [f[0].lower() for _, fields in SPEC for f in fields]
        assert not any("todays date" in n or "event location" in n for n in lowered)

    def test_the_sampled_count_field_is_matchable(self):
        """Guards the seeder against drifting away from the KPI matcher — the
        exact failure this module documents."""
        sampled = [
            f[0]
            for _, fields in SPEC
            for f in fields
            if _consumers_sampled_from_fields([(f[0], "1")]) == 1
        ]
        assert len(sampled) == 1, f"expected exactly one countable field, got {sampled}"

    def test_photo_and_number_kinds_are_canonical_tokens(self):
        kinds = {f[1] for _, fields in SPEC for f in fields}
        assert kinds <= {"text", "number", "longtext", "image", "select", "multiselect"}

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "FF-"
        assert TEMPLATE_NAME.startswith("Feel Free")
