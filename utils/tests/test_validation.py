import pytest
from graphql import GraphQLError

from utils.graphql.validation import (
    clamp_percentage,
    validate_consumer_engagements_counts,
)


def test_clamp_percentage_basic_behavior():
    assert clamp_percentage(-5.0) == 0.0
    assert clamp_percentage(0.0) == 0.0
    assert clamp_percentage(42.3) == 42.3
    assert clamp_percentage(100.0) == 100.0
    assert clamp_percentage(150.0) == 100.0


def test_validate_consumer_engagements_counts_all_ok():
    # Totals and sub-counts within bounds should pass without error.
    validate_consumer_engagements_counts(
        total_consumer=100,
        first_time_consumers=40,
        brand_aware_consumers=60,
        willing_to_purchase_consumers=50,
        not_willing_consumers=30,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_consumer": 100, "first_time_consumers": 101, "brand_aware_consumers": 0, "willing_to_purchase_consumers": 0, "not_willing_consumers": 0},
        {"total_consumer": 100, "first_time_consumers": 0, "brand_aware_consumers": 101, "willing_to_purchase_consumers": 0, "not_willing_consumers": 0},
        {"total_consumer": 100, "first_time_consumers": 0, "brand_aware_consumers": 0, "willing_to_purchase_consumers": 101, "not_willing_consumers": 0},
        {"total_consumer": 100, "first_time_consumers": 0, "brand_aware_consumers": 0, "willing_to_purchase_consumers": 0, "not_willing_consumers": 101},
        {"total_consumer": 100, "first_time_consumers": 0, "brand_aware_consumers": 0, "willing_to_purchase_consumers": 60, "not_willing_consumers": 50},
    ],
)
def test_validate_consumer_engagements_counts_invalid(kwargs):
    with pytest.raises(GraphQLError):
        validate_consumer_engagements_counts(**kwargs)

