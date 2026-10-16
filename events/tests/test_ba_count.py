from events.ba_count import BaCountError, ba_count_from_request, normalize_ba_count
import pytest


def test_normalize_accepts_one_through_twenty():
    assert normalize_ba_count(1) == 1
    assert normalize_ba_count("20") == 20
    assert normalize_ba_count(None) is None
    assert normalize_ba_count("") is None


def test_normalize_rejects_out_of_range():
    with pytest.raises(BaCountError):
        normalize_ba_count(0)
    with pytest.raises(BaCountError):
        normalize_ba_count(21)
    with pytest.raises(BaCountError):
        normalize_ba_count("nope")


class _Request:
    def __init__(self, *, ba_count=None, notes=""):
        self.ba_count = ba_count
        self.notes = notes


def test_stored_count_wins_over_notes():
    req = _Request(ba_count=4, notes="BA count: 2")
    assert ba_count_from_request(req) == 4


def test_notes_fallback_when_unset():
    req = _Request(notes="Please staff Saturday.\nBA count: 2")
    assert ba_count_from_request(req) == 2
    assert ba_count_from_request(_Request(notes="just notes")) is None
