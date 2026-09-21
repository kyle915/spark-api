from ambassadors.checkin_web import parse_used_corpo_card


def test_parse_used_corpo_card():
    assert parse_used_corpo_card(True) is True
    assert parse_used_corpo_card(False) is False
    assert parse_used_corpo_card("Yes") is True
    assert parse_used_corpo_card("no") is False
    assert parse_used_corpo_card(None) is None
    assert parse_used_corpo_card("") is None
