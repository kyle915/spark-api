import pytest

from utils.graphql.validation import clamp_percentage


def test_clamp_percentage_basic_behavior():
    assert clamp_percentage(-5.0) == 0.0
    assert clamp_percentage(0.0) == 0.0
    assert clamp_percentage(42.3) == 42.3
    assert clamp_percentage(100.0) == 100.0
    assert clamp_percentage(150.0) == 100.0

