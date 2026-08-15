"""Clock-out starts a draft recap when none exists."""

import pytest

from recaps.clock_out_recap import start_recap_on_clock_out


@pytest.mark.django_db
def test_start_recap_on_clock_out_returns_none_for_missing_attendance():
    assert start_recap_on_clock_out(-1) is None
